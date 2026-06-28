"""Tests for netwatch. Run with:  pytest tests.py -v

No real network is touched: we feed fake samples and a fake clock.
"""

import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import netwatch as nw


def at(seconds: int) -> datetime:
    """A fixed clock helper: time T0 + N seconds."""
    return datetime(2025, 1, 1, 12, 0, 0) + timedelta(seconds=seconds)


# --- the debounce state machine ---


def test_single_blip_is_ignored():
    d = nw.DropDetector(fail_threshold=2)
    assert d.update(True, at(0)) is None
    assert d.update(False, at(5)) is None  # one failure, not yet believed
    assert d.update(True, at(10)) is None  # came back -> it was just a blip


def test_real_drop_is_timed_from_first_failure():
    d = nw.DropDetector(fail_threshold=2, recover_threshold=1)
    assert d.update(True, at(0)) is None
    assert d.update(False, at(5)) is None
    t = d.update(False, at(10))
    assert t is not None
    assert t.to is nw.Connectivity.DOWN
    assert t.at == at(5)  # counts from the FIRST failure


def test_recovery_is_timed_from_first_success():
    d = nw.DropDetector(fail_threshold=2, recover_threshold=1)
    d.update(True, at(0))
    d.update(False, at(5))
    d.update(False, at(10))
    t = d.update(True, at(20))
    assert t is not None
    assert t.to is nw.Connectivity.UP
    assert t.at == at(20)


# --- the whole monitor, wired with fakes ---


class FakeReachability:
    """Returns the next value from a list each time it is asked."""

    def __init__(self, values):
        self._it = iter(values)

    def is_reachable(self):
        return next(self._it)


def test_monitor_logs_one_isp_drop():
    # internet: up, up, down, down, down, up, up
    internet = FakeReachability([True, True, False, False, False, True, True])
    router = FakeReachability([True] * 10)  # router fine -> blame the ISP
    clock = iter(at(i * 5) for i in range(20))
    log = nw.InMemoryOutageLog()

    monitor = nw.Monitor(
        internet,
        router,
        nw.DropDetector(fail_threshold=2, recover_threshold=1),
        log,
        gateway="192.168.1.1",
        interval=0,
        clock=lambda: next(clock),
    )
    for _ in range(7):
        monitor.tick()

    drops = log.read_all()
    assert len(drops) == 1
    assert drops[0].cause.startswith("ISP")
    assert drops[0].start == at(10)  # first failing sample
    assert drops[0].end == at(25)  # first success after


def test_monitor_blames_local_when_router_also_down():
    internet = FakeReachability([True, False, False, True])
    router = FakeReachability([False] * 10)  # router unreachable too
    clock = iter(at(i * 5) for i in range(20))
    log = nw.InMemoryOutageLog()

    monitor = nw.Monitor(
        internet,
        router,
        nw.DropDetector(fail_threshold=2, recover_threshold=1),
        log,
        gateway="192.168.1.1",
        interval=0,
        clock=lambda: next(clock),
    )
    for _ in range(4):
        monitor.tick()

    drops = log.read_all()
    assert len(drops) == 1
    assert drops[0].cause == "local network"


def test_monitor_uses_router_status_from_first_failure():
    # The router is down at the FIRST failing sample but back by the sample
    # that confirms the drop. That first reading must still count, so the
    # blame lands on the local network.
    internet = FakeReachability([True, False, False, True])
    router = FakeReachability([False, True])  # down at first failure, then up
    clock = iter(at(i * 5) for i in range(20))
    log = nw.InMemoryOutageLog()

    monitor = nw.Monitor(
        internet,
        router,
        nw.DropDetector(fail_threshold=2, recover_threshold=1),
        log,
        gateway="192.168.1.1",
        interval=0,
        clock=lambda: next(clock),
    )
    for _ in range(4):
        monitor.tick()

    drops = log.read_all()
    assert len(drops) == 1
    assert drops[0].cause == "local network"


def test_blip_with_bad_router_does_not_poison_later_drop():
    # A single failure (with the router briefly down) is just a blip and is
    # discarded. A real ISP drop later must NOT inherit that stale reading.
    internet = FakeReachability([True, False, True, True, False, False, True])
    router = FakeReachability([False, True, True])  # only read on failures
    clock = iter(at(i * 5) for i in range(20))
    log = nw.InMemoryOutageLog()

    monitor = nw.Monitor(
        internet,
        router,
        nw.DropDetector(fail_threshold=2, recover_threshold=1),
        log,
        gateway="192.168.1.1",
        interval=0,
        clock=lambda: next(clock),
    )
    for _ in range(7):
        monitor.tick()

    drops = log.read_all()
    assert len(drops) == 1
    assert drops[0].cause.startswith("ISP")


def test_flush_open_drop_logs_in_progress_outage():
    internet = FakeReachability([True, False, False])  # goes down and stays down
    router = FakeReachability([True] * 10)
    clock = iter(at(i * 5) for i in range(20))
    log = nw.InMemoryOutageLog()

    monitor = nw.Monitor(
        internet,
        router,
        nw.DropDetector(fail_threshold=2, recover_threshold=1),
        log,
        gateway="192.168.1.1",
        interval=0,
        clock=lambda: next(clock),
    )
    for _ in range(3):
        monitor.tick()
    assert log.read_all() == []  # drop is open, nothing logged yet

    outage = monitor.flush_open_drop()
    assert outage is not None
    assert outage.start == at(5)  # timed from the first failure
    assert len(log.read_all()) == 1

    # Calling it again is a no-op: the drop has been cleared.
    assert monitor.flush_open_drop() is None
    assert len(log.read_all()) == 1


