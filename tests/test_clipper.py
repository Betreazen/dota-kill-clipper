import json
import logging

import pytest

from killclipper.clipper import SAVE_TIMEOUT, Clipper
from tests.test_gsi import packet


class FakeBuffer:
    def __init__(self, active=True):
        self._active = active
        self.saves = 0
        self.starts = 0

    def active(self):
        return self._active

    def start(self):
        self.starts += 1
        self._active = True

    def save(self):
        self.saves += 1


def fake_cut(ok=True):
    calls = []

    def cut(ffmpeg, src, dst, start_ts, end_ts, saved_at):
        calls.append(dict(ffmpeg=ffmpeg, src=src, dst=dst, start_ts=start_ts, end_ts=end_ts, saved_at=saved_at))
        if ok:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"clip")
        return {"ok": ok, "path": str(dst), "start": 1.0, "end": 21.0, "duration": 120.0,
                "truncated": False, "error": None if ok else "boom"}
    cut.calls = calls
    return cut


@pytest.fixture
def env(tmp_path):
    buf = FakeBuffer()
    cut = fake_cut()
    cfg = dict(root=tmp_path / "Highlights", window=15, tail_single=10, tail_series=15, pre=10,
               count_assists=True, ffmpeg="ffmpeg.exe", hero="npc_dota_hero_pudge")
    c = Clipper(cfg, buffer=buf, log=logging.getLogger("test"), cut=cut, spawn=lambda fn: fn())
    return c, buf, cut, tmp_path / "Highlights"


def test_match_event_creates_folder_and_match_json(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(matchid="8954164528", kills=0))
    dirs = list(root.iterdir())
    assert len(dirs) == 1 and dirs[0].name.endswith("pudge (match 8954164528)")
    data = json.loads((dirs[0] / "match.json").read_text(encoding="utf-8"))
    assert data["match_id"] == 8954164528 and data["hero"] == "pudge"
    assert c.last_packet_ts == 1000.0


def test_single_kill_full_pipeline(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(kills=0, clock=1400, deaths=1, assists=2))
    c.on_packet(1017.0, packet(kills=1, clock=1417, deaths=1, assists=2))
    c.tick(1020.0)
    assert buf.saves == 0
    c.tick(1032.0)  # 15 s after the kill: series closed -> save requested
    assert buf.saves == 1
    c.tick(1033.0)
    assert buf.saves == 1  # waiting for the saved event, no duplicate save
    c.on_buffer_saved("D:/obs/Replay 2026.mp4", saved_at=1033.5)
    c.tick(1034.0)
    assert len(cut.calls) == 1
    call = cut.calls[0]
    assert call["src"].name == "Replay 2026.mp4"
    assert call["dst"].name == "[00.23.27-00.23.47] 1 kill (KDA 1-1-2).mp4"
    assert (call["start_ts"], call["end_ts"], call["saved_at"]) == (1007.0, 1027.0, 1033.5)
    data = json.loads((call["dst"].parent / "match.json").read_text(encoding="utf-8"))
    assert data["events"][0]["type"] == "kill"
    assert data["clips"][0]["status"] == "ok" and data["clips"][0]["ffmpeg_start"] == 1.0
    assert c.last_clip == str(call["dst"])


def test_series_of_two_uses_long_tail_and_counts(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(kills=0, clock=1400))
    c.on_packet(1010.0, packet(kills=1, clock=1410))
    c.on_packet(1020.0, packet(kills=1, assists=1, clock=1420))
    c.tick(1034.9)
    assert buf.saves == 0
    c.tick(1035.0)
    assert buf.saves == 1
    c.on_buffer_saved("r.mp4", 1036.0)
    c.tick(1036.0)
    assert cut.calls[0]["dst"].name == "[00.23.20-00.23.55] 1 kill + 1 assist (KDA 1-0-1).mp4"
    assert cut.calls[0]["end_ts"] == 1035.0


