import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from killclipper.naming import MatchLog
from killclipper.pipeline import Pipeline, next_quota_reset

NOW = datetime(2026, 9, 13, 9, 0).timestamp()


class FakeYoutube:
    def __init__(self, fail_insert=0, fail_playlists=0, logged_in=True, quota=False):
        self.inserted, self.playlisted, self.unlisted = [], [], []
        self.fail_insert, self.fail_playlists, self.logged_in, self.quota, self.logins = \
            fail_insert, fail_playlists, logged_in, quota, 0
        self.counts = {f"v{i}": 50 for i in range(8)} | {"weak": 1}

    def authorized(self):
        return self.logged_in

    def connect(self, interactive=False):
        self.logins += 1
        self.logged_in = True
        return self

    def insert(self, path, meta, publish_at):
        if self.quota:
            raise RuntimeError('HttpError 403: "reason": "quotaExceeded"')
        if self.fail_insert:
            self.fail_insert -= 1
            raise RuntimeError("connection reset")
        self.inserted.append((path, meta, publish_at))
        return f"vid{len(self.inserted)}"

    def add_to_playlists(self, video_id, kills):
        if self.fail_playlists:
            self.fail_playlists -= 1
            raise RuntimeError("backend error")
        self.playlisted.append((video_id, kills))

    def review_counts(self, now=None):
        return dict(self.counts)

    def unlist(self, ids):
        self.unlisted.extend(ids)


def fake_render(calls, fail=False):
    def render(ffmpeg, clip, dst, upper_cta="", lower_cta="", music="", music_volume=0.15):
        calls.append(dict(clip=clip, dst=dst, upper=upper_cta, lower=lower_cta, music=music, volume=music_volume))
        if fail:
            raise RuntimeError("encoder died")
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"short")
        return Path(dst)
    return render


def make(tmp_path, youtube=None, vertical=True, youtube_enabled=True, render_fail=False):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    (music / "Hot Pursuit.mp3").write_bytes(b"")
    cfg = {"ffmpeg": "ffmpeg.exe", "youtube_enabled": youtube_enabled, "channel": "Betreazen highlights Dota 2",
           "vertical": {"enabled": vertical, "upper_cta": "top.mov", "lower_cta": "bottom.mov",
                        "music_dir": str(music), "music_volume": 0.5}}
    calls = []
    p = Pipeline(cfg, tmp_path / "state", logging.getLogger("test"), youtube=youtube,
                 render=fake_render(calls, render_fail), clock=lambda: NOW)
    return p, calls


def clip_with_log(tmp_path):
    match = tmp_path / "match"
    match.mkdir(exist_ok=True)
    clip = match / "[00.01.00-00.01.20] 2 kills (KDA 2-0-0).mp4"
    clip.write_bytes(b"horizontal")
    log = MatchLog(match / "match.json")
    idx = log.add_clip({"path": str(clip), "status": "ok"})
    return clip, log, idx


def run_all(p, now=NOW):
    while p.process_once(now):
        pass


def test_clip_rendered_to_shorts_folder_then_uploaded_with_prepared_metadata(tmp_path):
    yt = FakeYoutube()
    p, calls = make(tmp_path, yt)
    clip, log, idx = clip_with_log(tmp_path)
    job = p.submit(str(clip), 2, 0, (2, 0, 0), log, idx)
    assert job["meta"]["title"] and "Kevin MacLeod" in job["meta"]["description"]  # text ready before render
    run_all(p)
    short = clip.parent / "shorts" / clip.name
    assert calls[0]["dst"] == short and calls[0]["music"].endswith("Hot Pursuit.mp3")
    assert calls[0]["upper"] == "top.mov" and calls[0]["volume"] == 0.5
    path, meta, slot = yt.inserted[0]
    assert path == str(short) and meta == job["meta"] and (slot.hour, slot.minute) == (10, 0)
    assert yt.playlisted == [("vid1", 2)]
    assert clip.read_bytes() == b"horizontal"
    saved = json.loads(log.path.read_text(encoding="utf-8"))["clips"][idx]
    assert saved["short"] == path and saved["youtube_id"] == "vid1" and saved["publish_at"].startswith("2026-09-13T10:00")
    assert json.loads((tmp_path / "state" / "jobs.json").read_text(encoding="utf-8"))[0]["stage"] == "done"


