import json
from datetime import datetime

from killclipper.naming import MatchLog, clip_name, clock_str, hero_short, match_dir_name, unique_path


def test_hero_short_strips_prefix():
    assert hero_short("npc_dota_hero_pudge") == "pudge"
    assert hero_short("npc_dota_hero_shadow_shaman") == "shadow_shaman"
    assert hero_short("") == "unknown"
    assert hero_short(None) == "unknown"


def test_clock_str_formats_hh_mm_ss():
    assert clock_str(0) == "00.00.00"
    assert clock_str(1417) == "00.23.37"
    assert clock_str(3661) == "01.01.01"
    assert clock_str(-40) == "-00.00.40"
    assert clock_str(59.6) == "00.01.00"


def test_match_dir_name():
    dt = datetime(2026, 9, 9, 21, 35, 12)
    assert match_dir_name(dt, "npc_dota_hero_pudge", 8954164528) == \
        "2026-09-09 21-35 pudge (match 8954164528)"
    assert match_dir_name(dt, "npc_dota_hero_axe", 0) == "2026-09-09 21-35 axe (demo)"


def test_clip_name_variants():
    assert clip_name(1407, 1427, 1, 0, (3, 1, 2)) == "[00.23.27-00.23.47] 1 kill (KDA 3-1-2).mp4"
    assert clip_name(1852, 1903, 3, 0, (6, 1, 4)) == "[00.30.52-00.31.43] 3 kills (KDA 6-1-4).mp4"
    assert clip_name(100, 130, 2, 1, (2, 0, 1)) == "[00.01.40-00.02.10] 2 kills + 1 assist (KDA 2-0-1).mp4"
    assert clip_name(100, 130, 0, 2, (0, 0, 2)) == "[00.01.40-00.02.10] 2 assists (KDA 0-0-2).mp4"
    assert clip_name(-40, -20, 1, 0, (1, 0, 0)) == "[-00.00.40--00.00.20] 1 kill (KDA 1-0-0).mp4"


def test_unique_path_adds_suffix(tmp_path):
    p = tmp_path / "a.mp4"
    assert unique_path(p) == p
    p.write_bytes(b"")
    assert unique_path(p) == tmp_path / "a (2).mp4"
    (tmp_path / "a (2).mp4").write_bytes(b"")
    assert unique_path(p) == tmp_path / "a (3).mp4"


def test_match_log_roundtrip(tmp_path):
    log = MatchLog(tmp_path / "match.json")
    log.start(match_id=5, hero_name="npc_dota_hero_pudge", steamid="765", started_at="2026-09-09T21:35:12")
    log.add_event({"type": "kill", "ts": 1000.0, "clock_time": 1417, "kda": (3, 1, 2)})
    idx = log.add_clip({"path": "x.mp4", "start_clock": 1407, "end_clock": 1427, "status": "saving"})
    log.update_clip(idx, status="ok", ffmpeg_start=88.0, ffmpeg_end=108.0)
    data = json.loads((tmp_path / "match.json").read_text(encoding="utf-8"))
    assert data["match_id"] == 5 and data["hero"] == "pudge" and data["steamid"] == "765"
    assert data["started_at"] == "2026-09-09T21:35:12"
    assert data["events"][0]["clock_time"] == 1417 and data["events"][0]["kda"] == [3, 1, 2]
    assert data["clips"][0]["status"] == "ok" and data["clips"][0]["ffmpeg_end"] == 108.0
    assert idx == 0


def test_match_log_loads_existing(tmp_path):
    p = tmp_path / "match.json"
    p.write_text(json.dumps({"match_id": 1, "hero": "axe", "events": [{"type": "kill"}], "clips": []}))
    log = MatchLog(p)
    log.add_event({"type": "assist"})
    assert len(json.loads(p.read_text())["events"]) == 2


def test_match_log_survives_corrupt_file(tmp_path):
    p = tmp_path / "match.json"
    p.write_text("{broken")
    log = MatchLog(p)
    log.start(match_id=1, hero_name="x", steamid=None, started_at="t")
    assert json.loads(p.read_text())["match_id"] == 1
