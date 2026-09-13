"""Background media pipeline: vertical render -> YouTube upload into a publication slot, monthly review.

One worker thread does the slow work in order: renders never overlap (the game keeps its CPU/GPU)
and the YouTube client is used from a single thread. Jobs persist in <state_dir>/jobs.json and
resume after an OBS restart. submit() runs on the OBS main thread and prepares the text right away.
"""
import json
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .naming import MatchLog
from .publishing import Schedule, metadata, review, write_atomic
from .render import choose_music, music_credit, render_vertical

TIMEZONE = "Europe/Minsk"
RENDER_ATTEMPTS, RENDER_RETRY = 3, 60
UPLOAD_ATTEMPTS, UPLOAD_RETRY, UPLOAD_RETRY_MAX = 10, 300, 3600
REVIEW_RETRY = 3600
IDLE_WAIT = 5.0
KEEP_DONE = 100
QUOTA_REASONS = ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded")


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
        self._login_requested = False
        self._review_retry_at = 0.0
        self._logs: dict[str, MatchLog] = {}
        self.jobs: list[dict] = self._load("jobs.json", [])
        self.state: dict = self._load("state.json", {})
        self.schedule = Schedule(self.state_dir / "schedule.json", TIMEZONE)
        self.youtube_state = "not checked"

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
            self.jobs.append(job)
            self._save_jobs()
        self._wake.set()
        return job

    def request_login(self) -> None:
        self._login_requested = True
        self._wake.set()

    def status(self) -> str:
        with self._lock:
            stages = Counter(j["stage"] for j in self.jobs)
        youtube = self.youtube_state if self.youtube and self.cfg.get("youtube_enabled") else "off"
        return (f"Shorts: render {stages['render']}, upload {stages['upload']}, failed {stages['failed']} | "
                f"YouTube: {youtube}")

    def start(self) -> None:
        threading.Thread(target=self._run, name="killclipper-pipeline", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

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
        now = self._clock() if now is None else now
        if self._login_requested and self.youtube:
            self._login_requested = False
            self._login()
            return True
        if self._review_due(now):
            self._review(now)
            return True
        job = self._next_job(now)
        if job is None:
            return False
        if job["stage"] == "render":
            self._do_render(job, now)
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

    def _next_job(self, now: float) -> dict | None:
        with self._lock:
            jobs = list(self.jobs)
        ready = None
        for job in jobs:
            if job["next_try"] > now:
                continue
            if job["stage"] == "render":
                return job
            if job["stage"] == "upload":
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
        path = job["short"] or job["clip"]
        try:
            if not job["video_id"]:
                slot = self.schedule.reserve(datetime.fromtimestamp(now, self.schedule.zone))
                try:
                    video_id = self.youtube.insert(path, job["meta"], slot)
                except Exception:
                    self.schedule.release(slot)
                    raise
                job.update(video_id=video_id, publish_at=slot.isoformat())
                with self._lock:
                    self._save_jobs()  # persisted before anything else can fail: a clip is never uploaded twice
                self._update_match(job, youtube_id=video_id, publish_at=slot.isoformat())
                self.log.info("YouTube: %s uploaded as %s, publishes %s", Path(path).name, video_id, slot.isoformat())
            self.youtube.add_to_playlists(job["video_id"], job["kills"])
        except Exception as e:
            if any(reason in str(e) for reason in QUOTA_REASONS):
                job.update(next_try=next_quota_reset(now), error=f"{type(e).__name__}: {e}")
                self.log.warning("YouTube quota exhausted, uploads resume after the daily reset")
                return
            self._retry_or_fail(job, now, e, "Upload", UPLOAD_ATTEMPTS, UPLOAD_RETRY, UPLOAD_RETRY_MAX)
            return
        job.update(stage="done", attempts=0, error=None)

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
        self.state["last_review"] = month
        self._save("state.json", self.state)
        self.log.info("YouTube monthly review %s: %d videos, median %s, %d set to unlisted",
                      month, report["videos"], report["median"], len(report["hide"]))

    # ---- persistence ------------------------------------------------------------------
    def _load(self, name: str, default):
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self, name: str, data) -> None:
        write_atomic(self.state_dir / name, json.dumps(data, ensure_ascii=False, indent=1))

    def _save_jobs(self) -> None:  # caller holds self._lock
        done = [j for j in self.jobs if j["stage"] == "done"]
        stale = {id(j) for j in done[:-KEEP_DONE]}
        self.jobs[:] = [j for j in self.jobs if id(j) not in stale]
        self._save("jobs.json", self.jobs)
