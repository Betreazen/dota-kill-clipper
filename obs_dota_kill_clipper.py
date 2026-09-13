"""OBS Studio script: auto-save Dota 2 kill/assist clips from the replay buffer, render Shorts, publish to YouTube.

Add in OBS: Tools -> Scripts (Python 3.12). All obspython calls happen on the OBS
main thread: the GSI HTTP server runs in a background thread and only fills a queue,
which the 100 ms timer drains. Rendering and uploads run in one pipeline worker thread.
Logic lives in the killclipper package next to this file.
"""
import logging
import logging.handlers
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import obspython as obs  # noqa: E402

from killclipper.clipper import Clipper  # noqa: E402
from killclipper.cut import find_ffmpeg  # noqa: E402
from killclipper.dota_paths import install_cfg  # noqa: E402
from killclipper.gsi import GsiServer  # noqa: E402
from killclipper.pipeline import Pipeline  # noqa: E402
from killclipper.youtube import YoutubeClient  # noqa: E402

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "dota-kill-clipper")
TOKEN_FILE = os.path.join(DATA_DIR, "token.txt")
LOG_FILE = os.path.join(DATA_DIR, "clipper.log")
YOUTUBE_TOKEN = os.path.join(DATA_DIR, "youtube-token.json")
CHANNEL_NAME = "Betreazen highlights Dota 2"
HERO = "npc_dota_hero_techies"
DEFAULTS = {"root": r"C:\Highlights", "port": 3220, "window": 15, "tail_single": 10,
            "tail_series": 15, "pre": 10, "ffmpeg": r"C:\ffmpeg\bin\ffmpeg.exe", "count_assists": True,
            "vertical_enabled": True, "upper_cta": "", "lower_cta": "", "music_dir": r"C:\Highlights\music",
            "music_volume": 15, "youtube_enabled": False, "youtube_credentials": ""}
BUFFER_SECONDS, BUFFER_MB = 120, 2048
TICK_MS = 100

log = logging.getLogger("killclipper")
server: GsiServer | None = None
clipper: Clipper | None = None
pipeline: Pipeline | None = None
buffer_start_at: float | None = None  # deferred replay-buffer start (after OBS finished loading)


# ---- logging -------------------------------------------------------------------------
class ObsLogHandler(logging.Handler):
    LEVELS = {logging.DEBUG: obs.LOG_DEBUG, logging.INFO: obs.LOG_INFO, logging.WARNING: obs.LOG_WARNING}

    def emit(self, record):
        obs.script_log(self.LEVELS.get(record.levelno, obs.LOG_ERROR), self.format(record))


def setup_logging():
    if log.handlers:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    oh = ObsLogHandler()
    oh.setFormatter(logging.Formatter("[killclipper] %(message)s"))
    log.addHandler(fh)
    log.addHandler(oh)
    log.setLevel(logging.INFO)


# ---- OBS replay buffer ---------------------------------------------------------------
class ObsBuffer:
    def active(self):
        return obs.obs_frontend_replay_buffer_active()

    def start(self):
        obs.obs_frontend_replay_buffer_start()

    def save(self):
        obs.obs_frontend_replay_buffer_save()


def ensure_buffer_config():
    """Raise replay-buffer length/size in the profile if below what the clips need."""
    cfg = obs.obs_frontend_get_profile_config()
    section = "AdvOut" if obs.config_get_string(cfg, "Output", "Mode") == "Advanced" else "SimpleOutput"
    changed = []
    if not obs.config_get_bool(cfg, section, "RecRB"):
        obs.config_set_bool(cfg, section, "RecRB", True)
        changed.append("RecRB=true")
    for key, need in (("RecRBTime", BUFFER_SECONDS), ("RecRBSize", BUFFER_MB)):
        cur = obs.config_get_uint(cfg, section, key)
        if cur < need:
            obs.config_set_uint(cfg, section, key, need)
            changed.append(f"{key} {cur}->{need}")
    if changed:
        obs.config_save(cfg)
        log.warning("Replay buffer settings changed in profile (%s): %s. "
                    "Restart OBS (or re-apply Settings -> Output) so they take effect.",
                    section, ", ".join(changed))


def try_start_buffer():
    if obs.obs_frontend_replay_buffer_active():
        return
    obs.obs_frontend_replay_buffer_start()
    log.info("Replay buffer start requested")


