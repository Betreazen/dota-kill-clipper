import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from killclipper.naming import MatchLog
from killclipper.pipeline import CHANNEL_LIMIT_PROBE, RECONCILE_CHECKS, RECONCILE_DELAY, Pipeline, next_quota_reset

NOW = datetime(2026, 9, 13, 9, 0).timestamp()


class FakeYoutube:
    def __init__(self, fail_insert=0, fail_playlists=0, logged_in=True, quota=False, limit=False, reject=None,
                 lose_response=False):
        self.inserted, self.playlisted, self.unlisted = [], [], []
        self.fail_insert, self.fail_playlists, self.logged_in, self.quota, self.logins = \
            fail_insert, fail_playlists, logged_in, quota, 0
        self.limit, self.reject, self.lose_response = limit, reject, lose_response
        self.insert_calls = self.find_calls = 0
        self.playlist_quota = False
        self.counts = {f"v{i}": 50 for i in range(8)} | {"weak": 1}

    def authorized(self):
        return self.logged_in

    def connect(self, interactive=False):
        self.logins += 1
        self.logged_in = True
        if self.on_connect:
            self.on_connect()
        return self

    on_connect = None
    logouts = 0

    def logout(self):
        self.logouts += 1
        self.logged_in = False

    def insert(self, path, meta, publish_at):
        self.insert_calls += 1
        if self.quota:
            raise RuntimeError('HttpError 403: "reason": "quotaExceeded"')
        if self.limit:
            raise RuntimeError('ResumableUploadError: "reason": "uploadLimitExceeded"')
        if self.reject:
            reason, self.reject = self.reject, None
            raise RuntimeError(f'HttpError 400: "reason": "{reason}"')
        if self.fail_insert:
            self.fail_insert -= 1
            raise RuntimeError("connection reset")
        self.inserted.append((path, meta, publish_at))
        if self.lose_response:  # YouTube accepted the video, the answer never arrived
            self.lose_response = False
            raise OSError("response lost after success")
        return f"vid{len(self.inserted)}"

    def find_upload(self, title, publish_at):
        self.find_calls += 1
        for n, (_, meta, slot) in enumerate(self.inserted, 1):
            if meta["title"] == title and slot.isoformat() == publish_at:
                return f"vid{n}"
        return None

    def add_to_playlists(self, video_id, kills):
        if self.playlist_quota:
            raise RuntimeError('HttpError 403: "reason": "quotaExceeded"')
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


def test_login_presses_during_an_open_login_are_ignored(tmp_path):
    yt = FakeYoutube(logged_in=False)
    p, _ = make(tmp_path, yt)
    yt.on_connect = p.request_login  # user presses the button again while the browser is open
    p.request_login()
    run_all(p)
    assert yt.logins == 1


def test_logout_stops_uploads_until_next_login(tmp_path):
    yt = FakeYoutube()
    p, _ = make(tmp_path, yt)
    run_all(p)
    p.request_logout()
    run_all(p)
    assert yt.logouts == 1 and "YouTube: logged out" in p.status()
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    assert p.jobs[0]["stage"] == "upload" and not yt.inserted
    p.request_login()
    run_all(p)
    assert yt.inserted and p.jobs[0]["stage"] == "done"


def submit_clips(p, tmp_path, count):
    match = tmp_path / "match"
    match.mkdir(exist_ok=True)
    log = MatchLog(match / "match.json")
    jobs = []
    for n in range(count):
        clip = match / f"clip{n}.mp4"
        clip.write_bytes(b"horizontal")
        jobs.append(p.submit(str(clip), 1, 0, (1, 0, 0), log, log.add_clip({"path": str(clip)})))
    return jobs


def slots_file(tmp_path):
    return json.loads((tmp_path / "state" / "schedule.json").read_text())


def test_unknown_upload_is_checked_on_the_channel_then_uploaded_again_if_missing(tmp_path):
    yt = FakeYoutube(fail_insert=1)  # the connection dropped before YouTube got the video
    p, _ = make(tmp_path, yt)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    job = p.jobs[0]
    assert job["stage"] == "upload_unknown" and "connection reset" in job["error"]
    assert len(slots_file(tmp_path)) == 1  # slot kept while the outcome is unknown
    now = NOW
    for _ in range(RECONCILE_CHECKS):
        now += RECONCILE_DELAY
        run_all(p, now)
    assert yt.find_calls == RECONCILE_CHECKS and job["stage"] == "done" and len(yt.inserted) == 1
    assert slots_file(tmp_path) == [job["publish_at"]]  # the first slot was released, no leak


