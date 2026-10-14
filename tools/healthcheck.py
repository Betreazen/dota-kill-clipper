"""Read-only health check of everything the OBS plugin needs.

Run with the OBS Python so imports and packages match what OBS uses:
    %LOCALAPPDATA%\\Python\\pythoncore-3.12-64\\python.exe tools\\healthcheck.py

Nothing is uploaded or changed on YouTube, OBS or Dota, and no packet is sent to the GSI port.
Only a 1-second NVENC test file is written to the temp folder. Exit code 1 when a check fails.
"""
import configparser
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from killclipper import DEFAULT_ROOT  # noqa: E402

DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dota-kill-clipper"
OBS = Path(os.environ.get("APPDATA", Path.home())) / "obs-studio"
PAGES = "https://betreazen.github.io/dota-kill-clipper/"
results: list[tuple[str, str, str]] = []


def check(name):
    def wrap(fn):
        try:
            status, detail = fn()
        except Exception as e:  # a crashing check is a failed check
            status, detail = "FAIL", f"{type(e).__name__}: {e}"
        results.append((status, name, detail))
        print(f"[{status:4}] {name}: {detail}", flush=True)
        return fn
    return wrap


def obs_settings() -> dict:
    scene_file = sorted(glob.glob(str(OBS / "basic" / "scenes" / "*.json")))[0]
    scene = json.loads(Path(scene_file).read_text(encoding="utf-8"))
    for script in scene.get("modules", {}).get("scripts-tool", []):
        if script["path"].endswith("obs_dota_kill_clipper.py"):
            return {"_path": script["path"], **script.get("settings", {})}
    return {}


SETTINGS = {}
try:
    SETTINGS = obs_settings()
except Exception:
    pass
FFMPEG = SETTINGS.get("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


@check("Python")
def _():
    ok = sys.version_info[:2] == (3, 12)
    return ("OK" if ok else "WARN"), f"{sys.version.split()[0]} ({sys.executable}); OBS uses 3.12"


@check("Package imports")
def _():
    import killclipper.clipper, killclipper.pipeline, killclipper.render, killclipper.youtube  # noqa: F401,E401
    import googleapiclient, google_auth_oauthlib  # noqa: F401,E401
    return "OK", "killclipper + google-api-python-client + google-auth-oauthlib"


@check("Time zone")
def _():
    from killclipper.publishing import zone
    z = zone("Europe/Minsk")
    return "OK", f"Europe/Minsk -> {z} ({'tzdata' if hasattr(z, 'key') else 'fixed UTC+3 fallback, expected without tzdata'})"


@check("OBS script registered")
def _():
    if not SETTINGS:
        return "FAIL", "obs_dota_kill_clipper.py not found in the scene collection"
    keys = ("vertical_enabled", "youtube_enabled", "upper_cta", "lower_cta", "music_dir", "music_volume")
    return "OK", f"{SETTINGS['_path']}; " + ", ".join(f"{k}={SETTINGS.get(k, 'default')}" for k in keys)


def obs_profile() -> tuple[configparser.ConfigParser, str]:
    ini = glob.glob(str(OBS / "basic" / "profiles" / "*" / "basic.ini"))[0]
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(ini, encoding="utf-8-sig")
    return cp, "AdvOut" if cp.get("Output", "Mode", fallback="") == "Advanced" else "SimpleOutput"


@check("OBS replay buffer profile")
def _():
    cp, section = obs_profile()
    rb, t, size = cp.get(section, "RecRB", fallback="false"), cp.getint(section, "RecRBTime", fallback=0), \
        cp.getint(section, "RecRBSize", fallback=0)
    ok = rb == "true" and t >= 120 and size >= 2048
    return ("OK" if ok else "FAIL"), f"{section}: RecRB={rb}, RecRBTime={t} s, RecRBSize={size} MB"


@check("OBS last session log")
def _():
    logs = sorted((OBS / "logs").glob("*.txt"), key=lambda p: p.stat().st_mtime)
    text = logs[-1].read_text(encoding="utf-8", errors="ignore")
    loaded = text.count("Loaded python script: obs_dota_kill_clipper.py")
    tracebacks = len(re.findall(r"obs_dota_kill_clipper\.py\].*Traceback|\[Unknown Script\] Traceback", text))
    status = "FAIL" if not loaded else ("WARN" if tracebacks else "OK")
    return status, f"{logs[-1].name}: script loaded {loaded}x, tracebacks {tracebacks}"


@check("ffmpeg / ffprobe")
def _():
    from killclipper.cut import ffprobe_path
    version = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True).stdout.split("\n")[0]
    assert Path(ffprobe_path(FFMPEG)).exists(), "ffprobe missing"
    return "OK", version