def on_frontend_event(event):
    global buffer_start_at
    try:
        if event == obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED and clipper:
            clipper.on_buffer_saved(obs.obs_frontend_get_last_replay(), time.time())
        elif event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING:
            buffer_start_at = time.time() + 1.0
        elif event == obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPED:
            log.warning("Replay buffer stopped; pending clips wait until it runs again")
    except Exception:
        log.exception("frontend event %s failed", event)


# ---- GSI ------------------------------------------------------------------------------
def read_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def start_server(port):
    global server
    stop_server()
    try:
        server = GsiServer(port, read_token())
        server.start()
        log.info("GSI listening on 127.0.0.1:%d%s", server.port, "" if server.token else " (no token file yet)")
    except OSError as e:
        server = None
        log.error("Cannot listen on port %d: %s", port, e)


def stop_server():
    global server
    if server:
        server.stop()
        server = None


def install_gsi_cfg(props, prop):
    token = read_token()
    if not token:
        os.makedirs(DATA_DIR, exist_ok=True)
        token = secrets.token_hex(16)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token)
    try:
        path = install_cfg(current["port"], token)
        log.info("GSI config installed: %s (restart Dota 2 if it is running)", path)
    except OSError as e:
        log.error("GSI config install failed: %s", e)
    start_server(current["port"])  # pick up the token
    return True


# ---- YouTube --------------------------------------------------------------------------
def make_youtube(cfg):
    return YoutubeClient(cfg["youtube_credentials"], YOUTUBE_TOKEN) if cfg["youtube_credentials"] else None


def youtube_login(props, prop):
    if not pipeline:
        return False
    if not pipeline.youtube:
        log.error("YouTube: choose the Google OAuth client JSON first")
        return False
    log.info("YouTube: opening the browser, pick the %s channel", CHANNEL_NAME)
    pipeline.request_login()
    return True


# ---- tick ----------------------------------------------------------------------------
def tick():
    global buffer_start_at
    now = time.time()
    try:
        if buffer_start_at is not None and now >= buffer_start_at:
            buffer_start_at = None
            try_start_buffer()
        if server:
            while not server.queue.empty():
                ts, payload = server.queue.get_nowait()
                clipper.on_packet(ts, payload)
        clipper.tick(now)
    except Exception:  # never let one bad tick kill the timer
        log.exception("tick failed")


# ---- OBS script API --------------------------------------------------------------------
current = dict(DEFAULTS)


def script_description():
    return ("<b>Dota Kill Clipper</b><br>Saves clips of your Techies kills/assists from the replay buffer "
            "into per-match folders, renders vertical Shorts and schedules them on YouTube.")


def script_defaults(settings):
    for key, val in DEFAULTS.items():
        if isinstance(val, bool):
            obs.obs_data_set_default_bool(settings, key, val)
        elif isinstance(val, int):
            obs.obs_data_set_default_int(settings, key, val)
        else:
            obs.obs_data_set_default_string(settings, key, val)


def read_settings(settings):
    return {
        "root": obs.obs_data_get_string(settings, "root") or DEFAULTS["root"],
        "port": obs.obs_data_get_int(settings, "port"),
        "window": obs.obs_data_get_int(settings, "window"),
        "tail_single": obs.obs_data_get_int(settings, "tail_single"),
        "tail_series": obs.obs_data_get_int(settings, "tail_series"),
        "pre": obs.obs_data_get_int(settings, "pre"),
        "ffmpeg": find_ffmpeg(obs.obs_data_get_string(settings, "ffmpeg")) or DEFAULTS["ffmpeg"],
        "count_assists": obs.obs_data_get_bool(settings, "count_assists"),
        "hero": HERO,
        "channel": CHANNEL_NAME,
        "vertical": {
            "enabled": obs.obs_data_get_bool(settings, "vertical_enabled"),
            "upper_cta": obs.obs_data_get_string(settings, "upper_cta"),
            "lower_cta": obs.obs_data_get_string(settings, "lower_cta"),
            "music_dir": obs.obs_data_get_string(settings, "music_dir"),
            "music_volume": obs.obs_data_get_int(settings, "music_volume") / 100,
        },
        "youtube_enabled": obs.obs_data_get_bool(settings, "youtube_enabled"),
        "youtube_credentials": obs.obs_data_get_string(settings, "youtube_credentials"),
    }