def test_lost_upload_response_is_found_on_the_channel_without_a_duplicate(tmp_path):
    yt = FakeYoutube(lose_response=True)
    p, _ = make(tmp_path, yt, vertical=False)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    assert p.jobs[0]["stage"] == "upload_unknown"
    restarted, _ = make(tmp_path, yt, vertical=False)  # OBS restarted meanwhile
    run_all(restarted, NOW + RECONCILE_DELAY)
    job = restarted.jobs[0]
    assert len(yt.inserted) == 1 and job["stage"] == "done" and job["video_id"] == "vid1"
    assert yt.playlisted == [("vid1", 1)]


def test_upload_interrupted_by_a_crash_is_checked_before_any_new_upload(tmp_path):
    yt = FakeYoutube()
    p, _ = make(tmp_path, yt, vertical=False)
    clip, log, idx = clip_with_log(tmp_path)
    job = p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    slot = datetime(2026, 9, 13, 10, 0, tzinfo=p.schedule.zone)
    yt.inserted.append((str(clip), job["meta"], slot))  # YouTube got it, OBS died before saving the id
    job.update(stage="uploading", publish_at=slot.isoformat())
    with p._lock:
        p._save_jobs()
    restarted, _ = make(tmp_path, yt, vertical=False)
    run_all(restarted)
    assert restarted.jobs[0]["stage"] == "done" and restarted.jobs[0]["video_id"] == "vid1"
    assert yt.insert_calls == 0


def test_api_quota_pauses_the_whole_queue_until_the_daily_reset(tmp_path):
    yt = FakeYoutube(quota=True)
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 3)
    run_all(p)
    assert yt.insert_calls == 1 and all(j["stage"] == "upload" and j["attempts"] == 0 for j in p.jobs)
    assert p.state["uploads_paused_until"] == next_quota_reset(NOW) and slots_file(tmp_path) == []
    yt.quota = False
    run_all(p, next_quota_reset(NOW) - 60)
    assert yt.insert_calls == 1
    run_all(p, next_quota_reset(NOW))
    assert [j["stage"] for j in p.jobs] == ["done"] * 3
    assert len({slot.isoformat() for _, _, slot in yt.inserted}) == 3


def test_channel_upload_limit_is_probed_hourly_with_a_single_video(tmp_path):
    yt = FakeYoutube(limit=True)
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 3)
    run_all(p)
    assert yt.insert_calls == 1 and p.state["uploads_paused_until"] == NOW + CHANNEL_LIMIT_PROBE
    assert "channel upload limit" in p.status()
    run_all(p, NOW + CHANNEL_LIMIT_PROBE)  # still limited: exactly one more probe
    assert yt.insert_calls == 2
    yt.limit = False
    run_all(p, NOW + 2 * CHANNEL_LIMIT_PROBE)
    assert [j["stage"] for j in p.jobs] == ["done"] * 3 and len(yt.inserted) == 3
    assert "paused" not in p.status()


def test_upload_pause_survives_an_obs_restart(tmp_path):
    yt = FakeYoutube(limit=True)
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 2)
    run_all(p)
    restarted, _ = make(tmp_path, yt, vertical=False)
    run_all(restarted, NOW + 60)
    assert yt.insert_calls == 1 and restarted.state["uploads_paused_until"] == NOW + CHANNEL_LIMIT_PROBE


def test_jobs_parked_by_the_old_quota_rule_are_released_on_start(tmp_path):
    yt = FakeYoutube()
    p, _ = make(tmp_path, yt, vertical=False)
    clip, log, idx = clip_with_log(tmp_path)
    job = p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    job.update(error='ResumableUploadError: "reason": "uploadLimitExceeded"', next_try=NOW + 86400)
    with p._lock:
        p._save_jobs()
    restarted, _ = make(tmp_path, yt, vertical=False)
    run_all(restarted)
    assert restarted.jobs[0]["stage"] == "done"


def test_playlist_quota_keeps_the_video_and_finishes_after_the_reset(tmp_path):
    yt = FakeYoutube()
    yt.playlist_quota = True
    p, _ = make(tmp_path, yt, vertical=False)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    job = p.jobs[0]
    assert job["video_id"] == "vid1" and job["stage"] == "upload"
    assert p.state["uploads_paused_until"] == next_quota_reset(NOW)
    yt.playlist_quota = False
    run_all(p, next_quota_reset(NOW))
    assert job["stage"] == "done" and len(yt.inserted) == 1 and yt.playlisted == [("vid1", 1)]


def test_rejected_video_fails_alone_and_frees_its_slot(tmp_path):
    yt = FakeYoutube(reject="invalidTitle")
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 2)
    run_all(p)
    assert p.jobs[0]["stage"] == "failed" and "invalidTitle" in p.jobs[0]["error"]
    assert p.jobs[1]["stage"] == "done" and slots_file(tmp_path) == [p.jobs[1]["publish_at"]]


