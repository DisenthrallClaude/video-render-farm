#!/usr/bin/env python3
"""Render one chunk of the 420 s procedural film to an H.264 segment.

The film is a deterministic WebGL/canvas page exposing window.renderAt(t) inside an
embed iframe.  A chunk is a contiguous range of frames; every chunk is encoded with
identical x264 settings so the segments can be concatenated with `-c copy`.

Usage (one shot, used by CI):
    python render_chunk.py --chunk 0 --chunks 20 --url "https://...arena.site/?embed=true"

Usage (resumable, used locally in time-boxed slices):
    python render_chunk.py render --chunk 0 --chunks 20 --format jpeg --budget 200
    python render_chunk.py encode --chunk 0 --chunks 20 --format jpeg

Frames are written to <workdir>/frames as fNNNNN.png (or .jpg) and kept between runs,
so a render stopped by --budget resumes where it left off.
"""
import argparse
import base64
import os
import pathlib
import shutil
import subprocess
import sys
import time

FPS = 24
TOTAL = 10080          # 420 s
W, H = 1920, 1080
MIN_IMG = 20000        # a rendered 1080p frame is far larger than this
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

X264 = [
    "-c:v", "libx264", "-preset", "slow", "-crf", "23",
    "-g", "48", "-keyint_min", "12", "-sc_threshold", "0",
    "-profile:v", "high", "-level", "4.2",
    "-pix_fmt", "yuv420p", "-video_track_timescale", "24000",
    "-movflags", "+faststart", "-an",
]

HIDE_JS = """() => {
  const kill = ['.floating-banner', '[class*=floating-banner]'];
  for (const sel of kill) document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
  document.querySelectorAll('body > div, body > a, body > button').forEach(e => {
    if (e.querySelector('iframe')) return;
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.width * r.height < 1920 * 1080 * 0.25) e.style.display = 'none';
  });
  const st = document.createElement('style');
  st.textContent = 'html,body{overflow:hidden!important;background:#000!important;cursor:none!important}::-webkit-scrollbar{display:none!important}';
  document.head.appendChild(st);
  return true;
}"""


def log(*a):
    print(*a, flush=True)


def boot(p, url):
    browser = p.chromium.launch(args=[
        "--no-sandbox", "--font-render-hinting=none",
        "--force-color-profile=srgb", "--disable-lcd-text",
        "--hide-scrollbars", "--mute-audio",
    ])
    page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
    log("opening film page")
    page.goto(url, wait_until="networkidle", timeout=180000)
    frame = None
    t0 = time.time()
    while time.time() - t0 < 150:
        for f in page.frames:
            if "embed=true" in f.url:
                frame = f
        if frame:
            break
        time.sleep(0.5)
    if frame is None:
        raise SystemExit("FATAL: embed iframe not found (preview page changed or expired)")
    frame.wait_for_function("() => window.__ready === true", timeout=300000)
    info = frame.evaluate(
        "() => ({fonts: window.__fontsOK, missing: window.__missingFonts || [], "
        "fps: window.FPS, total: window.TOTAL_DURATION, w: window.innerWidth, h: window.innerHeight})"
    )
    log(f"ready fonts={info}")
    if info.get("fps") not in (None, FPS):
        log(f"WARNING: page FPS={info.get('fps')} expected {FPS}")
    if info.get("w") != W or info.get("h") != H:
        log(f"WARNING: canvas viewport {info.get('w')}x{info.get('h')} expected {W}x{H}")
    page.evaluate(HIDE_JS)
    cdp = page.context.new_cdp_session(page)
    return browser, page, frame, cdp


def frame_path(frames_dir, i, ext):
    return frames_dir / f"f{i:05d}.{ext}"


def grab(page, cdp, path, fmt, quality):
    """Screenshot the 1920x1080 viewport. CDP path is fastest; falls back to Playwright."""
    params = {
        "format": "png" if fmt == "png" else "jpeg",
        "fromSurface": True,
        "captureBeyondViewport": False,
        "clip": {"x": 0, "y": 0, "width": W, "height": H, "scale": 1},
    }
    if fmt != "png":
        params["quality"] = quality
    if cdp is not None:
        data = base64.b64decode(cdp.send("Page.captureScreenshot", params)["data"])
        path.write_bytes(data)
    else:
        kwargs = {"type": "png"} if fmt == "png" else {"type": "jpeg", "quality": quality}
        page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H},
                        animations="disabled", caret="hide", **kwargs)


