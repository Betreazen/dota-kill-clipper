"""Dota 2 GSI: HTTP receiver (background thread) and packet -> kill/assist events.

Parsing logic adapted from dota-helper-app/d2pt/events.py (first packet of a match
is a baseline, no diff; reconnect-safe).
"""
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACTIVE_STATES = {"DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"}


def parse_packet(p: dict) -> dict | None:
    """GSI payload -> flat state. None when in menu/lobby (no hero, no match)."""
    m, pl, h = p.get("map") or {}, p.get("player") or {}, p.get("hero") or {}
    mid = str(m.get("matchid") or "0")
    if not mid.isdigit():
        mid = "0"
    if int(mid) == 0 and not h.get("name"):
        return None
    return {
        "match_id": int(mid),
        "game_state": m.get("game_state", ""),
        "clock_time": m.get("clock_time", 0),
        "paused": bool(m.get("paused")),
        "steamid": pl.get("steamid"),
        "hero_name": h.get("name") or "",
        "kills": pl.get("kills", 0),
        "deaths": pl.get("deaths", 0),
        "assists": pl.get("assists", 0),
    }


class GsiTracker:
    """Feeds packets, emits events:
    {"type": "match", "match_id", "hero_name", "steamid", "ts", "clock_time"} once per match
    (when the game becomes active), and one {"type": "kill"|"assist", "ts", "clock_time",
    "kda"} per counter increment while the game is active.
    """

    def __init__(self, count_assists: bool = True):
        self.count_assists = count_assists
        self.state: dict | None = None   # last parsed state (None = menu)
        self.match_id: int | None = None
        self._started = False

    def feed(self, payload: dict, ts: float) -> list[dict]:
        s = parse_packet(payload)
        if s is None:
            self.state = None
            return []
        prev, self.state = self.state, s
        evs: list[dict] = []
        active = s["game_state"] in ACTIVE_STATES
        became_active = active and (prev is None or prev["game_state"] not in ACTIVE_STATES)
        # new match, or a fresh demo/bot game (demo id is always 0) after the menu or
        # after a non-active state (hero selection, post game)
        if s["match_id"] != self.match_id or (became_active and s["match_id"] == 0):
            self.match_id = s["match_id"]
            self._started = False
        if became_active:
            prev = None  # counters may have jumped/reset while not in game: baseline
        if active and not self._started:
            self._started = True
            evs.append({"type": "match", "match_id": s["match_id"], "hero_name": s["hero_name"],
                        "steamid": s["steamid"], "ts": ts, "clock_time": s["clock_time"]})
        if prev is None or not active:
            return evs  # baseline packet (start/reconnect) or not in game
        kda = (s["kills"], s["deaths"], s["assists"])
        counters = [("kill", "kills")] + ([("assist", "assists")] if self.count_assists else [])
        for etype, key in counters:
            for _ in range(max(0, s[key] - prev[key])):
                evs.append({"type": etype, "ts": ts, "clock_time": s["clock_time"], "kda": kda})
        return evs


class GsiServer:
    """HTTP receiver for Dota GSI POSTs. Packets land in self.queue as (ts, payload)."""

    def __init__(self, port: int, token: str | None = None):
        self.queue: queue.Queue = queue.Queue()
        self.token = token
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(n))
                except ValueError:
                    return self._reply(400)
                if not isinstance(payload, dict):
                    return self._reply(400)
                if server.token and (payload.get("auth") or {}).get("token") != server.token:
                    return self._reply(403)
                server.queue.put((time.time(), payload))
                self._reply(200)

            def _reply(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):  # silence per-packet stderr noise
                pass

        return Handler

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