def script_properties():
    p = obs.obs_properties_create()
    obs.obs_properties_add_path(p, "root", "Clips folder", obs.OBS_PATH_DIRECTORY, "", None)
    obs.obs_properties_add_int(p, "port", "GSI port", 1024, 65535, 1)
    obs.obs_properties_add_int(p, "pre", "Seconds before first kill", 1, 60, 1)
    obs.obs_properties_add_int(p, "tail_single", "Tail after a single kill (s)", 1, 60, 1)
    obs.obs_properties_add_int(p, "tail_series", "Tail after a series (s)", 1, 60, 1)
    obs.obs_properties_add_int(p, "window", "Series window (s)", 1, 60, 1)
    obs.obs_properties_add_path(p, "ffmpeg", "ffmpeg.exe", obs.OBS_PATH_FILE, "ffmpeg (*.exe)", None)
    obs.obs_properties_add_bool(p, "count_assists", "Count assists as events")
    obs.obs_properties_add_bool(p, "vertical_enabled", "Render vertical 1080x1920 Short (shorts subfolder)")
    obs.obs_properties_add_path(p, "upper_cta", "Top CTA, 1080x420 with alpha", obs.OBS_PATH_FILE,
                                "Video (*.mov *.webm *.gif *.png *.mp4)", None)
    obs.obs_properties_add_path(p, "lower_cta", "Bottom CTA, 1080x420 with alpha", obs.OBS_PATH_FILE,
                                "Video (*.mov *.webm *.gif *.png *.mp4)", None)
    obs.obs_properties_add_path(p, "music_dir", "Music folder (Kevin MacLeod)", obs.OBS_PATH_DIRECTORY, "", None)
    obs.obs_properties_add_int(p, "music_volume", "Music volume (%)", 0, 100, 1)
    obs.obs_properties_add_bool(p, "youtube_enabled", f"Upload Shorts to YouTube ({CHANNEL_NAME})")
    obs.obs_properties_add_path(p, "youtube_credentials", "Google OAuth client JSON", obs.OBS_PATH_FILE,
                                "JSON (*.json)", None)
    obs.obs_properties_add_button(p, "youtube_login", "Log in to YouTube", youtube_login)
    obs.obs_properties_add_button(p, "install", "Install GSI config into Dota 2", install_gsi_cfg)
    obs.obs_properties_add_text(p, "status", status_text(), obs.OBS_TEXT_INFO)
    obs.obs_properties_add_button(p, "refresh", "Refresh status", lambda props, prop: True)
    return p


def status_text():
    if not clipper:
        return "not loaded"
    gsi = "no packets"
    if clipper.last_packet_ts:
        gsi = f"last packet {time.time() - clipper.last_packet_ts:.0f} s ago"
    if not server:
        gsi = "server NOT running (port busy?)"
    buf = "active" if obs.obs_frontend_replay_buffer_active() else "NOT active"
    shorts = pipeline.status() if pipeline else "-"
    return (f"GSI: {gsi} | Replay buffer: {buf} | Last clip: {clipper.last_clip or '-'} | "
            f"{shorts} | Log: {LOG_FILE}")


def script_update(settings):
    global current
    new = read_settings(settings)
    port_changed = new["port"] != current["port"]
    credentials_changed = new["youtube_credentials"] != current["youtube_credentials"]
    current = new
    if clipper:
        clipper.update(current)
    if pipeline:
        pipeline.cfg = current
        if credentials_changed:
            pipeline.youtube = make_youtube(current)
    if clipper and (port_changed or server is None):
        start_server(current["port"])


def script_load(settings):
    global clipper, pipeline, buffer_start_at
    setup_logging()
    current.update(read_settings(settings))
    pipeline = Pipeline(current, DATA_DIR, log, youtube=make_youtube(current))
    pipeline.start()
    clipper = Clipper(current, ObsBuffer(), log, on_clip=pipeline.submit)
    os.makedirs(current["root"], exist_ok=True)
    log.info("Loaded (python %s). Clips -> %s, ffmpeg: %s", sys.version.split()[0], current["root"], current["ffmpeg"])
    ensure_buffer_config()
    start_server(current["port"])
    obs.obs_frontend_add_event_callback(on_frontend_event)
    obs.timer_add(tick, TICK_MS)
    buffer_start_at = time.time() + 2.0  # script added to a running OBS: FINISHED_LOADING never comes


def script_unload():
    global clipper, pipeline
    obs.timer_remove(tick)
    obs.obs_frontend_remove_event_callback(on_frontend_event)
    stop_server()
    if clipper:
        clipper.flush(time.time())
        clipper = None
    if pipeline:
        pipeline.stop()
        pipeline = None
    log.info("Unloaded")
