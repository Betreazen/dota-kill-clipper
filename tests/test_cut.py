import subprocess
from pathlib import Path

import pytest

from killclipper.cut import clip_bounds, cut_clip, ffmpeg_cmd, ffprobe_path, find_ffmpeg, probe_duration


def test_clip_bounds_inside_buffer():
    # buffer saved at t=1030, holds 120 s -> file covers [910, 1030]
    start, end, truncated = clip_bounds(saved_at=1030.0, duration=120.0, start_ts=990.0, end_ts=1010.0)
    assert (start, end, truncated) == (80.0, 100.0, False)


def test_clip_bounds_truncated_at_buffer_start():
    start, end, truncated = clip_bounds(saved_at=1030.0, duration=30.0, start_ts=990.0, end_ts=1028.0)
    assert (start, end, truncated) == (0.0, 28.0, True)


def test_clip_bounds_end_clamped_to_duration_is_truncated():
    start, end, truncated = clip_bounds(saved_at=1000.0, duration=100.0, start_ts=950.0, end_ts=1005.0)
    assert (start, end, truncated) == (50.0, 100.0, True)


def test_ffmpeg_cmd():
    cmd = ffmpeg_cmd("ffmpeg.exe", Path("in.mp4"), 80.0, 100.5, Path("out.mp4"))
    assert cmd == ["ffmpeg.exe", "-y", "-loglevel", "error", "-ss", "80.000", "-to", "100.500",
                   "-i", "in.mp4", "-c", "copy", "out.mp4"]


def test_ffprobe_path_next_to_ffmpeg():
    assert ffprobe_path(r"C:\ffmpeg\bin\ffmpeg.exe") == r"C:\ffmpeg\bin\ffprobe.exe"
    assert ffprobe_path("ffmpeg") == "ffprobe"


def test_find_ffmpeg_prefers_configured_then_path_then_default(tmp_path, monkeypatch):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"")
    assert find_ffmpeg(str(exe)) == str(exe)
    monkeypatch.setattr("killclipper.cut.shutil.which", lambda name: None)
    monkeypatch.setattr("killclipper.cut.DEFAULT_FFMPEG", str(tmp_path / "missing.exe"))
    assert find_ffmpeg(str(tmp_path / "nope.exe")) is None
    monkeypatch.setattr("killclipper.cut.shutil.which", lambda name: "C:/x/ffmpeg.exe")
    assert find_ffmpeg("") == "C:/x/ffmpeg.exe"


def fake_run_factory(duration="120.0", ffmpeg_rc=0, log=None):
    def run(cmd, **kw):
        if log is not None:
            log.append(cmd)
        if "ffprobe" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 0, stdout=duration + "\n", stderr="")
        if ffmpeg_rc == 0:
            Path(cmd[-1]).write_bytes(b"clip")
        return subprocess.CompletedProcess(cmd, ffmpeg_rc, stdout="", stderr="boom" if ffmpeg_rc else "")
    return run


def test_probe_duration_parses_output():
    assert probe_duration("ffmpeg.exe", Path("x.mp4"), run=fake_run_factory("119.98")) == pytest.approx(119.98)


def test_cut_clip_success_removes_source(tmp_path):
    src = tmp_path / "Replay 2026-09-09.mp4"
    src.write_bytes(b"raw")
    dst = tmp_path / "match" / "[00.23.27-00.23.47] 1 kill (KDA 3-1-2).mp4"
    log = []
    res = cut_clip("ffmpeg.exe", src, dst, start_ts=990.0, end_ts=1010.0, saved_at=1030.0,
                   run=fake_run_factory("120.0", log=log))
    assert res["ok"] is True
    assert res["path"] == str(dst)
    assert (res["start"], res["end"], res["truncated"]) == (80.0, 100.0, False)
    assert dst.read_bytes() == b"clip"
    assert not src.exists()
    assert log[1][0] == "ffmpeg.exe" and log[1][-1] == str(dst)


def test_cut_clip_failure_keeps_raw_in_match_dir(tmp_path):
    src = tmp_path / "Replay.mp4"
    src.write_bytes(b"raw")
    dst = tmp_path / "match" / "[00.00.10-00.00.30] 1 kill (KDA 1-0-0).mp4"
    res = cut_clip("ffmpeg.exe", src, dst, start_ts=990.0, end_ts=1010.0, saved_at=1030.0,
                   run=fake_run_factory("120.0", ffmpeg_rc=1))
    assert res["ok"] is False and "boom" in res["error"]
    raw = tmp_path / "match" / "[00.00.10-00.00.30] 1 kill (KDA 1-0-0)_raw.mp4"
    assert raw.read_bytes() == b"raw" and not src.exists()
    assert res["path"] == str(raw)


def test_cut_clip_ffmpeg_missing_keeps_raw(tmp_path):
    src = tmp_path / "Replay.mp4"
    src.write_bytes(b"raw")
    dst = tmp_path / "match" / "clip.mp4"

    def run(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    res = cut_clip(r"C:\nope\ffmpeg.exe", src, dst, start_ts=0.0, end_ts=20.0, saved_at=30.0, run=run)
    assert res["ok"] is False and "FileNotFoundError" in res["error"]
    assert (tmp_path / "match" / "clip_raw.mp4").exists()


def test_cut_clip_reports_truncation(tmp_path):
    src = tmp_path / "Replay.mp4"
    src.write_bytes(b"raw")
    dst = tmp_path / "m" / "c.mp4"
    res = cut_clip("ffmpeg.exe", src, dst, start_ts=990.0, end_ts=1010.0, saved_at=1030.0,
                   run=fake_run_factory("25.0"))
    assert res["ok"] and res["truncated"] and res["start"] == 0.0 and res["end"] == 5.0
