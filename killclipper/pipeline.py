"""Background media pipeline: vertical render -> YouTube upload into a publication slot, monthly review.

One worker thread does the slow work in order: renders never overlap (the game keeps its CPU/GPU)
and the YouTube client is used from a single thread. Jobs persist in <state_dir>/jobs.json and
resume after an OBS restart. submit() runs on the OBS main thread and prepares the text right away.
"""
import json
import hashlib
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .naming import MatchLog
from .locking import FileLock
from .publishing import Schedule, metadata, review, write_atomic
from .render import choose_music, music_credit, render_vertical

TIMEZONE = "Europe/Minsk"
RENDER_ATTEMPTS, RENDER_RETRY = 3, 60
UPLOAD_RETRY, UPLOAD_RETRY_MAX = 300, 3600    # transient upload errors: retried forever with this backoff
RECONCILE_DELAY, RECONCILE_CHECKS = 600, 3    # unknown upload result: look for the video on the channel first
RECONCILE_ERROR_ALERT = 3                     # lookups failing this many times in a row are logged as errors
CHANNEL_LIMIT_PROBE = 3600                    # channel upload limit is a rolling 24 h window: probe hourly
RATE_LIMIT_PAUSE, AUTH_PAUSE, SLOT_RETRY = 900, 600, 300
REVIEW_RETRY = 3600
REPORT_KEEP_DAYS = 36 * 30
IDLE_WAIT = 5.0
KEEP_DONE = 100
# YouTube answers that pause the whole upload queue (renders keep going)
PAUSE_REASONS = {"quotaExceeded": "quota", "dailyLimitExceeded": "quota", "uploadLimitExceeded": "channel_limit",
                 "rateLimitExceeded": "rate", "userRateLimitExceeded": "rate", "NotLoggedIn": "auth",
                 "invalid_grant": "auth", "RefreshError": "auth", "HttpError 401": "auth"}
PAUSE_TEXT = {"quota": "YouTube API daily quota", "channel_limit": "channel upload limit",
              "rate": "YouTube rate limit", "auth": "YouTube login needed"}
# answers that mean YouTube refused this particular video; retrying the same request cannot help
REJECTED_REASONS = ("invalidTitle", "invalidDescription", "invalidTags", "invalidCategoryId", "invalidFilename",
                    "invalidVideoMetadata", "defaultLanguageNotSet", "mediaBodyRequired", "forbiddenLicenseSetting",
                    "forbiddenPrivacySetting", "invalidRecordingDetails")


def classify(error: str) -> str | None:
    """quota / channel_limit / rate / auth pause the queue, rejected / slot concern one video, None = unknown."""
    for reason, kind in PAUSE_REASONS.items():
        if reason in error:
            return kind
    if any(reason in error for reason in REJECTED_REASONS):
        return "rejected"
    if "invalidPublishAt" in error:
        return "slot"
    return None


def next_quota_reset(now: float) -> float:
    """YouTube quotas reset at midnight Pacific time; 08:05 UTC is past it in both PST and PDT."""
    t = datetime.fromtimestamp(now, timezone.utc)
    reset = t.replace(hour=8, minute=5, second=0, microsecond=0)
    return (reset if reset > t else reset + timedelta(days=1)).timestamp()


