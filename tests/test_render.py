import json
import subprocess
from pathlib import Path

import pytest

from killclipper import render
from killclipper.render import ENCODERS, choose_music, music_credit, render_vertical, vertical_cmd


def test_vertical_cmd_layout_blur_bands_music_and_duration():
    cmd = vertical_cmd("ffmpeg.exe", Path("clip.mp4"), Path("short.mp4"), 20.0,
                       "top.mov", "bottom.mov", "music.mp3", 0.5, ENCODERS["x264"])
    text = " ".join(cmd)
    assert cmd[:7] == ["ffmpeg.exe", "-y", "-loglevel", "error", "-hwaccel", "auto", "-i"]
    assert cmd[7] == "clip.mp4"
    assert cmd.count("-stream_loop") == 3  # two CTA bands + music loop until the clip ends
    assert "split=2" in text and "boxblur" in text and "eq=brightness=-0.25" in text
    assert "crop=1080:1080" in text and "overlay=0:420" in text
    assert text.count("scale=1080:420:force_original_aspect_ratio=decrease") == 2  # CTA fits its band
    assert "overlay=(W-w)/2:(420-h)/2" in text and "overlay=(W-w)/2:1500+(420-h)/2" in text
    assert "volume=0.500" in text and "afade=t=out:st=18.500:d=1.500" in text
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0" in text
    assert cmd[cmd.index("-t") + 1] == "20.000"
    assert "libx264" in cmd and cmd[-1] == "short.mp4"


def test_webm_cta_uses_alpha_capable_decoder():
    cmd = vertical_cmd("ffmpeg.exe", Path("c.mp4"), Path("s.mp4"), 10.0, "top.webm", "", "", 0.1, ENCODERS["x264"])
    i = cmd.index("top.webm")
    assert cmd[i - 3:i + 1] == ["-c:v", "libvpx-vp9", "-i", "top.webm"]


def test_without_music_keeps_game_audio_only():
    cmd = vertical_cmd("ffmpeg.exe", Path("c.mp4"), Path("s.mp4"), 10.0, "", "", "", 0.1, ENCODERS["x264"])
    assert "amix" not in " ".join(cmd) and cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "0:a?"
    assert cmd.count("-stream_loop") == 0


def fake_run(fail_encoders=(), log=None):
    def run(cmd, **kw):
        if log is not None:
            log.append((cmd, kw))
        if "ffprobe" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 0, "20.0\n", "")
        codec = cmd[cmd.index("-c:v", cmd.index("-filter_complex")) + 1]
        if codec in fail_encoders:
            Path(cmd[-1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(cmd, 1, "", f"{codec} broken")
        Path(cmd[-1]).write_bytes(b"short")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_render_keeps_original_and_falls_back_to_cpu_encoder(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"horizontal")
    dst = tmp_path / "shorts" / "clip.mp4"
    log = []
    assert render_vertical("ffmpeg.exe", clip, dst, run=fake_run(fail_encoders=("h264_nvenc",), log=log)) == dst
    assert dst.read_bytes() == b"short" and clip.read_bytes() == b"horizontal"
    assert not list(dst.parent.glob("*.rendering.mp4"))
    codecs = [c[c.index("-c:v", c.index("-filter_complex")) + 1] for c, _ in log[1:]]
    assert codecs == ["h264_nvenc", "libx264"]
    assert log[1][1]["creationflags"] & getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) == \
        getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def test_render_failure_raises_and_leaves_nothing_behind(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"horizontal")
    dst = tmp_path / "shorts" / "clip.mp4"
    with pytest.raises(RuntimeError, match="libx264 broken"):
        render_vertical("ffmpeg.exe", clip, dst, run=fake_run(fail_encoders=("h264_nvenc", "libx264")))
    assert not dst.exists() and not list(dst.parent.glob("*")) and clip.read_bytes() == b"horizontal"


def test_choose_music_uses_only_audio_files(tmp_path):
    (tmp_path / "theme.mp3").write_bytes(b"")
    (tmp_path / "credits.json").write_text("{}")
    assert choose_music(str(tmp_path)) == str(tmp_path / "theme.mp3")
    assert choose_music("") == "" and choose_music(str(tmp_path / "missing")) == ""


def test_music_credit_uses_catalog_title_or_file_name(tmp_path):
    (tmp_path / "credits.json").write_text(json.dumps({"Local Forecast - Elevator.mp3": "Local Forecast - Elevator"}))
    credit = music_credit(str(tmp_path / "Local Forecast - Elevator.mp3"))
    assert credit.startswith('"Local Forecast - Elevator" Kevin MacLeod (incompetech.com)')
    assert "Creative Commons: By Attribution 4.0" in credit and "creativecommons.org/licenses/by/4.0" in credit
    assert music_credit(str(tmp_path / "Hot Pursuit.mp3")).startswith('"Hot Pursuit" Kevin MacLeod')
    assert music_credit("") == ""
    assert render.BAND == 420
