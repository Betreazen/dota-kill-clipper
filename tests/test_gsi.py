import json
import queue
import urllib.error
import urllib.request

import pytest

from killclipper.gsi import GsiServer, GsiTracker, parse_packet

ACTIVE = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"


def packet(kills=0, deaths=0, assists=0, clock=100, matchid="8954164528",
           state=ACTIVE, hero="npc_dota_hero_pudge", paused=False):
    return {
        "provider": {"name": "Dota 2", "timestamp": 1},
        "map": {"matchid": matchid, "game_state": state, "clock_time": clock,
                "game_time": clock + 90, "paused": paused},
        "player": {"steamid": "76561198000000000", "kills": kills, "deaths": deaths,
                   "assists": assists, "team_name": "radiant"},
        "hero": {"name": hero, "id": 14},
    }


def test_parse_packet_extracts_state():
    s = parse_packet(packet(kills=3, deaths=1, assists=2, clock=1417))
    assert s == {
        "match_id": 8954164528, "game_state": ACTIVE, "clock_time": 1417,
        "paused": False, "steamid": "76561198000000000",
        "hero_name": "npc_dota_hero_pudge", "kills": 3, "deaths": 1, "assists": 2,
    }


def test_parse_packet_menu_returns_none():
    assert parse_packet({"provider": {"name": "Dota 2"}}) is None
    assert parse_packet({"map": {"matchid": "0"}, "player": {}, "hero": {}}) is None


def test_parse_packet_demo_has_match_id_zero():
    s = parse_packet(packet(matchid="0"))
    assert s["match_id"] == 0
    s = parse_packet({"map": {"game_state": ACTIVE, "clock_time": 5},
                      "player": {"kills": 1}, "hero": {"name": "npc_dota_hero_axe"}})
    assert s["match_id"] == 0 and s["kills"] == 1


def test_parse_packet_missing_counters_default_zero():
    s = parse_packet({"map": {"matchid": "5"}, "player": {}, "hero": {"name": "x"}})
    assert (s["kills"], s["deaths"], s["assists"], s["clock_time"]) == (0, 0, 0, 0)


def test_first_packet_is_baseline_no_kill_events():
    t = GsiTracker()
    evs = t.feed(packet(kills=5), ts=1000.0)
    assert [e["type"] for e in evs] == ["match"]
    assert evs[0]["match_id"] == 8954164528
    assert evs[0]["hero_name"] == "npc_dota_hero_pudge"
    assert evs[0]["ts"] == 1000.0


def test_kill_increment_emits_kill_event_with_kda_after():
    t = GsiTracker()
    t.feed(packet(kills=2, deaths=1, assists=2, clock=1400), ts=1000.0)
    evs = t.feed(packet(kills=3, deaths=1, assists=2, clock=1417), ts=1017.0)
    assert evs == [{"type": "kill", "ts": 1017.0, "clock_time": 1417, "kda": (3, 1, 2)}]


def test_double_kill_in_one_packet_emits_two_events():
    t = GsiTracker()
    t.feed(packet(kills=0), ts=0.0)
    evs = t.feed(packet(kills=2, clock=200), ts=1.0)
    assert [e["type"] for e in evs] == ["kill", "kill"]
    assert all(e["clock_time"] == 200 for e in evs)


def test_assist_increment_emits_assist_event():
    t = GsiTracker()
    t.feed(packet(assists=0), ts=0.0)
    evs = t.feed(packet(assists=1, clock=300), ts=1.0)
    assert evs == [{"type": "assist", "ts": 1.0, "clock_time": 300, "kda": (0, 0, 1)}]


def test_assists_can_be_disabled():
    t = GsiTracker(count_assists=False)
    t.feed(packet(assists=0), ts=0.0)
    assert t.feed(packet(assists=1), ts=1.0) == []


def test_no_change_no_events():
    t = GsiTracker()
    t.feed(packet(kills=1), ts=0.0)
    assert t.feed(packet(kills=1), ts=0.5) == []


def test_kill_outside_active_state_is_ignored():
    t = GsiTracker()
    t.feed(packet(kills=1), ts=0.0)
    evs = t.feed(packet(kills=2, state="DOTA_GAMERULES_STATE_POST_GAME"), ts=1.0)
    assert evs == []