def test_two_series_queue_one_save_at_a_time(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(kills=0, clock=100))
    c.on_packet(1001.0, packet(kills=1, clock=101))
    c.tick(1016.0)
    assert buf.saves == 1
    c.on_packet(1017.0, packet(kills=2, clock=117))  # new series while first is saving
    c.tick(1032.0)
    assert buf.saves == 1  # second save waits for the first saved event
    c.on_buffer_saved("a.mp4", 1032.5)
    c.tick(1033.0)
    assert buf.saves == 2
    c.on_buffer_saved("b.mp4", 1034.0)
    c.tick(1034.0)
    assert [x["src"].name for x in cut.calls] == ["a.mp4", "b.mp4"]


def test_buffer_inactive_series_waits_then_saves_when_buffer_back(env, caplog):
    c, buf, cut, root = env
    buf._active = False
    buf.start = lambda: buf.__dict__.__setitem__("starts", buf.starts + 1)  # start does not activate
    c.on_packet(1000.0, packet(kills=0))
    c.on_packet(1001.0, packet(kills=1))
    with caplog.at_level(logging.WARNING, logger="test"):
        c.tick(1016.0)
        c.tick(1016.1)
        c.tick(1022.0)
    assert buf.saves == 0 and buf.starts == 2  # retried after BUFFER_RETRY, not every tick
    assert "buffer is not active" in caplog.text.lower()
    assert len(c.pending) == 1 and c.saving is None
    buf._active = True
    c.tick(1023.0)
    assert buf.saves == 1 and not c.pending


def test_buffer_inactive_too_long_drops_stale_series(env, caplog):
    c, buf, cut, root = env
    buf._active = False
    buf.start = lambda: None
    c.on_packet(1000.0, packet(kills=0))
    c.on_packet(1001.0, packet(kills=1))
    c.tick(1016.0)
    with caplog.at_level(logging.ERROR, logger="test"):
        c.tick(1001.0 + 10 + 60 + 1)  # end_ts=1011, stale after 60 s
    assert not c.pending and "dropped" in caplog.text.lower()


def test_clip_goes_to_the_match_it_happened_in(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(matchid="1", kills=0, clock=100))
    c.on_packet(1001.0, packet(matchid="1", kills=1, clock=101))
    c.tick(1016.0)  # save requested for match 1
    c.on_packet(1017.0, packet(matchid="2", kills=0, clock=0))  # match changes while saving
    c.on_buffer_saved("r.mp4", 1018.0)
    c.tick(1018.0)
    assert cut.calls[0]["dst"].parent.name.endswith("(match 1)")
    data = json.loads((cut.calls[0]["dst"].parent / "match.json").read_text(encoding="utf-8"))
    assert data["match_id"] == 1 and data["clips"][0]["status"] == "ok"


def test_save_timeout_drops_series_and_continues(env, caplog):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(kills=0))
    c.on_packet(1001.0, packet(kills=1))
    c.tick(1016.0)
    assert buf.saves == 1
    with caplog.at_level(logging.ERROR, logger="test"):
        c.tick(1016.0 + SAVE_TIMEOUT + 1)
    assert c.saving is None and "timeout" in caplog.text.lower()


def test_cut_failure_recorded_in_match_json(tmp_path):
    buf, cut = FakeBuffer(), fake_cut(ok=False)
    cfg = dict(root=tmp_path, window=15, tail_single=10, tail_series=15, pre=10,
               count_assists=True, ffmpeg="ffmpeg.exe")
    c = Clipper(cfg, buffer=buf, log=logging.getLogger("test"), cut=cut, spawn=lambda fn: fn())
    c.on_packet(1000.0, packet(kills=0))
    c.on_packet(1001.0, packet(kills=1))
    c.tick(1016.0)
    c.on_buffer_saved("r.mp4", 1017.0)
    c.tick(1017.0)
    data = json.loads((cut.calls[0]["dst"].parent / "match.json").read_text(encoding="utf-8"))
    assert data["clips"][0]["status"] == "error" and data["clips"][0]["error"] == "boom"


def test_match_change_flushes_pending_series(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(matchid="1", kills=0))
    c.on_packet(1001.0, packet(matchid="1", kills=1))
    c.on_packet(1002.0, packet(matchid="2", kills=0))
    c.tick(1002.0)
    assert buf.saves == 1
    assert len(list(root.iterdir())) == 2