def test_vertical_disabled_uploads_horizontal_clip(tmp_path):
    yt = FakeYoutube()
    p, calls = make(tmp_path, yt, vertical=False)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    assert not calls and yt.inserted[0][0] == str(clip)
    assert "Kevin MacLeod" not in yt.inserted[0][1]["description"]


def test_shorts_rendered_while_youtube_is_off_upload_once_it_is_enabled(tmp_path):
    yt = FakeYoutube()
    p, calls = make(tmp_path, yt, youtube_enabled=False)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    assert len(calls) == 1 and p.jobs[0]["stage"] == "upload" and not yt.inserted
    assert "YouTube: off" in p.status()
    p.cfg["youtube_enabled"] = True
    run_all(p)
    assert yt.inserted and p.jobs[0]["stage"] == "done"


def test_upload_waits_for_login_and_survives_restart(tmp_path):
    yt = FakeYoutube(logged_in=False)
    p, _ = make(tmp_path, yt)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    assert p.jobs[0]["stage"] == "upload" and not yt.inserted
    assert "not logged in" in p.status().lower()
    restarted, _ = make(tmp_path, yt)
    restarted.request_login()
    run_all(restarted)
    assert yt.logins == 1 and yt.inserted and restarted.jobs[0]["stage"] == "done"


def test_failed_upload_releases_slot_and_retries_later(tmp_path):
    yt = FakeYoutube(fail_insert=1)
    p, _ = make(tmp_path, yt)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    job = p.jobs[0]
    assert job["stage"] == "upload" and job["attempts"] == 1 and "connection reset" in job["error"]
    assert json.loads((tmp_path / "state" / "schedule.json").read_text()) == []
    run_all(p, now=job["next_try"])
    assert job["stage"] == "done" and yt.inserted[0][2].hour == 10


def test_quota_error_waits_for_daily_reset_without_using_attempts(tmp_path):
    yt = FakeYoutube(quota=True)
    p, _ = make(tmp_path, yt)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    job = p.jobs[0]
    assert job["stage"] == "upload" and job["attempts"] == 0 and job["next_try"] == next_quota_reset(NOW)
    reset = datetime.fromtimestamp(job["next_try"], timezone.utc)
    assert (reset.hour, reset.minute) == (8, 5) and job["next_try"] > NOW


def test_playlist_failure_after_insert_never_uploads_the_video_twice(tmp_path):
    yt = FakeYoutube(fail_playlists=1)
    p, _ = make(tmp_path, yt)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 3, 0, (3, 0, 0), log, idx)
    run_all(p)
    stored = json.loads((tmp_path / "state" / "jobs.json").read_text(encoding="utf-8"))[0]
    assert stored["video_id"] == "vid1" and stored["stage"] == "upload"  # id persisted before playlists
    restarted, _ = make(tmp_path, yt)  # e.g. OBS closed right after the failure
    run_all(restarted, now=stored["next_try"])
    assert len(yt.inserted) == 1 and yt.playlisted == [("vid1", 3)] and restarted.jobs[0]["stage"] == "done"


def test_render_failure_keeps_horizontal_clip_and_gives_up_after_three_attempts(tmp_path):
    yt = FakeYoutube()
    p, calls = make(tmp_path, yt, render_fail=True)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    now = NOW
    for _ in range(5):
        run_all(p, now)
        now += 3600
    assert len(calls) == 3 and p.jobs[0]["stage"] == "failed" and not yt.inserted
    assert clip.read_bytes() == b"horizontal"


def test_monthly_review_runs_once_per_month_and_writes_report(tmp_path):
    yt = FakeYoutube()
    p, _ = make(tmp_path, yt)
    run_all(p)
    assert yt.unlisted == ["weak"]
    report = json.loads((tmp_path / "state" / "reports" / "review-2026-09.json").read_text(encoding="utf-8"))
    assert report["hide"] == ["weak"] and report["median"] == 50 and report["hidden_as"] == "unlisted"
    again, _ = make(tmp_path, yt)
    run_all(again)
    assert yt.unlisted == ["weak"]  # not repeated after an OBS restart in the same month


def test_monthly_review_skipped_until_logged_in(tmp_path):
    yt = FakeYoutube(logged_in=False)
    p, _ = make(tmp_path, yt)
    run_all(p)
    assert not yt.unlisted and not (tmp_path / "state" / "reports").exists()
