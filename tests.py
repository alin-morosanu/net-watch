"""Tests for netwatch. Run with:  pytest tests.py -v

No real network is touched: we feed fake samples and a fake clock.
"""

import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta

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


def test_hourly_report_splits_drop_across_hour_boundary():
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
    assert " 10.0 min  (1 drops)" in lines[23]


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
    log.append(nw.Outage(at(0), at(95), "ISP / upstream"))

    restored = nw.CsvOutageLog(path).read_all()
    assert len(restored) == 1
    assert restored[0].start == at(0)
    assert restored[0].end == at(95)
    assert restored[0].cause == "ISP / upstream"
    assert restored[0].seconds == 95


def test_csv_log_read_all_empty_when_missing(tmp_path):
    log = nw.CsvOutageLog(tmp_path / "nope.csv")
    assert log.read_all() == []
