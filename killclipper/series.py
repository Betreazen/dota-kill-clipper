"""Kill/assist series detector. Pure logic, time injected (wall clock seconds)."""
from dataclasses import dataclass, field


@dataclass
class Series:
    first: float          # wall time of first event
    last: float           # wall time of last event
    first_clock: int      # Dota clock_time of first event
    last_clock: int
    kda: tuple = (0, 0, 0)
    kills: int = 0
    assists: int = 0
    events: list = field(default_factory=list)
    pre: float = 10.0     # seconds before first event
    tail: float = 10.0    # seconds after last event (set on close)

    @property
    def start_ts(self) -> float:
        return self.first - self.pre

    @property
    def end_ts(self) -> float:
        return self.last + self.tail

    @property
    def start_clock(self) -> int:
        return int(round(self.first_clock - self.pre))

    @property
    def end_clock(self) -> int:
        return int(round(self.last_clock + self.tail))


class SeriesDetector:
    def __init__(self, window: float = 15, tail_single: float = 10,
                 tail_series: float = 15, pre: float = 10):
        self.window = window
        self.tail_single = tail_single
        self.tail_series = tail_series
        self.pre = pre
        self.current: Series | None = None

    def add(self, ev: dict) -> Series | None:
        """Add a kill/assist event. Returns the previous series if this event starts a new one."""
        closed = None
        s = self.current
        if s is not None and ev["ts"] - s.last > self.window:
            closed = self._close()
            s = None
        if s is None:
            s = self.current = Series(first=ev["ts"], last=ev["ts"], first_clock=ev["clock_time"],
                                      last_clock=ev["clock_time"], pre=self.pre)
        s.last, s.last_clock, s.kda = ev["ts"], ev["clock_time"], tuple(ev["kda"])
        s.events.append(ev)
        if ev["type"] == "assist":
            s.assists += 1
        else:
            s.kills += 1
        return closed

    def tick(self, now: float) -> Series | None:
        """Call periodically. Returns the series once it is complete (no events for max(window, tail))."""
        s = self.current
        if s is None or now - s.last < max(self.window, self._tail(s)):
            return None
        return self._close()

    def flush(self) -> Series | None:
        """Close the pending series immediately (match change / unload)."""
        return self._close() if self.current else None

    def _tail(self, s: Series) -> float:
        return self.tail_single if s.kills + s.assists == 1 else self.tail_series

    def _close(self) -> Series:
        s, self.current = self.current, None
        s.tail = self._tail(s)
        return s