def test_match_change_resets_baseline():
    t = GsiTracker()
    t.feed(packet(kills=7, matchid="1"), ts=0.0)
    evs = t.feed(packet(kills=0, matchid="2"), ts=1.0)
    assert [e["type"] for e in evs] == ["match"]
    assert evs[0]["match_id"] == 2
    assert t.feed(packet(kills=1, matchid="2"), ts=2.0)[0]["type"] == "kill"


def test_match_event_waits_for_active_state():
    t = GsiTracker()
    assert t.feed(packet(state="DOTA_GAMERULES_STATE_HERO_SELECTION", hero=""), ts=0.0) == []
    evs = t.feed(packet(state="DOTA_GAMERULES_STATE_PRE_GAME", clock=-60), ts=1.0)
    assert [e["type"] for e in evs] == ["match"]
    assert evs[0]["hero_name"] == "npc_dota_hero_pudge"


def test_menu_then_same_match_is_reconnect_not_new_match():
    t = GsiTracker()
    t.feed(packet(kills=1), ts=0.0)
    assert t.feed({"provider": {}}, ts=1.0) == []
    evs = t.feed(packet(kills=4), ts=2.0)  # kills jumped while disconnected: baseline
    assert evs == []
    assert t.feed(packet(kills=5), ts=3.0)[0]["type"] == "kill"


def test_menu_then_new_demo_is_new_match():
    t = GsiTracker()
    t.feed(packet(matchid="0"), ts=0.0)
    t.feed({"provider": {}}, ts=1.0)
    evs = t.feed(packet(matchid="0"), ts=2.0)
    assert [e["type"] for e in evs] == ["match"]


def test_second_bot_game_via_hero_selection_is_new_match_with_fresh_baseline():
    t = GsiTracker()
    t.feed(packet(matchid="0", kills=9), ts=0.0)
    t.feed(packet(matchid="0", kills=9, state="DOTA_GAMERULES_STATE_POST_GAME"), ts=1.0)
    t.feed(packet(matchid="0", kills=0, state="DOTA_GAMERULES_STATE_HERO_SELECTION"), ts=2.0)
    evs = t.feed(packet(matchid="0", kills=0, state="DOTA_GAMERULES_STATE_PRE_GAME"), ts=3.0)
    assert [e["type"] for e in evs] == ["match"]
    assert t.feed(packet(matchid="0", kills=1), ts=4.0)[0]["type"] == "kill"


def test_real_match_hero_selection_to_game_is_one_match():
    t = GsiTracker()
    t.feed(packet(matchid="7", state="DOTA_GAMERULES_STATE_HERO_SELECTION"), ts=0.0)
    assert [e["type"] for e in t.feed(packet(matchid="7", state="DOTA_GAMERULES_STATE_PRE_GAME"), ts=1.0)] == ["match"]
    assert t.feed(packet(matchid="7"), ts=2.0) == []
    assert t.feed(packet(matchid="7", kills=1), ts=3.0)[0]["type"] == "kill"


def test_server_rejects_non_object_json_and_bad_length():
    srv = GsiServer(port=0)
    srv.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(srv.port, b"[1, 2]")
        assert ei.value.code == 400
    finally:
        srv.stop()


def post(port, data):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=5)


def test_server_receives_post_and_queues_payload():
    srv = GsiServer(port=0)
    srv.start()
    try:
        with post(srv.port, json.dumps(packet(kills=1)).encode()) as r:
            assert r.status == 200
        ts, payload = srv.queue.get(timeout=5)
        assert payload["player"]["kills"] == 1
        assert isinstance(ts, float)
    finally:
        srv.stop()


def test_server_rejects_bad_token():
    srv = GsiServer(port=0, token="secret")
    srv.start()
    try:
        bad = {**packet(), "auth": {"token": "nope"}}
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(srv.port, json.dumps(bad).encode())
        assert ei.value.code == 403
        good = {**packet(), "auth": {"token": "secret"}}
        post(srv.port, json.dumps(good).encode()).close()
        assert srv.queue.get(timeout=5)[1]["auth"]["token"] == "secret"
    finally:
        srv.stop()


def test_server_ignores_garbage_body():
    srv = GsiServer(port=0)
    srv.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(srv.port, b"not json")
        assert ei.value.code == 400
        with pytest.raises(queue.Empty):
            srv.queue.get(timeout=0.2)
    finally:
        srv.stop()