@check("NVENC encoder")
def _():
    out = Path(tempfile.gettempdir()) / "killclipper_nvenc_test.mp4"
    r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=60",
                        "-t", "1", "-c:v", "h264_nvenc", str(out)], capture_output=True, text=True)
    out.unlink(missing_ok=True)
    return ("OK", "h264_nvenc works") if r.returncode == 0 else ("WARN", f"falls back to libx264: {r.stderr.strip()[:120]}")


@check("CTA overlays")
def _():
    from killclipper.cut import ffprobe_path
    parts = []
    status = "OK"
    for key in ("upper_cta", "lower_cta"):
        path = SETTINGS.get(key)
        if not path:
            parts.append(f"{key}: not set")
            status = "WARN"
            continue
        probe = subprocess.run([ffprobe_path(FFMPEG), "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height,pix_fmt:format=duration", "-of", "json", path],
                               capture_output=True, text=True)
        info = json.loads(probe.stdout or "{}")
        stream, fmt = (info.get("streams") or [{}])[0], info.get("format", {})
        alpha = "a" in stream.get("pix_fmt", "").replace("gray", "")
        if not stream or not alpha:
            status = "WARN" if stream else "FAIL"
        parts.append(f"{Path(path).name} {stream.get('width')}x{stream.get('height')} {stream.get('pix_fmt')} "
                     f"{float(fmt.get('duration', 0)):.1f}s alpha={alpha}")
    return status, "; ".join(parts)


@check("Music library")
def _():
    from killclipper.render import AUDIO_SUFFIXES
    folder = Path(SETTINGS.get("music_dir") or DEFAULT_ROOT + r"\music")
    tracks = [p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES]
    credits = json.loads((folder / "credits.json").read_text(encoding="utf-8")) if (folder / "credits.json").exists() else {}
    missing = [p.name for p in tracks if p.name not in credits]
    status = "OK" if tracks else "FAIL"
    return status, f"{len(tracks)} tracks in {folder}, credits.json titles for {len(tracks) - len(missing)} " \
                   f"(others use the file name)"


@check("SEO dictionaries and limits")
def _():
    import random
    from killclipper.publishing import load_dictionary, metadata, playlist_description, tag_cost
    d = load_dictionary()
    worst = []
    for seed in range(12):
        m = metadata(seed % 6, seed % 3, (5, 2, 3), "Betreazen highlights Dota 2", '"X" Kevin MacLeod', rng=random.Random(seed))
        worst.append((len(m["title"]), len(m["description"].encode()), tag_cost(m["tags"])))
    plist = max(len(playlist_description(k, "Betreazen highlights Dota 2").encode()) for k in (None, 1, 2, 3, 4, 5, 6))
    ok = max(w[0] for w in worst) <= 100 and max(w[1] for w in worst) <= 5000 and max(w[2] for w in worst) <= 500 and plist <= 5000
    hooks = sum(len(v) for v in d["ru"]["hooks"].values()) + sum(len(v) for v in d["en"]["hooks"].values())
    return ("OK" if ok else "FAIL"), (f"{hooks} hooks, {len(d['tags'])} tags; max title {max(w[0] for w in worst)}, "
                                      f"description {max(w[1] for w in worst)} B, tags {max(w[2] for w in worst)}, playlist {plist} B")