def test_unload_flush_saves_pending(env):
    c, buf, cut, root = env
    c.on_packet(1000.0, packet(kills=0))
    c.on_packet(1001.0, packet(kills=1))
    c.flush(1002.0)
    assert buf.saves == 1


def test_config_update_changes_detector_and_root(env, tmp_path):
    c, buf, cut, root = env
    c.update(dict(root=tmp_path / "Other", window=5, tail_single=3, tail_series=4, pre=2,
                  count_assists=False, ffmpeg="x.exe", hero="npc_dota_hero_pudge"))
    c.on_packet(1000.0, packet(kills=0, assists=0, clock=100))
    assert c.on_packet(1001.0, packet(kills=0, assists=1, clock=101)) == []
    c.on_packet(1002.0, packet(kills=1, assists=1, clock=102))
    c.tick(1007.0)
    assert buf.saves == 1
    c.on_buffer_saved("r.mp4", 1008.0)
    c.tick(1008.0)
    assert cut.calls[0]["dst"].parent.parent == tmp_path / "Other"
    assert cut.calls[0]["dst"].name == "[00.01.40-00.01.45] 1 kill (KDA 1-0-1).mp4"
    assert cut.calls[0]["ffmpeg"] == "x.exe"


def test_other_hero_is_ignored(tmp_path):
    buf, cut = FakeBuffer(), fake_cut()
    cfg = dict(root=tmp_path, window=15, tail_single=10, tail_series=15, pre=10,
               count_assists=True, ffmpeg="ffmpeg.exe", hero="npc_dota_hero_techies")
    c = Clipper(cfg, buffer=buf, log=logging.getLogger("test"), cut=cut, spawn=lambda fn: fn())
    c.on_packet(1000.0, packet(hero="npc_dota_hero_pudge", kills=0))
    c.on_packet(1001.0, packet(hero="npc_dota_hero_pudge", kills=1))
    c.tick(1030.0)
    assert not list(tmp_path.iterdir()) and not buf.saves and not cut.calls


def test_finished_clip_is_handed_to_pipeline(tmp_path):
    handed = []
    buf, cut = FakeBuffer(), fake_cut()
    cfg = dict(root=tmp_path, window=15, tail_single=10, tail_series=15, pre=10,
               count_assists=True, ffmpeg="ffmpeg.exe")
    c = Clipper(cfg, buf, logging.getLogger("test"), cut=cut, spawn=lambda fn: fn(),
                on_clip=lambda *args: handed.append(args))
    c.on_packet(1000, packet(kills=0))
    c.on_packet(1001, packet(kills=1, assists=1))
    c.tick(1016)
    c.on_buffer_saved("r.mp4", 1017)
    c.tick(1017)
    path, kills, assists, kda, log, idx = handed[0]
    assert path == str(cut.calls[0]["dst"]) and (kills, assists, kda) == (1, 1, (1, 0, 1))
    assert log.data["clips"][idx]["status"] == "ok"


def test_cut_crash_is_recorded_not_stuck_and_not_handed_off(tmp_path, caplog):
    handed = []

    def crashing_cut(*args, **kwargs):
        raise TypeError("'dict' object is not callable")

    cfg = dict(root=tmp_path, window=15, tail_single=10, tail_series=15, pre=10,
               count_assists=True, ffmpeg="ffmpeg.exe")
    c = Clipper(cfg, FakeBuffer(), logging.getLogger("test"), cut=crashing_cut, spawn=lambda fn: fn(),
                on_clip=lambda *args: handed.append(args))
    c.on_packet(1000, packet(kills=0))
    c.on_packet(1001, packet(kills=1))
    c.tick(1016)
    c.on_buffer_saved("r.mp4", 1017)
    with caplog.at_level(logging.ERROR, logger="test"):
        c.tick(1017)
    clip = c.match_log.data["clips"][0]
    assert clip["status"] == "error" and "TypeError" in clip["error"] and not handed