def render_range(page, frame, cdp, start, end, frames_dir, fmt, quality, budget):
    ext = "png" if fmt == "png" else "jpg"
    t_start = time.time()
    n_done = 0
    i = start
    for i in range(start, end):
        path = frame_path(frames_dir, i, ext)
        if path.exists() and path.stat().st_size > MIN_IMG:
            continue
        if budget and time.time() - t_start > budget:
            log(f"budget reached at frame {i}")
            return i
        tmp = frame_path(frames_dir, i, ext + ".tmp")
        for attempt in range(1, 5):
            try:
                frame.evaluate("(t) => window.renderAt(t)", i / FPS)
                grab(page, cdp, tmp, fmt, quality)
                if tmp.stat().st_size < MIN_IMG:
                    raise RuntimeError(f"tiny screenshot {tmp.stat().st_size}B")
                os.replace(tmp, path)
                break
            except Exception as exc:  # noqa: BLE001
                log(f"  frame {i} attempt {attempt} failed: {exc}")
                if attempt == 4:
                    raise
                time.sleep(2)
        n_done += 1
        if n_done % 50 == 0:
            el = time.time() - t_start
            rate = el / n_done
            log(f"  f{i} ({i / FPS:.1f}s) {rate * 1000:.0f}ms/frame "
                f"eta {(end - i) * rate / 60:.1f} min")
    el = time.time() - t_start
    if n_done:
        log(f"rendered {n_done} frames in {el:.0f}s ({el / n_done * 1000:.0f}ms/frame)")
    return end


def encode_range(start, end, frames_dir, fmt, seg):
    ext = "png" if fmt == "png" else "jpg"
    part = seg.with_suffix(".part.mp4")
    cmd = [FFMPEG, "-y", "-loglevel", "warning", "-stats",
           "-framerate", str(FPS), "-start_number", str(start),
           "-i", str(frames_dir / f"f%05d.{ext}"),
           "-vf", "hqdn3d=5:4:8:8,format=yuv420p"] + X264 + [str(part)]
    log(f"encoding frames {start}..{end - 1} -> {seg.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    os.replace(part, seg)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
             "-of", "default=nw=1", str(seg)],
            capture_output=True, text=True, check=True).stdout.strip().replace("\n", " ")
    except FileNotFoundError:
        probe = "ffprobe not installed"
    log(f"segment {seg.name}: {seg.stat().st_size / 1e6:.1f} MB  {probe}")


def geometry(args):
    per = -(-TOTAL // args.chunks)
    start = args.chunk * per
    end = min(TOTAL, start + per)
    if start >= TOTAL:
        raise SystemExit(f"FATAL: chunk {args.chunk} outside {TOTAL} frames")
    return start, end, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?", default="auto", choices=["auto", "render", "encode"])
    ap.add_argument("--chunk", type=int, required=True)
    ap.add_argument("--chunks", type=int, default=20)
    ap.add_argument("--url", default=os.environ.get("FILM_URL", ""))
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--format", default="png", choices=["png", "jpeg"])
    ap.add_argument("--quality", type=int, default=100)
    ap.add_argument("--budget", type=float, default=0)   # seconds of rendering, 0 = no limit
    args = ap.parse_args()

    start, end, _ = geometry(args)
    work = pathlib.Path(args.workdir).resolve()
    frames_dir = work / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    seg = work / f"seg_{args.chunk:02d}.mp4"
    ext = "png" if args.format == "png" else "jpg"
    log(f"chunk {args.chunk}/{args.chunks}: frames {start}..{end - 1} "
        f"({end - start} frames, {args.format})")

    if args.stage in ("auto", "render"):
        have = sum(1 for i in range(start, end) if frame_path(frames_dir, i, ext).exists())
        if have < end - start:
            if not args.url:
                raise SystemExit("FATAL: no film URL (pass --url or set FILM_URL)")
            from playwright.sync_api import sync_playwright
            t0 = time.time()
            with sync_playwright() as p:
                browser, page, frame, cdp = boot(p, args.url)
                try:
                    render_range(page, frame, cdp, start, end, frames_dir, args.format,
                                 args.quality, args.budget)
                finally:
                    browser.close()
            log(f"boot+render {time.time() - t0:.0f}s")
        else:
            log("all frames already on disk")

        missing = [i for i in range(start, end) if not frame_path(frames_dir, i, ext).exists()]
        if missing:
            log(f"INCOMPLETE {len(missing)} frames missing (first {missing[:3]}), "
                f"next run resumes here")
            return 3

    if args.stage in ("auto", "encode"):
        have = sum(1 for i in range(start, end) if frame_path(frames_dir, i, ext).exists())
        if have != end - start:
            raise SystemExit(f"FATAL: cannot encode, {end - start - have} frames missing")
        encode_range(start, end, frames_dir, args.format, seg)
        for i in range(start, end):
            frame_path(frames_dir, i, ext).unlink(missing_ok=True)
        log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
