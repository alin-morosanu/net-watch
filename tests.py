"""Tests for net-watch. Run with:  pytest tests.py -v

No real network is touched: we feed fake samples and a fake clock.
"""

import argparse
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest

import config
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
    assert "  1.0 min  (1 drop)" in lines[22]
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


# --- config wiring ---


def test_version_and_defaults_come_from_config():
    assert nw.__version__ == config.VERSION
    assert nw.TARGETS == config.TARGETS
    assert nw.CHECK_INTERVAL == config.CHECK_INTERVAL
    assert nw.FAIL_THRESHOLD == config.FAIL_THRESHOLD
    assert nw.LOG_FILE == config.LOG_FILE


# --- date-range filtering and parsing ---


def _outage_on(day: str, cause: str = "ISP / upstream") -> nw.Outage:
    """A 60-second drop at noon (local time) on the given YYYY-MM-DD."""
    start = datetime.fromisoformat(day + "T12:00:00").astimezone()
    return nw.Outage(start, start + timedelta(seconds=60), cause)


def test_filter_by_range_keeps_only_drops_inside_window():
    outages = [
        _outage_on("2026-06-01"),
        _outage_on("2026-06-10"),
        _outage_on("2026-06-20"),
    ]
    since = nw.parse_date("2026-06-05")
    until = nw.parse_date("2026-06-15") + timedelta(days=1)  # half-open upper bound
    kept = nw.filter_by_range(outages, since, until)
    assert len(kept) == 1
    assert kept[0].start.date().isoformat() == "2026-06-10"


def test_filter_by_range_with_no_bounds_keeps_everything():
    outages = [_outage_on("2026-06-01"), _outage_on("2026-06-20")]
    assert nw.filter_by_range(outages, None, None) == outages


def test_parse_window_days_hours_and_bare_number():
    assert nw.parse_window("7d") == timedelta(days=7)
    assert nw.parse_window("24h") == timedelta(hours=24)
    assert nw.parse_window("30") == timedelta(days=30)  # bare number means days


def test_parse_window_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        nw.parse_window("soon")


def test_parse_date_makes_aware_midnight():
    dt = nw.parse_date("2026-06-01")
    assert dt.tzinfo is not None
    assert (dt.hour, dt.minute, dt.second) == (0, 0, 0)


def test_parse_date_rejects_bad_format():
    with pytest.raises(argparse.ArgumentTypeError):
        nw.parse_date("01/06/2026")


def test_resolve_range_widens_until_to_an_inclusive_day():
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(last=None, since=None, until=nw.parse_date("2026-06-15"))
    since, until = nw._resolve_range(args, parser)
    assert since is None
    assert until == nw.parse_date("2026-06-16")


def test_resolve_range_rejects_last_with_since():
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(
        last=timedelta(days=7), since=nw.parse_date("2026-06-01"), until=None
    )
    with pytest.raises(SystemExit):  # parser.error exits
        nw._resolve_range(args, parser)


# --- daily report ---


def test_daily_report_splits_drop_across_midnight():
    # 23:50 -> 00:10 next day: ten minutes land on each day, counted once.
    start = datetime(2026, 6, 1, 23, 50, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, 0, 10, tzinfo=timezone.utc)
    out = io.StringIO()
    with redirect_stdout(out):
        nw.DailyReporter().report([nw.Outage(start, end, "ISP / upstream")])

    lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("2026-06-0")]
    assert len(lines) == 2
    assert lines[0].startswith("2026-06-01")
    assert "(1 drop)" in lines[0]  # counted on the day it began
    assert lines[1].startswith("2026-06-02")
    assert "(0 drops)" in lines[1]  # only spilled-over seconds, no new drop


def test_daily_report_handles_empty():
    out = io.StringIO()
    with redirect_stdout(out):
        nw.DailyReporter().report([])
    assert "No drops logged yet." in out.getvalue()


# --- hand-to-ISP report ---


def test_report_reporter_shows_period_and_stats():
    o1 = _outage_on("2026-06-10", "ISP / upstream")
    o2 = _outage_on("2026-06-11", "local network")
    since = nw.parse_date("2026-06-01")
    until = nw.parse_date("2026-06-30") + timedelta(days=1)
    out = io.StringIO()
    with redirect_stdout(out):
        nw.ReportReporter(since, until).report([o1, o2])

    text = out.getvalue()
    assert "2026-06-01 to 2026-06-30" in text  # inclusive period, not the half-open end
    assert "Likely ISP fault: 1 of 2 (50%)" in text
    assert "Worst 2 outages:" in text


def test_report_reporter_handles_empty_period():
    since = nw.parse_date("2026-06-01")
    until = nw.parse_date("2026-06-07") + timedelta(days=1)
    out = io.StringIO()
    with redirect_stdout(out):
        nw.ReportReporter(since, until).report([])

    text = out.getvalue()
    assert "2026-06-01 to 2026-06-07" in text
    assert "No drops recorded in this period." in text


def test_parse_window_rejects_zero_and_negative():
    with pytest.raises(argparse.ArgumentTypeError):
        nw.parse_window("0d")
    with pytest.raises(argparse.ArgumentTypeError):
        nw.parse_window("-3d")


def test_resolve_range_rejects_since_after_until():
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(
        last=None, since=nw.parse_date("2026-06-20"), until=nw.parse_date("2026-06-10")
    )
    with pytest.raises(SystemExit):  # parser.error exits
        nw._resolve_range(args, parser)


def test_resolve_range_last_pins_a_concrete_upper_bound():
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(last=timedelta(days=7), since=None, until=None)
    since, until = nw._resolve_range(args, parser)
    assert since is not None and until is not None
    assert until - since == timedelta(days=7)


def test_report_reporter_last_window_names_period_when_empty():
    # --last resolves to (since, now); an empty report should still name the
    # window rather than fall back to "all time".
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(last=timedelta(days=7), since=None, until=None)
    since, until = nw._resolve_range(args, parser)
    out = io.StringIO()
    with redirect_stdout(out):
        nw.ReportReporter(since, until).report([])
    text = out.getvalue()
    assert "all time" not in text
    assert "No drops recorded in this period." in text


def test_daily_report_survives_zero_length_outage():
    # A hand-edited CSV can yield up_at == down_at; the chart must not crash.
    ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    out = io.StringIO()
    with redirect_stdout(out):
        nw.DailyReporter().report([nw.Outage(ts, ts, "unknown")])
    assert "Drops by day" in out.getvalue()  # printed cleanly, no exception


def test_filter_flags_require_a_report_mode(monkeypatch):
    # --last without a report mode is a usage error, caught before any network.
    monkeypatch.setattr(sys, "argv", ["netwatch.py", "--last", "7d"])
    with pytest.raises(SystemExit):
        nw.main()
