"""Orchestration: GSI packets -> series -> replay-buffer save -> ffmpeg cut -> on_clip hand-off.

No OBS imports. The OBS script passes a `buffer` object (active()/start()/save()),
calls on_packet() for queued GSI packets, on_buffer_saved() from the OBS
REPLAY_BUFFER_SAVED event and tick(now) every 100 ms, all on the OBS main thread.
cut_clip runs in a worker thread (spawn); its result is applied on the next tick, and every
finished clip is handed to on_clip (the Shorts/YouTube pipeline).
"""
import queue
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .cut import cut_clip
from .gsi import GsiTracker
from .naming import MatchLog, clip_name, match_dir_name, unique_path
from .series import Series, SeriesDetector

SAVE_TIMEOUT = 30.0     # seconds to wait for OBS to report the saved replay file
BUFFER_RETRY = 5.0      # seconds between replay-buffer start attempts while it is inactive
STALE_AFTER = 60.0      # drop a pending series once its end is this far in the past (buffer gone)


def _thread(fn):
    threading.Thread(target=fn, daemon=True).start()


class Clipper:
    def __init__(self, cfg: dict, buffer, log, cut=cut_clip, spawn=_thread, on_clip=None):
        self.buffer, self.log, self._cut, self._spawn, self.on_clip = buffer, log, cut, spawn, on_clip
        self.tracker = GsiTracker(cfg["count_assists"])
        self.detector = SeriesDetector(cfg["window"], cfg["tail_single"], cfg["tail_series"], cfg["pre"])
        self.cfg = cfg
        self.match_dir: Path | None = None
        self.match_log: MatchLog | None = None
        # a job is (series, match_dir, match_log) captured when the series closed
        self.pending: deque[tuple] = deque()             # jobs waiting for a buffer save
        self.saving: tuple[tuple, float] | None = None   # (job, requested_at)
        self._results: queue.Queue = queue.Queue()       # (match_log, clip_idx, series, result)
        self._buffer_try_at = 0.0
        self._capture_enabled = False
        self.last_packet_ts: float | None = None
        self.last_clip: str | None = None

    # ---- inputs -------------------------------------------------------------------
    def update(self, cfg: dict) -> None:
        """New settings from the OBS panel. Series timings apply to the next series."""
        self.cfg = cfg
        self.tracker.count_assists = cfg["count_assists"]
        d = self.detector
        d.window, d.tail_single, d.tail_series, d.pre = cfg["window"], cfg["tail_single"], cfg["tail_series"], cfg["pre"]

    def on_packet(self, ts: float, payload: dict) -> list[dict]:
        self.last_packet_ts = ts
        events = self.tracker.feed(payload, ts)
        for ev in events:
            if ev["type"] == "match":
                self._queue(self.detector.flush())
                self._open_match(ev)
            elif self._capture_enabled:
                if self.match_log:
                    self.match_log.add_event(ev)
                self._queue(self.detector.add(ev))
        return events

    def on_buffer_saved(self, path: str, saved_at: float) -> None:
        if self.saving is None:
            self.log.info("Replay buffer saved without a pending series, ignored: %s", path)
            return
        job, _ = self.saving
        self.saving = None
        self._start_cut(job, Path(path), saved_at)

    def tick(self, now: float) -> None:
        self._queue(self.detector.tick(now))
        if self.saving and now - self.saving[1] > SAVE_TIMEOUT:
            self.log.error("Replay buffer save timeout (%.0f s), series dropped", SAVE_TIMEOUT)
            self.saving = None
        if self.saving is None and self.pending:
            self._request_save(now)
        self._apply_results()

    def flush(self, now: float) -> None:
        """Script unload / OBS exit: save whatever is pending right away."""
        self._queue(self.detector.flush())
        self.tick(now)

    # ---- internals ----------------------------------------------------------------
    def _open_match(self, ev: dict) -> None:
        wanted_hero = self.cfg.get("hero")
        self._capture_enabled = not wanted_hero or ev["hero_name"] == wanted_hero
        if not self._capture_enabled:
            self.match_dir, self.match_log = None, None
            self.log.info("Ignoring %s: only %s is configured", ev["hero_name"], wanted_hero)
            return
        name = match_dir_name(datetime.fromtimestamp(ev["ts"]), ev["hero_name"], ev["match_id"])
        match_dir = Path(self.cfg["root"]) / name
        self.match_dir = match_dir
        try:
            match_dir.mkdir(parents=True, exist_ok=True)
            match_log = MatchLog(match_dir / "match.json")
            match_log.start(ev["match_id"], ev["hero_name"], ev["steamid"],
                            datetime.fromtimestamp(ev["ts"]).isoformat(timespec="seconds"))
            self.match_log = match_log
            self.log.info("Match folder: %s", match_dir)
        except OSError as e:
            self.log.error("Cannot create match folder %s: %s", match_dir, e)
            self.match_dir, self.match_log = None, None

    def _queue(self, series: Series | None) -> None:
        if series is not None:
            self.pending.append((series, self.match_dir, self.match_log))
            self.log.info("Series closed: %d kills, %d assists, clock %d..%d",
                          series.kills, series.assists, series.start_clock, series.end_clock)

    def _request_save(self, now: float) -> None:
        if not self.buffer.active():
            if now - self._buffer_try_at >= BUFFER_RETRY:
                self._buffer_try_at = now
                self.log.warning("Replay buffer is not active (%d clip(s) waiting); starting it", len(self.pending))
                self.buffer.start()
            while self.pending and now - self.pending[0][0].end_ts > STALE_AFTER:
                self.pending.popleft()
                self.log.error("Series dropped: replay buffer was inactive for too long")
            return
        self.buffer.save()
        self.saving = (self.pending.popleft(), now)

    def _start_cut(self, job: tuple, src: Path, saved_at: float) -> None:
        series, folder, log = job
        folder = folder or Path(self.cfg["root"])
        dst = unique_path(folder / clip_name(series.start_clock, series.end_clock,
                                             series.kills, series.assists, series.kda))
        idx = None
        if log:
            idx = log.add_clip({"path": str(dst), "start_clock": series.start_clock,
                                "end_clock": series.end_clock, "kills": series.kills,
                                "assists": series.assists, "status": "cutting"})
        ffmpeg = self.cfg["ffmpeg"]

        def work():
            try:
                res = self._cut(ffmpeg, src, dst, series.start_ts, series.end_ts, saved_at)
            except Exception as e:  # never leave a clip stuck in "cutting"
                res = {"ok": False, "path": str(src), "start": None, "end": None, "duration": None,
                       "truncated": False, "error": f"{type(e).__name__}: {e}"}
            self._results.put((log, idx, series, res))

        self._spawn(work)

    def _apply_results(self) -> None:
        while True:
            try:
                log, idx, series, res = self._results.get_nowait()
            except queue.Empty:
                return
            if res["ok"]:
                self.last_clip = res["path"]
                self.log.info("Clip saved: %s (%.1f..%.1f of %.1f s)%s", res["path"], res["start"],
                              res["end"], res["duration"], " TRUNCATED by buffer length" if res["truncated"] else "")
            else:
                self.log.error("Cut failed, raw buffer kept: %s (%s)", res["path"], res["error"])
            if log is not None and idx is not None:
                log.update_clip(idx, status="ok" if res["ok"] else "error", path=res["path"],
                                ffmpeg_start=res["start"], ffmpeg_end=res["end"],
                                truncated=res["truncated"], error=res["error"])
            if res["ok"] and self.on_clip:
                try:
                    self.on_clip(res["path"], series.kills, series.assists, series.kda, log, idx)
                except Exception:
                    self.log.exception("Shorts pipeline hand-off failed for %s", res["path"])
