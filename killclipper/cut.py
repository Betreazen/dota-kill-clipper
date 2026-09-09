"""Clip bounds inside a saved replay-buffer file and the ffmpeg -c copy cut.

cut_clip() blocks (ffprobe + ffmpeg, a few seconds); the OBS script runs it in a
worker thread and reads the result on its timer tick. ffmpeg -ss before -i seeks to the
previous keyframe, so the clip starts up to one GOP earlier than requested (spec Q6).
"""
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash inside OBS


def find_ffmpeg(configured: str | None = None) -> str | None:
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("ffmpeg") or (DEFAULT_FFMPEG if os.path.isfile(DEFAULT_FFMPEG) else None)


def ffprobe_path(ffmpeg: str) -> str:
    head, tail = os.path.split(ffmpeg)
    return os.path.join(head, tail.replace("ffmpeg", "ffprobe", 1))


def clip_bounds(saved_at: float, duration: float, start_ts: float, end_ts: float) -> tuple[float, float, bool]:
    """Wall-clock clip window -> (start, end, truncated) offsets inside the replay file."""
    file_start = saved_at - duration
    start = start_ts - file_start
    end = end_ts - file_start
    truncated = start < 0 or end > duration
    return max(start, 0.0), min(end, duration), truncated


def ffmpeg_cmd(ffmpeg: str, src: Path, start: float, end: float, dst: Path) -> list[str]:
    return [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", str(src), "-c", "copy", str(dst)]


def probe_duration(ffmpeg: str, src: Path, run=subprocess.run) -> float:
    cmd = [ffprobe_path(ffmpeg), "-v", "error", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(src)]
    r = run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()}")
    return float(r.stdout.strip())


def cut_clip(ffmpeg: str, src: Path, dst: Path, start_ts: float, end_ts: float,
             saved_at: float, run=subprocess.run) -> dict:
    """Cut src -> dst. Success deletes src; failure moves src next to dst as <name>_raw.mp4."""
    src, dst = Path(src), Path(dst)
    res = {"ok": False, "path": str(dst), "start": None, "end": None, "duration": None,
           "truncated": False, "error": None}
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        duration = probe_duration(ffmpeg, src, run)
        start, end, truncated = clip_bounds(saved_at, duration, start_ts, end_ts)
        res.update(start=start, end=end, duration=duration, truncated=truncated)
        r = run(ffmpeg_cmd(ffmpeg, src, start, end, dst), capture_output=True, text=True,
                creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg exit {r.returncode}: {r.stderr.strip()}")
        src.unlink()
        res["ok"] = True
    except (OSError, RuntimeError, ValueError) as e:
        res["error"] = f"{type(e).__name__}: {e}"
        raw = dst.with_name(dst.stem + "_raw.mp4")
        try:
            dst.unlink(missing_ok=True)
            src.replace(raw)
            res["path"] = str(raw)
        except OSError as e2:
            res["error"] += f"; keep raw failed: {e2}"
            res["path"] = str(src)
    return res
