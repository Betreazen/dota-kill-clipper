from pathlib import Path

import pytest

from killclipper import dota_paths
from killclipper.dota_paths import CFG_NAME, cfg_text, find_dota_path, gsi_cfg_path, install_cfg


def test_cfg_text_contains_port_token_and_only_needed_sections():
    txt = cfg_text(3220, "abc123")
    assert '"uri"       "http://127.0.0.1:3220/"' in txt
    assert '"token" "abc123"' in txt
    for sec in ("provider", "map", "player", "hero"):
        assert f'"{sec}"' in txt
    assert '"items"' not in txt and '"abilities"' not in txt
    assert '"throttle"  "0.1"' in txt and '"buffer"    "0.1"' in txt


def test_gsi_cfg_path():
    assert gsi_cfg_path(Path("D:/dota 2 beta")) == \
        Path("D:/dota 2 beta") / "game" / "dota" / "cfg" / "gamestate_integration" / CFG_NAME
    assert CFG_NAME == "gamestate_integration_killclipper.cfg"


def test_find_dota_path_via_libraryfolders(tmp_path, monkeypatch):
    steam = tmp_path / "Steam"
    lib = tmp_path / "Games"
    dota = lib / "steamapps" / "common" / "dota 2 beta"
    dota.mkdir(parents=True)
    (steam / "steamapps").mkdir(parents=True)
    vdf = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % str(lib).replace("\\", "\\\\")
    (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf)
    monkeypatch.setattr(dota_paths, "find_steam_path", lambda: steam)
    assert find_dota_path() == dota


def test_find_dota_path_none_when_no_steam(monkeypatch):
    monkeypatch.setattr(dota_paths, "find_steam_path", lambda: None)
    assert find_dota_path() is None


def test_install_cfg_writes_file_idempotently(tmp_path):
    dota = tmp_path / "dota 2 beta"
    dota.mkdir()
    p = install_cfg(3220, "tok", dota_path=dota)
    assert p == gsi_cfg_path(dota) and p.read_text() == cfg_text(3220, "tok")
    mtime = p.stat().st_mtime_ns
    assert install_cfg(3220, "tok", dota_path=dota) == p
    assert p.stat().st_mtime_ns == mtime  # unchanged content not rewritten


def test_install_cfg_raises_when_dota_not_found(monkeypatch):
    monkeypatch.setattr(dota_paths, "find_dota_path", lambda: None)
    with pytest.raises(FileNotFoundError):
        install_cfg(3220, "tok")
