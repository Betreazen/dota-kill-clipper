"""Smoke test of the OBS entry point against a stub obspython module (no OBS needed)."""
import importlib
import json
import sys
import time
import types
import urllib.request

import pytest

from tests.test_gsi import packet


def make_obs_stub():
    obs = types.SimpleNamespace()
    obs.LOG_DEBUG, obs.LOG_INFO, obs.LOG_WARNING, obs.LOG_ERROR = 300, 200, 100, 0
    obs.OBS_PATH_DIRECTORY, obs.OBS_PATH_FILE, obs.OBS_TEXT_INFO = 2, 0, 3
    obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED = 1
    obs.OBS_FRONTEND_EVENT_FINISHED_LOADING = 2
    obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPED = 3
    obs.logs, obs.timers, obs.callbacks, obs.calls = [], [], [], []
    obs.state = {"buffer_active": False, "last_replay": "", "config": {}}
    obs.script_log = lambda level, msg: obs.logs.append((level, msg))
    obs.timer_add = lambda fn, ms: obs.timers.append((fn, ms))
    obs.timer_remove = lambda fn: obs.timers.clear()
    obs.obs_frontend_add_event_callback = lambda fn: obs.callbacks.append(fn)
    obs.obs_frontend_remove_event_callback = lambda fn: obs.callbacks.remove(fn)
    obs.obs_frontend_replay_buffer_active = lambda: obs.state["buffer_active"]

    def start():
        obs.state["buffer_active"] = True
        obs.calls.append("start")
    obs.obs_frontend_replay_buffer_start = start
    obs.obs_frontend_replay_buffer_save = lambda: obs.calls.append("save")
    obs.obs_frontend_get_last_replay = lambda: obs.state["last_replay"]
    obs.obs_frontend_get_profile_config = lambda: obs.state["config"]
    obs.config_get_string = lambda cfg, sec, key: cfg.get((sec, key), "Advanced")
    obs.config_get_bool = lambda cfg, sec, key: bool(cfg.get((sec, key), False))
    obs.config_get_uint = lambda cfg, sec, key: int(cfg.get((sec, key), 20))
    obs.config_set_bool = lambda cfg, sec, key, v: cfg.__setitem__((sec, key), v)
    obs.config_set_uint = lambda cfg, sec, key, v: cfg.__setitem__((sec, key), v)
    obs.config_save = lambda cfg: obs.calls.append("config_save")
    # settings = plain dict
    obs.obs_data_set_default_bool = lambda s, k, v: s.setdefault(k, v)
    obs.obs_data_set_default_int = lambda s, k, v: s.setdefault(k, v)
    obs.obs_data_set_default_string = lambda s, k, v: s.setdefault(k, v)
    obs.obs_data_get_string = lambda s, k: s.get(k, "")
    obs.obs_data_get_int = lambda s, k: s.get(k, 0)
    obs.obs_data_get_bool = lambda s, k: s.get(k, False)
    obs.obs_properties_create = lambda: []
    for name in ("add_path", "add_int", "add_bool", "add_button", "add_text"):
        setattr(obs, f"obs_properties_{name}", lambda p, *a, _n=name: p.append((_n, a[0])))
    return obs


@pytest.fixture
def script(tmp_path, monkeypatch):
    obs = make_obs_stub()
    monkeypatch.setitem(sys.modules, "obspython", obs)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    sys.modules.pop("obs_dota_kill_clipper", None)
    mod = importlib.import_module("obs_dota_kill_clipper")
    monkeypatch.setattr(mod, "DEFAULTS", {**mod.DEFAULTS, "root": str(tmp_path / "Highlights"), "port": 0})
    settings = {}
    mod.script_defaults(settings)
    settings["root"] = str(tmp_path / "Highlights")
    settings["port"] = 0
    settings["vertical_enabled"] = False  # no real ffmpeg render in this smoke test
    mod.script_load(settings)
    yield mod, obs, settings
    mod.script_unload()
    for h in list(mod.log.handlers):
        h.close()
        mod.log.removeHandler(h)


def test_load_sets_up_timer_callbacks_server_and_buffer_config(script, tmp_path):
    mod, obs, settings = script
    assert obs.timers and obs.timers[0][1] == 100 and obs.callbacks == [mod.on_frontend_event]
    assert mod.server is not None and mod.server.port > 0
    assert (tmp_path / "Highlights").is_dir()
    cfg = obs.state["config"]
    assert cfg[("AdvOut", "RecRB")] is True and cfg[("AdvOut", "RecRBTime")] == 120
    assert cfg[("AdvOut", "RecRBSize")] == 2048 and "config_save" in obs.calls
    assert any("Replay buffer settings changed" in m for _, m in obs.logs)
    assert not any("Traceback" in m for _, m in obs.logs)


def test_tick_starts_buffer_and_processes_gsi_packets(script, tmp_path):
    mod, obs, settings = script
    mod.buffer_start_at = 0  # due now
    mod.tick()
    assert "start" in obs.calls and obs.state["buffer_active"]
    port = mod.server.port

    def send(p):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(p).encode())
        urllib.request.urlopen(req, timeout=5).close()
        deadline = time.time() + 5
        while mod.server.queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        mod.tick()

    send(packet(kills=0, clock=100, hero="npc_dota_hero_techies"))
    assert [d.name for d in (tmp_path / "Highlights").iterdir()][0].endswith("techies (match 8954164528)")
    cut_calls = []
    mod.clipper._cut = lambda *a, **kw: (cut_calls.append(a), {"ok": True, "path": str(a[2]), "start": 1.0, "end": 21.0,
                                                          "duration": 60.0, "truncated": False, "error": None})[1]
    mod.clipper._spawn = lambda fn: fn()
    send(packet(kills=1, clock=101, hero="npc_dota_hero_techies"))
    assert mod.clipper.detector.current is not None
    mod.clipper.detector.current.last -= 20  # fast-forward: series older than the window
    mod.tick()
    assert obs.calls.count("save") == 1
    obs.state["last_replay"] = str(tmp_path / "Replay.mp4")
    mod.on_frontend_event(obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED)
    mod.tick()
    assert len(cut_calls) == 1 and cut_calls[0][2].name.startswith("[00.01.31-00.01.51] 1 kill")
    assert "Last clip: " in mod.status_text() and "last packet" in mod.status_text()
    assert not any("Traceback" in m or "tick failed" in m for _, m in obs.logs)


def test_update_restarts_server_on_port_change_and_properties_build(script):
    mod, obs, settings = script
    old = mod.server
    mod.script_update({**settings, "port": 0})
    assert mod.server is old  # same port: no restart
    props = mod.script_properties()
    assert [name for name, _ in props][:3] == ["add_path", "add_int", "add_int"]
    assert ("add_button", "install") in props and ("add_text", "status") in props


def test_install_button_creates_token_and_cfg(script, tmp_path, monkeypatch):
    mod, obs, settings = script
    dota = tmp_path / "dota 2 beta"
    dota.mkdir()
    monkeypatch.setattr("killclipper.dota_paths.find_dota_path", lambda: dota)
    assert mod.install_gsi_cfg(None, None) is True
    token = (tmp_path / "appdata" / "dota-kill-clipper" / "token.txt").read_text()
    cfg = dota / "game" / "dota" / "cfg" / "gamestate_integration" / "gamestate_integration_killclipper.cfg"
    assert len(token) == 32 and token in cfg.read_text()
    assert mod.server.token == token