@check("Dota 2 GSI config")
def _():
    from killclipper.dota_paths import find_dota_path, gsi_cfg_path
    cfg = gsi_cfg_path(find_dota_path())
    text = cfg.read_text(encoding="utf-8")
    token = (DATA / "token.txt").read_text(encoding="utf-8").strip()
    port = SETTINGS.get("port", 3220)
    ok = token in text and f":{port}/" in text
    return ("OK" if ok else "FAIL"), f"{cfg} (port {port}, token matches: {token in text})"


@check("GSI port listening")
def _():
    port = SETTINGS.get("port", 3220)
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
    return "OK", f"127.0.0.1:{port} accepts connections (OBS script running)"


@check("Disk space")
def _():
    root = SETTINGS.get("root") or DEFAULT_ROOT
    cp, section = obs_profile()
    recordings = cp.get(section, "RecFilePath" if section == "AdvOut" else "FilePath", fallback="D:\\")
    parts, status = [], "OK"
    for label, path in (("clips", root), ("OBS recordings", recordings)):  # disk_usage follows junctions
        if not Path(path).exists():
            continue
        free = shutil.disk_usage(path).free / 2**30
        status = "WARN" if free < 20 else status
        parts.append(f"{label} {path}: {free:.0f} GB free")
    return status, "; ".join(parts)


@check("Pipeline state files")
def _():
    def load(name, default):
        try:
            return json.loads((DATA / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
    jobs = load("jobs.json", [])
    stages = dict(Counter(j["stage"] for j in jobs))
    status = "WARN" if stages.get("failed") else "OK"
    reports = sorted(p.name for p in (DATA / "reports").glob("*.json")) if (DATA / "reports").exists() else []
    state = load("state.json", {})
    paused = state.get("uploads_paused_until", 0)
    pause = "none"
    if paused > time.time():
        status = "WARN" if status == "OK" else status
        pause = f"until {datetime.fromtimestamp(paused):%d.%m %H:%M} ({state.get('uploads_pause_reason')})"
    return status, (f"jobs {stages or 'none'}; upload pause {pause}; schedule slots {len(load('schedule.json', []))}; "
                    f"last review {state.get('last_review')}; reports {reports}")


@check("YouTube channel, token and playlists")
def _():
    from killclipper.publishing import playlist_title
    from killclipper.youtube import YoutubeClient
    if not SETTINGS.get("youtube_credentials"):
        return "WARN", "YouTube upload not configured in OBS"
    yt = YoutubeClient(SETTINGS["youtube_credentials"], str(DATA / "youtube-token.json"),
                       playlist_cache=str(DATA / "playlists.json")).connect(interactive=False)
    titles, request = [], yt.service.playlists().list(part="snippet", mine=True, maxResults=50)
    while request:
        page = request.execute()
        titles += [p["snippet"]["title"] for p in page.get("items", [])]
        request = yt.service.playlists().list_next(request, page)
    expected = [playlist_title(k) for k in (None, 1, 2, 3, 4, 5)]
    missing = [t for t in expected if t not in titles]
    dupes = [t for t, n in Counter(titles).items() if n > 1]
    status = "FAIL" if missing or dupes else "OK"
    enabled = SETTINGS.get("youtube_enabled")
    return status, (f"channel '{yt.channel_title}' ({yt.channel_id}), upload enabled={enabled}; "
                    f"playlists missing={missing or 'none'}, duplicates={dupes or 'none'}")


@check("Project site (GitHub Pages)")
def _():
    codes = {}
    for page in ("", "privacy.html", "terms.html"):
        with urllib.request.urlopen(PAGES + page, timeout=15) as r:
            codes[page or "index"] = r.status
    return ("OK" if set(codes.values()) == {200} else "FAIL"), str(codes)


failed = [r for r in results if r[0] == "FAIL"]
warned = [r for r in results if r[0] == "WARN"]
print(f"\n{len(results)} checks: {len(results) - len(failed) - len(warned)} OK, {len(warned)} WARN, {len(failed)} FAIL")
sys.exit(1 if failed else 0)
