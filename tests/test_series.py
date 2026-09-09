import pytest

from killclipper.series import Series, SeriesDetector


def ev(ts, clock, type="kill", kda=(1, 0, 0)):
    return {"type": type, "ts": ts, "clock_time": clock, "kda": kda}


@pytest.fixture
def det():
    return SeriesDetector(window=15, tail_single=10, tail_series=15, pre=10)


def test_single_kill_closes_after_window(det):
    assert det.add(ev(1000.0, 1417, kda=(3, 1, 2))) is None
    assert det.tick(1014.9) is None
    s = det.tick(1015.0)
    assert isinstance(s, Series)
    assert (s.kills, s.assists, s.kda) == (1, 0, (3, 1, 2))
    assert s.tail == 10
    assert (s.start_ts, s.end_ts) == (990.0, 1010.0)
    assert (s.start_clock, s.end_clock) == (1407, 1427)
    assert det.tick(1100.0) is None  # nothing pending


def test_two_kills_within_window_form_series_with_long_tail(det):
    det.add(ev(1000.0, 1862, kda=(4, 1, 4)))
    assert det.add(ev(1013.0, 1875, kda=(5, 1, 4))) is None
    assert det.tick(1027.9) is None
    s = det.tick(1028.0)
    assert (s.kills, s.assists, s.kda) == (2, 0, (5, 1, 4))
    assert s.tail == 15
    assert (s.start_ts, s.end_ts) == (990.0, 1028.0)
    assert (s.start_clock, s.end_clock) == (1852, 1890)


def test_three_kills_each_extends_window(det):
    det.add(ev(1000.0, 1862))
    det.add(ev(1013.0, 1875))
    assert det.tick(1027.0) is None
    det.add(ev(1027.5, 1893, kda=(6, 1, 4)))
    assert det.tick(1042.0) is None
    s = det.tick(1042.5)
    assert s.kills == 3 and s.last_clock == 1893 and s.kda == (6, 1, 4)


def test_double_kill_same_timestamp_is_series_of_two(det):
    det.add(ev(1000.0, 200))
    det.add(ev(1000.0, 200))
    s = det.tick(1015.0)
    assert s.kills == 2 and s.tail == 15


def test_assist_inside_series_extends_it(det):
    det.add(ev(1000.0, 500, kda=(1, 0, 0)))
    det.add(ev(1010.0, 510, type="assist", kda=(1, 0, 1)))
    assert det.tick(1024.9) is None
    s = det.tick(1025.0)
    assert (s.kills, s.assists, s.tail, s.kda) == (1, 1, 15, (1, 0, 1))


def test_only_assist_makes_a_series(det):
    det.add(ev(1000.0, 500, type="assist", kda=(0, 0, 1)))
    s = det.tick(1015.0)
    assert (s.kills, s.assists, s.tail) == (0, 1, 10)


def test_event_after_gap_closes_old_series_immediately():
    det = SeriesDetector(window=15, tail_single=10, tail_series=20, pre=10)
    det.add(ev(1000.0, 100))
    det.add(ev(1005.0, 105))
    # close time is max(window, tail)=20 after last -> 1025; event at 1021 is outside window
    old = det.add(ev(1021.0, 121))
    assert old is not None and old.kills == 2 and old.end_ts == 1025.0
    s = det.tick(1036.0)
    assert s.kills == 1 and s.first == 1021.0


def test_close_uses_max_of_window_and_tail():
    det = SeriesDetector(window=5, tail_single=10, tail_series=15, pre=10)
    det.add(ev(0.0, 0))
    assert det.tick(9.9) is None
    assert det.tick(10.0).tail == 10


def test_flush_returns_pending_series(det):
    assert det.flush() is None
    det.add(ev(1000.0, 100))
    s = det.flush()
    assert s.kills == 1
    assert det.flush() is None


def test_negative_clock_bounds(det):
    det.add(ev(1000.0, -30, kda=(1, 0, 0)))
    s = det.tick(1015.0)
    assert (s.start_clock, s.end_clock) == (-40, -20)


def test_series_keeps_event_list(det):
    det.add(ev(1000.0, 100))
    det.add(ev(1005.0, 105, type="assist"))
    s = det.flush()
    assert [e["type"] for e in s.events] == ["kill", "assist"]
    assert (s.first, s.last, s.first_clock, s.last_clock) == (1000.0, 1005.0, 100, 105)
