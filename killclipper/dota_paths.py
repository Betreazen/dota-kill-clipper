"""Find Dota 2 via Steam registry + libraryfolders.vdf, install the GSI cfg.

Adapted from dota-helper-app/d2pt/gsi_config.py.
"""
import re
from pathlib import Path

CFG_NAME = "gamestate_integration_killclipper.cfg"
CFG_TEMPLATE = '''"KillClipper"
{{
    "uri"       "http://127.0.0.1:{port}/"
    "timeout"   "5.0"
    "buffer"    "0.1"
    "throttle"  "0.1"
    "heartbeat" "30.0"
    "auth"      {{ "token" "{token}" }}
    "data"
    {{
        "provider"   "1"
        "map"        "1"
        "player"     "1"
        "hero"       "1"
    }}
}}
'''


def cfg_text(port: int, token: str) -> str:
    return CFG_TEMPLATE.format(port=port, token=token)


def find_steam_path() -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except (OSError, ImportError):
        return None


def find_dota_path() -> Path | None:
    steam = find_steam_path()
    if not steam:
        return None
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        # ponytail: no VDF parser, "path" values are enough
        libs += [Path(m.replace("\\\\", "\\"))
                 for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore"))]
    for lib in libs:
        d = lib / "steamapps" / "common" / "dota 2 beta"
        if d.exists():
            return d
    return None


def gsi_cfg_path(dota_path: Path) -> Path:
    return Path(dota_path) / "game" / "dota" / "cfg" / "gamestate_integration" / CFG_NAME


def install_cfg(port: int, token: str, dota_path: Path | str | None = None) -> Path:
    """Write the cfg (idempotent). Raises FileNotFoundError if Dota is not found, OSError on write."""
    dota = Path(dota_path) if dota_path else find_dota_path()
    if not dota or not dota.exists():
        raise FileNotFoundError("Dota 2 folder not found (Steam registry / libraryfolders.vdf)")
    cfg = gsi_cfg_path(dota)
    content = cfg_text(port, token)
    if not cfg.exists() or cfg.read_text(encoding="utf-8") != content:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(content, encoding="utf-8")
    return cfg
