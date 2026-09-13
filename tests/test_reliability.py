"""Crash/reload contracts. No OBS, FFmpeg or YouTube writes."""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from killclipper.clipper import Clipper
from killclipper.publishing import Schedule
from tests.test_clipper import FakeBuffer
from tests.test_gsi import packet


def capture(tmp_path, spawn, handoff, cut):
    cfg = dict(root=tmp_path / 'clips', window=15, tail_single=10, tail_series=15,
               pre=10, count_assists=True, ffmpeg='fake')
    return Clipper(cfg, FakeBuffer(), logging.getLogger('test'), cut=cut, spawn=spawn,
                   on_clip=handoff, state_dir=tmp_path / 'state')


def cutter(calls):
    def cut(ffmpeg, src, dst, start, end, saved_at, keep_source=False):
        assert keep_source
        calls.append(str(src))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b'clip')
        return dict(ok=True, path=str(dst), start=0, end=20, duration=30,
                    truncated=False, error=None)
    return cut


def test_capture_recovers_when_obs_closes_before_cut_worker_runs(tmp_path):
    deferred, handed, calls = [], [], []
    c = capture(tmp_path, deferred.append, lambda *a: handed.append(a), cutter(calls))
    c.on_packet(1000, packet(kills=0))
    c.on_packet(1001, packet(kills=1))
    c.tick(1016)
    raw = tmp_path / 'replay.mp4'
    raw.write_bytes(b'raw')
    c.on_buffer_saved(str(raw), 1017)
    assert not calls
    capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    assert len(calls) == len(handed) == 1
    # A late old worker sees the committed journal and cannot cut/submit again.
    deferred[0]()
    assert len(calls) == len(handed) == 1
    assert not raw.exists()  # the OBS replay is removed once the clip is cut and safely handed off
    log = json.loads(Path(handed[0][4].path).read_text(encoding='utf-8'))
    assert log['clips'][0]['status'] == 'ok'


def test_failed_handoff_is_recovered_without_recut(tmp_path):
    deferred, calls, handed = [], [], []
    def unavailable(*args):
        raise OSError('queue storage unavailable')
    c = capture(tmp_path, deferred.append, unavailable, cutter(calls))
    c.on_packet(1000, packet(kills=0)); c.on_packet(1001, packet(kills=1)); c.tick(1016)
    raw = tmp_path / 'replay.mp4'; raw.write_bytes(b'raw')
    c.on_buffer_saved(str(raw), 1017)
    import pytest
    with pytest.raises(OSError):
        deferred[0]()
    capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    assert len(calls) == len(handed) == 1


def test_late_replay_is_recovered_from_request_journal(tmp_path):
    handed, calls = [], []
    c = capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    c.on_packet(1000, packet(kills=0)); c.on_packet(1001, packet(kills=1)); c.flush(1016)
    raw = tmp_path / 'replay.mp4'; raw.write_bytes(b'raw'); os.utime(raw, (1017, 1017))
    restarted = capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    restarted.recover_replay(str(raw))
    restarted.recover_replay(str(raw))
    assert len(calls) == len(handed) == 1


def closed_series_waiting_for_obs(tmp_path, handed, calls):
    """A series whose replay save was requested, then the script was reloaded before OBS answered."""
    c = capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    c.on_packet(1000, packet(kills=0))
    c.on_packet(1001, packet(kills=1))
    c.tick(1016)
    assert c.buffer.saves == 1 and list((tmp_path / 'state' / 'captures').glob('awaiting-*.json'))
    return c


def test_reload_re_requests_a_replay_that_is_still_in_the_running_buffer(tmp_path):
    handed, calls = [], []
    closed_series_waiting_for_obs(tmp_path, handed, calls)
    reloaded = capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    reloaded.recover_replay(None)            # OBS gave no replay path for the old request
    reloaded.resume_awaiting(1030)
    reloaded.tick(1030)
    assert reloaded.buffer.saves == 1        # a fresh save of the same footage
    raw = tmp_path / 'replay.mp4'
    raw.write_bytes(b'raw')
    reloaded.on_buffer_saved(str(raw), 1031)
    assert len(calls) == len(handed) == 1
    assert not list((tmp_path / 'state' / 'captures').glob('awaiting-*.json'))


def test_series_whose_footage_left_the_buffer_is_reported_lost_not_kept_forever(tmp_path, caplog):
    handed, calls = [], []
    closed_series_waiting_for_obs(tmp_path, handed, calls)
    restarted = capture(tmp_path, lambda f: f(), lambda *a: handed.append(a), cutter(calls))
    restarted.buffer._active = False         # OBS was restarted: the replay buffer is empty
    with caplog.at_level(logging.ERROR, logger='test'):
        restarted.resume_awaiting(1030)
    captures = tmp_path / 'state' / 'captures'
    assert not restarted.pending and not list(captures.glob('awaiting-*.json'))
    assert len(list(captures.glob('lost-*.json'))) == 1 and 'lost' in caplog.text.lower()


def test_concurrent_reservations_are_unique(tmp_path):
    path = tmp_path / 'schedule.json'
    def reserve(_):
        schedule = Schedule(path)
        return schedule.reserve(datetime.now(schedule.zone)).isoformat()
    with ThreadPoolExecutor(max_workers=8) as pool:
        slots = list(pool.map(reserve, range(16)))
    assert len(set(slots)) == 16