def test_flush_open_drop_returns_none_when_up():
    log = nw.InMemoryOutageLog()
    monitor = nw.Monitor(
        FakeReachability([True]),
        FakeReachability([True]),
        nw.DropDetector(),
        log,
        gateway="192.168.1.1",
        interval=0,
        clock=lambda: at(0),
    )
    assert monitor.flush_open_drop() is None
    assert log.read_all() == []
    out = io.StringIO()
    outage = nw.Outage(
        datetime(2025, 1, 1, 20, 59),
        datetime(2025, 1, 1, 21, 10),
        "ISP / upstream",
    )

    with redirect_stdout(out):
        nw.HourlyReporter().report([outage])
    lines = out.getvalue().splitlines()
    assert "20:00  ###" in lines[22]
    assert "  1.0 min  (1 drops)" in lines[22]
    assert "21:00  ##############################" in lines[23]
    # The drop is counted once, in the hour it began (20:00). Its seconds are
    # still split across both hours, so 21:00 shows the minutes but 0 drops.
    assert " 10.0 min  (0 drops)" in lines[23]


# --- cause attribution ---


def test_cause_for_blames_isp_when_router_ok():
    assert nw.cause_for("192.168.1.1", True).startswith("ISP")


def test_cause_for_blames_local_when_router_down():
    assert nw.cause_for("192.168.1.1", False) == "local network"


def test_cause_for_unknown_when_no_gateway():
    assert nw.cause_for(None, True) == "unknown"


# --- reporters with nothing logged ---


def test_summary_report_handles_empty():
    out = io.StringIO()
    with redirect_stdout(out):
        nw.SummaryReporter().report([])
    assert "No drops logged yet." in out.getvalue()


def test_hourly_report_handles_empty():
    out = io.StringIO()
    with redirect_stdout(out):
        nw.HourlyReporter().report([])
    assert "No drops logged yet." in out.getvalue()


# --- CSV repository round-trips ---


def test_csv_log_round_trips(tmp_path):
    path = tmp_path / "outages.csv"
    log = nw.CsvOutageLog(path)
    start = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, 12, 1, 35, tzinfo=timezone.utc)
    log.append(nw.Outage(start, end, "ISP / upstream"))

    restored = nw.CsvOutageLog(path).read_all()
    assert len(restored) == 1
    assert restored[0].start == start
    assert restored[0].end == end
    assert restored[0].cause == "ISP / upstream"
    assert restored[0].seconds == 95


def test_csv_log_read_all_empty_when_missing(tmp_path):
    log = nw.CsvOutageLog(tmp_path / "nope.csv")
    assert log.read_all() == []


def test_csv_read_skips_unreadable_rows(tmp_path, capsys):
    path = tmp_path / "messy.csv"
    # A good row, a corrupt row (bad dates), then another good row.
    path.write_text(
        "down_at,up_at,seconds,likely_cause\n"
        "2025-06-10T20:03:12+01:00,2025-06-10T20:04:47+01:00,95.0,ISP / upstream\n"
        "not-a-date,also-bad,oops,broken\n"
        "2025-06-10T22:11:05+01:00,2025-06-10T22:11:22+01:00,17.0,local network\n"
    )
    restored = nw.CsvOutageLog(path).read_all()
    assert len(restored) == 2  # the bad row is skipped, the good ones survive
    assert restored[0].cause == "ISP / upstream"
    assert restored[1].cause == "local network"
    assert "skipping unreadable row" in capsys.readouterr().err


# --- timezone handling ---


def test_duration_is_correct_across_a_dst_fall_back():
    # 01:30 happens twice when the clocks go back: once at -04:00, once at
    # -05:00. The real gap is one hour. Aware subtraction must see that.
    edt = timezone(timedelta(hours=-4))
    est = timezone(timedelta(hours=-5))
    outage = nw.Outage(
        datetime(2025, 11, 2, 1, 30, tzinfo=edt),
        datetime(2025, 11, 2, 1, 30, tzinfo=est),
        "ISP / upstream",
    )
    assert outage.seconds == 3600


def test_ensure_aware_pins_naive_to_local():
    naive = datetime(2025, 1, 1, 12, 0, 0)
    result = nw.ensure_aware(naive)
    assert result.tzinfo is not None


def test_ensure_aware_leaves_aware_untouched():
    aware = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert nw.ensure_aware(aware) is aware


def test_csv_read_normalizes_naive_timestamps(tmp_path):
    path = tmp_path / "legacy.csv"
    # A hand-edited / older CSV with no timezone offset on the timestamps.
    path.write_text(
        "down_at,up_at,seconds,likely_cause\n"
        "2025-06-10T20:03:12,2025-06-10T20:04:47,95.0,ISP / upstream\n"
    )
    restored = nw.CsvOutageLog(path).read_all()
    assert len(restored) == 1
    assert restored[0].start.tzinfo is not None
    assert restored[0].end.tzinfo is not None
    assert restored[0].seconds == 95