def test_transient_playlist_errors_never_fail_an_uploaded_video(tmp_path):
    yt = FakeYoutube(fail_playlists=15)
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 1)
    now = NOW
    for _ in range(30):
        run_all(p, now)
        now += 3600
    assert p.jobs[0]["stage"] == "done" and len(yt.inserted) == 1


def test_orphan_slot_left_by_a_crash_is_released_but_owned_slots_stay(tmp_path):
    yt = FakeYoutube()
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 1)
    run_all(p)  # uploaded, publishes at 10:00
    zone = p.schedule.zone
    orphan = p.schedule.reserve(datetime(2026, 9, 13, 11, 0, tzinfo=zone))  # crash before the job was saved
    assert len(slots_file(tmp_path)) == 2
    p._release_orphan_slots(NOW)
    assert slots_file(tmp_path) == [p.jobs[0]["publish_at"]] and orphan.isoformat() not in slots_file(tmp_path)


def test_done_jobs_publishing_in_the_future_are_never_pruned(tmp_path, monkeypatch):
    import killclipper.pipeline as pipeline
    monkeypatch.setattr(pipeline, "KEEP_DONE", 1)
    yt = FakeYoutube()
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 2)
    run_all(p)
    assert [j["stage"] for j in p.jobs] == ["done", "done"]  # both still own future slots


def test_channel_lookup_errors_back_off_without_reuploading(tmp_path, caplog):
    yt = FakeYoutube(lose_response=True)
    p, _ = make(tmp_path, yt, vertical=False)
    submit_clips(p, tmp_path, 1)
    run_all(p)
    yt.find_upload = lambda title, publish_at: (_ for _ in ()).throw(RuntimeError("backend unavailable"))
    now, delays = NOW, []
    with caplog.at_level(logging.ERROR, logger="test"):
        for _ in range(8):
            now = max(now, p.jobs[0]["next_try"])
            run_all(p, now)
            delays.append(p.jobs[0]["next_try"] - now)
    job = p.jobs[0]
    assert job["stage"] == "upload_unknown" and job.get("checks", 0) == 0 and len(yt.inserted) == 1
    assert delays == sorted(delays) and max(delays) <= 3600 and "still cannot check" in caplog.text


def test_missing_short_is_rendered_again_before_upload(tmp_path):
    yt = FakeYoutube()
    p, calls = make(tmp_path, yt, youtube_enabled=False)
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    run_all(p)
    Path(p.jobs[0]["short"]).unlink()
    p.cfg["youtube_enabled"] = True
    run_all(p)
    assert len(calls) == 2 and p.jobs[0]["stage"] == "done" and len(yt.inserted) == 1


def test_reload_waits_for_old_worker_and_renders_only_once(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()
    p, calls = make(tmp_path, youtube_enabled=False)
    base = p._render
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return base(*args)
    p._render = blocked
    clip, log, idx = clip_with_log(tmp_path)
    p.submit(str(clip), 1, 0, (1, 0, 0), log, idx)
    p.start()
    assert entered.wait(5)
    p.stop()
    restarted, new_calls = make(tmp_path, youtube_enabled=False)
    restarted.start()
    release.set()
    p._thread.join(5)
    # Wait for the new owner to load the finished render, then stop it.
    import time
    deadline = time.monotonic() + 5
    while restarted.jobs[0]['stage'] == 'render' and time.monotonic() < deadline:
        time.sleep(0.02)
    restarted.stop()
    restarted._thread.join(5)
    assert len(calls) == 1 and not new_calls
    assert restarted.jobs[0]['stage'] == 'upload'


def test_daily_quota_reset_is_after_midnight_pacific():
    reset = datetime.fromtimestamp(next_quota_reset(NOW), timezone.utc)
    assert (reset.hour, reset.minute) == (8, 5) and next_quota_reset(NOW) > NOW


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
    reports = tmp_path / "state" / "reports"
    reports.mkdir(parents=True)
    (reports / "review-2023-01.json").write_text("{}")  # older than 36 months: removed
    (reports / "review-2024-01.json").write_text("{}")  # kept
    p, _ = make(tmp_path, yt)
    run_all(p)
    assert yt.unlisted == ["weak"]
    assert sorted(x.name for x in reports.iterdir()) == ["review-2024-01.json", "review-2026-09.json"]
    report = json.loads((reports / "review-2026-09.json").read_text(encoding="utf-8"))
    assert report["hide"] == ["weak"] and report["median"] == 50 and report["hidden_as"] == "unlisted"
    again, _ = make(tmp_path, yt)
    run_all(again)
    assert yt.unlisted == ["weak"]  # not repeated after an OBS restart in the same month


def test_monthly_review_skipped_until_logged_in(tmp_path):
    yt = FakeYoutube(logged_in=False)
    p, _ = make(tmp_path, yt)
    run_all(p)
    assert not yt.unlisted and not (tmp_path / "state" / "reports").exists()
