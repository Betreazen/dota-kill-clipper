"""Match folder names, clip names, match.json."""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

HERO_PREFIX = "npc_dota_hero_"


def hero_short(name: str | None) -> str:
    return (name or "").removeprefix(HERO_PREFIX) or "unknown"


def clock_str(seconds: float) -> str:
    """Dota clock seconds -> 'HH.MM.SS'; negative -> '-HH.MM.SS'."""
    t = int(round(seconds))
    sign = "-" if t < 0 else ""
    t = abs(t)
    return f"{sign}{t // 3600:02d}.{t // 60 % 60:02d}.{t % 60:02d}"


def match_dir_name(started_at: datetime, hero_name: str | None, match_id: int) -> str:
    tag = f"match {match_id}" if match_id else "demo"
    return f"{started_at:%Y-%m-%d %H-%M} {hero_short(hero_name)} ({tag})"


def clip_name(start_clock: int, end_clock: int, kills: int, assists: int, kda: tuple) -> str:
    parts = []
    if kills:
        parts.append(f"{kills} kill{'s' if kills > 1 else ''}")
    if assists:
        parts.append(f"{assists} assist{'s' if assists > 1 else ''}")
    k, d, a = kda
    return f"[{clock_str(start_clock)}-{clock_str(end_clock)}] {' + '.join(parts)} (KDA {k}-{d}-{a}).mp4"


def unique_path(path: Path) -> Path:
    n = 1
    candidate = path
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
    return candidate


class MatchLog:
    """match.json next to the clips. Rewritten on every change (small file).

    Shared by the OBS main thread and the Shorts pipeline thread, hence the lock.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict = {"events": [], "clips": []}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = {"events": [], "clips": [], **loaded}
        except (OSError, ValueError):
            pass

    def start(self, match_id: int, hero_name: str | None, steamid: str | None, started_at: str) -> None:
        with self._lock:
            self.data.update(match_id=match_id, hero=hero_short(hero_name), steamid=steamid,
                             started_at=started_at)
            self.save()

    def add_event(self, ev: dict) -> None:
        with self._lock:
            self.data["events"].append(ev)
            self.save()

    def add_clip(self, clip: dict) -> int:
        with self._lock:
            self.data["clips"].append(clip)
            self.save()
            return len(self.data["clips"]) - 1

    def update_clip(self, idx: int, **fields) -> None:
        with self._lock:
            self.data["clips"][idx].update(fields)
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)  # a crash mid-write never truncates match.json