class Pipeline:
    def __init__(self, cfg: dict, state_dir, log, youtube=None, render=render_vertical, clock=time.time, rng=random):
        self.cfg, self.log, self.youtube = cfg, log, youtube
        self.state_dir = Path(state_dir)
        self._render, self._clock, self._rng = render, clock, rng
        self._lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._login_requested = self._logout_requested = False
        self._review_retry_at = 0.0
        self._logs: dict[str, MatchLog] = {}
        self.jobs: list[dict] = self._load("jobs.json", [])
        self.state: dict = self._load("state.json", {})
        self.schedule = Schedule(self.state_dir / "schedule.json", TIMEZONE)
        self.youtube_state = "not checked"
        self._thread = None
        self._recover_uploads()

    def _recover_uploads(self):
        """Interrupted uploads are looked up on the channel; jobs parked by the old per-job quota rule are released."""
        for job in self.jobs:
            if job['stage'] == 'uploading':
                job.update(stage='upload_unknown', next_try=0, checks=0,
                           error='Upload interrupted; checking the channel before uploading again')
            elif job['stage'] == 'upload' and job.get('error') and classify(job['error']) in PAUSE_TEXT:
                job['next_try'] = 0

    # ---- OBS main thread ------------------------------------------------------------
    def submit(self, clip: str, kills: int, assists: int, kda, match_log: MatchLog | None = None,
               clip_idx: int | None = None) -> dict:
        vertical = self.cfg.get("vertical") or {}
        music = choose_music(vertical.get("music_dir", ""), self._rng) if vertical.get("enabled") else ""
        if match_log is not None:
            self._logs[str(match_log.path)] = match_log
        # "upload" jobs wait until YouTube is enabled and logged in, so nothing rendered is ever skipped
        job = {"id": f"{int(self._clock() * 1000)}-{len(self.jobs)}", "clip": str(clip), "short": None,
               "kills": kills, "assists": assists, "kda": list(kda), "music": music,
               "meta": metadata(kills, assists, kda, self.cfg.get("channel", ""), music_credit(music), rng=self._rng),
               "match_json": str(match_log.path) if match_log is not None else None, "clip_idx": clip_idx,
               "stage": "render" if vertical.get("enabled") else "upload",
               "attempts": 0, "next_try": 0, "video_id": None, "publish_at": None, "error": None}
        with self._lock:
            for existing in self.jobs:
                if existing['clip'] == str(clip):
                    return existing
            key = hashlib.sha256(str(clip).encode()).hexdigest()
            self._save(f'inbox/{key}.json', job)
            if self._thread is None:
                self.jobs.append(job)
                self._save_jobs()
        self._wake.set()
        return job

    def request_login(self) -> None:
        self._login_requested = True
        self._wake.set()

    def request_logout(self) -> None:
        self._logout_requested = True
        self._wake.set()

    def status(self) -> str:
        with self._lock:
            stages = Counter(j["stage"] for j in self.jobs)
        youtube = self.youtube_state if self.youtube and self.cfg.get("youtube_enabled") else "off"
        paused = self.state.get("uploads_paused_until", 0)
        pause = (f" | uploads paused until {datetime.fromtimestamp(paused):%d.%m %H:%M}: "
                 f"{self.state.get('uploads_pause_reason')}" if paused > self._clock() else "")
        return (f"Shorts: render {stages['render']}, upload {stages['upload']}, "
                f"checking {stages['upload_unknown']}, failed {stages['failed']} | YouTube: {youtube}{pause}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_owned, name="killclipper-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run_owned(self):
        ownership = FileLock(self.state_dir / 'pipeline.lock')
        while not ownership.acquire():
            if self._stop.wait(0.1):
                return
        try:
            with self._lock:
                self.jobs = self._load('jobs.json', [])
                self.state = self._load('state.json', {})
                self._recover_uploads()
                self._release_orphan_slots(self._clock())
            self._run()
        finally:
            ownership.release()

    # ---- worker thread ----------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.process_once()
            except Exception:
                self.log.exception("Pipeline step failed")
                busy = False
            if not busy:
                self._wake.wait(IDLE_WAIT)
                self._wake.clear()

    def process_once(self, now: float | None = None) -> bool:
        """Do one unit of work. Returns False when nothing is due."""
        if self._stop.is_set():
            return False
        with self._lock:
            inbox = list((self.state_dir / 'inbox').glob('*.json'))
            known = {j['clip'] for j in self.jobs}
            for path in inbox:
                job = json.loads(path.read_text(encoding='utf-8'))
                if job['clip'] not in known:
                    self.jobs.append(job)
                    known.add(job['clip'])
            if inbox:
                self._save_jobs()
                for path in inbox:
                    path.unlink()
        now = self._clock() if now is None else now
        if self._logout_requested and self.youtube:
            self._logout_requested = self._login_requested = False
            self._logout()
            return True
        if self._login_requested and self.youtube:
            self._login()
            self._login_requested = False  # presses while the browser was open are dropped
            return True
        if self._review_due(now):
            self._review(now)
            return True
        job = self._next_job(now)
        if job is None:
            return False
        if job["stage"] == "render":
            self._do_render(job, now)
        elif job["stage"] == "upload_unknown":
            self._do_reconcile(job, now)
        else:
            self._do_upload(job, now)
        with self._lock:
            self._save_jobs()
        return True

    def _youtube_ready(self) -> bool:
        if not (self.youtube and self.cfg.get("youtube_enabled")):
            return False
        ok = self.youtube.authorized()
        self.youtube_state = "logged in" if ok else "not logged in (press 'Log in to YouTube')"
        return ok

    def _login(self) -> None:
        try:
            self.youtube.connect(interactive=True)
            self.youtube_state = "logged in"
            self.log.info("YouTube: logged in to %s", getattr(self.youtube, "channel_title", "") or "channel")
        except Exception as e:
            self.youtube_state = f"login failed: {e}"
            self.log.error("YouTube login failed: %s", e)

    def _logout(self) -> None:
        try:
            self.youtube.logout()
            self.log.info("YouTube: logged out, access revoked at Google")
        except Exception as e:
            self.log.warning("YouTube: logged out locally, revoking at Google failed: %s", e)
        self.youtube_state = "logged out (press 'Log in to YouTube')"

    def _next_job(self, now: float) -> dict | None:
        with self._lock:
            jobs = list(self.jobs)
        ready = None
        uploads_open = now >= self.state.get("uploads_paused_until", 0)
        for job in jobs:
            if job["next_try"] > now:
                continue
            if job["stage"] == "render":
                return job
            if job["stage"] in ("upload", "upload_unknown") and uploads_open:
                if ready is None:
                    ready = self._youtube_ready()
                if ready:
                    return job
        return None

    def _do_render(self, job: dict, now: float) -> None:
        v = self.cfg.get("vertical") or {}
        clip = Path(job["clip"])
        dst = clip.parent / "shorts" / clip.name
        try:
            self._render(self.cfg["ffmpeg"], clip, dst, v.get("upper_cta", ""), v.get("lower_cta", ""),
                         job["music"], v.get("music_volume", 0.15))
        except Exception as e:
            self._retry_or_fail(job, now, e, "Render", RENDER_ATTEMPTS, RENDER_RETRY)
            return
        job.update(short=str(dst), stage="upload", attempts=0, error=None)
        self._update_match(job, short=str(dst))
        self.log.info("Short rendered: %s", dst)

    def _do_upload(self, job: dict, now: float) -> None:
        """Upload into a reserved slot, then add to playlists. No outcome ever drops or duplicates the video."""
        path = Path(job["short"] or job["clip"])
        if not job["video_id"]:
            if not path.exists():
                self._file_missing(job, path)
                return
            slot = self.schedule.reserve(datetime.fromtimestamp(now, self.schedule.zone))
            job.update(stage="uploading", publish_at=slot.isoformat())
            with self._lock:
                self._save_jobs()  # a crash from here on is looked up on the channel, never blindly re-uploaded
            try:
                video_id = self.youtube.insert(str(path), job["meta"], slot)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                kind = classify(error)
                if kind is None:  # the video may or may not have reached YouTube
                    job.update(stage="upload_unknown", error=error, checks=0, next_try=now + RECONCILE_DELAY)
                    self.log.warning("Upload result unknown for %s; checking the channel in %d min before retrying: %s",
                                     path.name, RECONCILE_DELAY // 60, error)
                    return
                # YouTube refused the request, so nothing was uploaded
                self.schedule.release(slot, datetime.fromtimestamp(now, self.schedule.zone))
                job.update(stage="upload", publish_at=None, error=error)
                if kind == "rejected":
                    job["stage"] = "failed"
                    self.log.error("YouTube rejected %s, it stays on disk and out of the queue: %s", path.name, error)
                elif kind == "slot":
                    job["next_try"] = now + SLOT_RETRY
                else:
                    self._pause(kind, now)
                return
            job.update(video_id=video_id, stage="upload", publish_at=slot.isoformat(), error=None)
            with self._lock:
                self._save_jobs()  # retries after this point only repair playlist membership
            self._resume_uploads()
            self._update_match(job, youtube_id=video_id, publish_at=slot.isoformat())
            self.log.info("YouTube: %s uploaded as %s, publishes %s", path.name, video_id, slot.isoformat())
        try:
            self.youtube.add_to_playlists(job["video_id"], job["kills"])
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            kind = classify(error)
            job["error"] = error
            if kind in PAUSE_TEXT:
                self._pause(kind, now)
            else:
                self._retry_later(job, now, error)
            return
        self._resume_uploads()
        job.update(stage="done", attempts=0, error=None, next_try=0)

    def _do_reconcile(self, job: dict, now: float) -> None:
        """Unknown upload result: find the video on the channel before ever uploading it again."""
        try:
            found = self.youtube.find_upload(job["meta"]["title"], job["publish_at"]) if job["publish_at"] else None
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            job["error"] = error
            kind = classify(error)
            if kind in PAUSE_TEXT:
                self._pause(kind, now)
                return
            # never re-upload on a failed lookup: that could duplicate the video; back off and keep checking
            job["reconcile_errors"] = job.get("reconcile_errors", 0) + 1
            job["next_try"] = now + min(RECONCILE_DELAY * job["reconcile_errors"], UPLOAD_RETRY_MAX)
            level = self.log.error if job["reconcile_errors"] >= RECONCILE_ERROR_ALERT else self.log.warning
            level("YouTube: still cannot check the channel for %s (%d tries); it waits, neither lost nor re-uploaded: %s",
                  Path(job["short"] or job["clip"]).name, job["reconcile_errors"], error)
            return
        job.pop("reconcile_errors", None)
        if found:
            job.update(stage="upload", video_id=found, checks=0, next_try=0, error=None)
            self._update_match(job, youtube_id=found, publish_at=job["publish_at"])
            self.log.info("YouTube: upload with unknown result found on the channel as %s, not uploading again", found)
            return
        job["checks"] = job.get("checks", 0) + 1
        if job["checks"] < RECONCILE_CHECKS:
            job["next_try"] = now + RECONCILE_DELAY
            return
        if job["publish_at"]:
            self.schedule.release(datetime.fromisoformat(job["publish_at"]),
                                  datetime.fromtimestamp(now, self.schedule.zone))
        job.update(stage="upload", publish_at=None, checks=0, next_try=0,
                   error="Not found on the channel after an unknown upload result; uploading again")
        self.log.warning("YouTube: %s is not on the channel after %d checks, uploading it again",
                         Path(job["short"] or job["clip"]).name, RECONCILE_CHECKS)

    def _release_orphan_slots(self, now: float) -> None:
        """A crash between reserving a slot and saving the job leaves a slot no video owns: free future ones."""
        owned = {job["publish_at"] for job in self.jobs if job.get("publish_at")}
        current = datetime.fromtimestamp(now, self.schedule.zone)
        for slot in sorted(self.schedule._read()):
            when = datetime.fromisoformat(slot)
            if slot not in owned and when > current:
                self.schedule.release(when, current)
                self.log.warning("Released publication slot %s that no queued video owns", slot)

    def _file_missing(self, job: dict, path: Path) -> None:
        if job["short"] and Path(job["clip"]).exists():
            job.update(stage="render", short=None, error=f"Short missing, rendering again: {path}")
            self.log.warning("Short %s is missing, rendering it again", path.name)
        else:
            job.update(stage="failed", error=f"Clip file missing: {path}")
            self.log.error("Cannot upload %s: the clip file is missing", path)

    def _pause(self, kind: str, now: float) -> None:
        until = {"quota": next_quota_reset(now), "channel_limit": now + CHANNEL_LIMIT_PROBE,
                 "rate": now + RATE_LIMIT_PAUSE, "auth": now + AUTH_PAUSE}[kind]
        self.state.update(uploads_paused_until=until, uploads_pause_reason=PAUSE_TEXT[kind])
        self._save("state.json", self.state)
        self.log.warning("YouTube uploads paused until %s (%s); queued videos wait, nothing is lost",
                         datetime.fromtimestamp(until).strftime("%d.%m %H:%M"), PAUSE_TEXT[kind])

    def _resume_uploads(self) -> None:
        if self.state.pop("uploads_paused_until", None) is not None:
            self.state.pop("uploads_pause_reason", None)
            self._save("state.json", self.state)

    def _retry_later(self, job: dict, now: float, error: str) -> None:
        job["attempts"] += 1
        job["error"] = error
        job["next_try"] = now + min(UPLOAD_RETRY * job["attempts"], UPLOAD_RETRY_MAX)
        self.log.warning("Upload step failed (attempt %d), retry in %d min: %s", job["attempts"],
                         (job["next_try"] - now) // 60, error)

    def _retry_or_fail(self, job, now, error, what, attempts, delay, cap=None) -> None:
        job["attempts"] += 1
        job["error"] = f"{type(error).__name__}: {error}"
        if job["attempts"] >= attempts:
            job["stage"] = "failed"
            self.log.error("%s failed %d times, giving up on %s: %s", what, job["attempts"], job["clip"], job["error"])
            return
        wait = delay * job["attempts"]
        job["next_try"] = now + (min(wait, cap) if cap else wait)
        self.log.warning("%s failed (attempt %d), retry in %.0f s: %s", what, job["attempts"], job["next_try"] - now,
                         job["error"])

    def _update_match(self, job: dict, **fields) -> None:
        if not job["match_json"] or job["clip_idx"] is None:
            return
        try:
            log = self._logs.get(job["match_json"]) or self._logs.setdefault(job["match_json"],
                                                                             MatchLog(Path(job["match_json"])))
            log.update_clip(job["clip_idx"], **fields)
        except (OSError, IndexError, KeyError) as e:
            self.log.warning("match.json not updated (%s): %s", job["match_json"], e)

    # ---- monthly review ---------------------------------------------------------------
    def _review_due(self, now: float) -> bool:
        if now < self._review_retry_at:
            return False
        if self.state.get("last_review") == datetime.fromtimestamp(now).strftime("%Y-%m"):
            return False
        return self._youtube_ready()

    def _review(self, now: float) -> None:
        month = datetime.fromtimestamp(now).strftime("%Y-%m")
        try:
            report = review(self.youtube.review_counts())
            self.youtube.unlist(report["hide"])
        except Exception as e:
            self._review_retry_at = now + REVIEW_RETRY
            self.log.error("YouTube monthly review failed, retry in 1 h: %s", e)
            return
        report.update(month=month, created=datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                      hidden_as="unlisted")
        self._save(f"reports/review-{month}.json", report)
        # YouTube API data storage terms: statistics kept at most 36 months
        oldest = f"review-{(datetime.fromtimestamp(now) - timedelta(days=REPORT_KEEP_DAYS)).strftime('%Y-%m')}.json"
        for old in (self.state_dir / "reports").glob("review-*.json"):
            if old.name < oldest:
                old.unlink(missing_ok=True)
        self.state["last_review"] = month
        self._save("state.json", self.state)
        self.log.info("YouTube monthly review %s: %d videos, median %s, %d set to unlisted",
                      month, report["videos"], report["median"], len(report["hide"]))

    # ---- persistence ------------------------------------------------------------------
    def _load(self, name: str, default):
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def _save(self, name: str, data) -> None:
        write_atomic(self.state_dir / name, json.dumps(data, ensure_ascii=False, indent=1))

    def _save_jobs(self) -> None:  # caller holds self._lock
        now = datetime.fromtimestamp(self._clock(), self.schedule.zone)
        # a done job still owns its future slot; pruning it would let another video take that slot
        done = [j for j in self.jobs if j["stage"] == "done"
                and (not j.get("publish_at") or datetime.fromisoformat(j["publish_at"]) <= now)]
        stale = {id(j) for j in (done[:-KEEP_DONE] if KEEP_DONE else done)}
        self.jobs[:] = [j for j in self.jobs if id(j) not in stale]
        self._save("jobs.json", self.jobs)
