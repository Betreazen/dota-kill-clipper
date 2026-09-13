"""Vertical 9:16 Shorts render: square gameplay over a blurred, darkened copy, looping CTA bands, music.

Layout on the 1080x1920 canvas: 420 px top band, 1080x1080 gameplay square, 420 px bottom band.
CTA files are scaled to fit their band (recommended source size 1080x420 with alpha) and loop
for the whole clip. The horizontal source clip is never modified.
"""
import json
import os
import random
import subprocess
from pathlib import Path

from .cut import probe_duration

WIDTH, HEIGHT, BAND = 1080, 1920, 420
AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
MUSIC_FADE = 1.5
ENCODERS = {
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
              "-maxrate", "16M", "-bufsize", "32M"],
    "x264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
}
CREDIT = ('"{title}" Kevin MacLeod (incompetech.com)\n'
          "Licensed under Creative Commons: By Attribution 4.0 License\n"
          "http://creativecommons.org/licenses/by/4.0/")
# no console window inside OBS; below-normal priority so the game keeps the CPU
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def choose_music(folder: str, rng=random) -> str:
    """Random track from the music folder, or "" when none is configured."""
    if not folder:
        return ""
    try:
        tracks = sorted(str(p) for p in Path(folder).iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)
    except OSError:
        return ""
    return rng.choice(tracks) if tracks else ""


def music_credit(path: str) -> str:
    """CC BY 4.0 attribution; title from credits.json written by tools/fetch_music.py."""
    if not path:
        return ""
    track = Path(path)
    try:
        titles = json.loads((track.parent / "credits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        titles = {}
    return CREDIT.format(title=titles.get(track.name) or track.stem)


def _cta_input(path: str) -> list[str]:
    # ffmpeg's native VP9 decoder drops the alpha plane; libvpx-vp9 keeps it
    decoder = ["-c:v", "libvpx-vp9"] if Path(path).suffix.lower() == ".webm" else []
    return ["-stream_loop", "-1", *decoder, "-i", path]


def vertical_cmd(ffmpeg: str, src: Path, dst: Path, duration: float, upper_cta: str = "", lower_cta: str = "",
                 music: str = "", music_volume: float = 0.15, encoder: list[str] = ENCODERS["x264"]) -> list[str]:
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-hwaccel", "auto", "-i", str(src)]
    filters = [
        "[0:v]split=2[bgsrc][gamesrc]",
        # blur a small copy and scale it up: same look, a fraction of the CPU
        f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
        f"scale={WIDTH}:{HEIGHT},eq=brightness=-0.25:saturation=0.7[bg]",
        f"[gamesrc]scale={WIDTH}:{WIDTH}:force_original_aspect_ratio=increase,crop={WIDTH}:{WIDTH}[game]",
        f"[bg][game]overlay=0:{BAND}[v0]",
    ]
    video, index = "v0", 1
    for path, y in ((upper_cta, f"({BAND}-h)/2"), (lower_cta, f"{BAND + WIDTH}+({BAND}-h)/2")):
        if not path:
            continue
        cmd += _cta_input(path)
        filters.append(f"[{index}:v]scale={WIDTH}:{BAND}:force_original_aspect_ratio=decrease,format=rgba[cta{index}]")
        filters.append(f"[{video}][cta{index}]overlay=(W-w)/2:{y}:format=auto[v{index}]")
        video, index = f"v{index}", index + 1
    audio = "0:a?"
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        fade_start = max(duration - MUSIC_FADE, 0.0)
        filters.append(f"[{index}:a]volume={music_volume:.3f},afade=t=out:st={fade_start:.3f}:d={MUSIC_FADE:.3f}[music]")
        filters.append("[0:a][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        audio = "[aout]"
    return cmd + ["-filter_complex", ";".join(filters), "-map", f"[{video}]", "-map", audio,
                  "-t", f"{duration:.3f}", *encoder, "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]


def render_vertical(ffmpeg: str, clip: Path, dst: Path, upper_cta: str = "", lower_cta: str = "",
                    music: str = "", music_volume: float = 0.15, run=subprocess.run) -> Path:
    """Render clip -> dst (NVENC, then libx264 as fallback). Raises RuntimeError/OSError on failure."""
    clip, dst = Path(clip), Path(dst)
    duration = probe_duration(ffmpeg, clip, run)
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.stem + ".rendering.mp4")
    error = ""
    for name, encoder in ENCODERS.items():
        cmd = vertical_cmd(ffmpeg, clip, temp, duration, upper_cta, lower_cta, music, music_volume, encoder)
        try:
            r = run(cmd, capture_output=True, text=True, creationflags=_FLAGS)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
        if r.returncode == 0:
            os.replace(temp, dst)
            return dst
        error = f"{name}: ffmpeg exit {r.returncode}: {r.stderr.strip()[-500:]}"
        temp.unlink(missing_ok=True)
    raise RuntimeError(error)
