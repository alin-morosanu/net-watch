#!/usr/bin/env python3
"""net-watch — log internet drops so you can show your provider.

Works on Linux, macOS and Windows.

    python netwatch.py                  # watch and log drops
    python netwatch.py --summary        # totals from the log
    python netwatch.py --hourly         # drops grouped by hour of day
    python netwatch.py --interval 3 --fails 3   # tune it

Design notes (the patterns, in plain words):
  * Reachability  -> Strategy. "Can I reach X?" One small object per kind
                     of check (the internet, the router). Easy to fake in tests.
  * DropDetector  -> State machine. Turns a stream of up/down samples into
                     real drops, and ignores tiny one-off blips (debounce).
  * OutageLog     -> Repository. Where drops are saved. CSV today, could be
                     SQLite tomorrow, in-memory in tests.
  * Reporter      -> Strategy + Open/Closed. Add a new report without
                     touching anything else.
  * Monitor       -> the orchestrator. Depends only on the interfaces above
                     (Dependency Inversion), so it is tiny and testable.
  * main()        -> composition root. The ONE place that builds the real
                     objects and wires them together.
"""

from __future__ import annotations

import argparse
import csv
import platform
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

# Tunable defaults live in config.py — one place to edit the values that
# change from time to time. The command-line flags (--interval, --fails,
# --logfile) still override the matching settings at runtime.
from config import (
    CHECK_INTERVAL,
    FAIL_THRESHOLD,
    LOG_FILE,
    RECOVER_THRESHOLD,
    SOCKET_TIMEOUT,
    TARGETS,
    VERSION,
)

__version__ = VERSION

_OS = platform.system()  # "Linux", "Darwin" (Mac), or "Windows"


def now() -> datetime:
    """Local time, with timezone info. Injected as a 'clock' so tests can fake it."""
    return datetime.now().astimezone()


def ensure_aware(dt: datetime) -> datetime:
    """Guarantee a timezone-aware datetime.

    Timestamps we create are always aware (see ``now``), but a CSV that was
    hand-edited or written by an older build may hold a bare, naive value.
    A naive value is read as local wall-clock time and pinned to the local
    zone, so every Outage in memory is aware. That keeps durations correct
    even when the recorded times straddle a DST change, because aware
    subtraction works on the underlying instants, not the wall clock.
    """
    return dt if dt.tzinfo is not None else dt.astimezone()


# =====================================================================
#  Value object — a finished drop
# =====================================================================
@dataclass(frozen=True)
class Outage:
    start: datetime
    end: datetime
    cause: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


# =====================================================================
#  Interfaces (Protocols) — the seams of the design
# =====================================================================
class Reachability(Protocol):
    def is_reachable(self) -> bool: ...


class OutageLog(Protocol):
    def append(self, outage: Outage) -> None: ...
    def read_all(self) -> list[Outage]: ...


class Reporter(Protocol):
    def report(self, outages: list[Outage]) -> None: ...


# =====================================================================
#  Reachability strategies
# =====================================================================
def can_reach(host: str, port: int, timeout: float) -> bool:
    """True if we can open a TCP socket to host:port. Same on every OS."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class SocketReachability:
    """Internet check: up if at least ONE target answers."""

    def __init__(self, targets: list[tuple[str, int]], timeout: float) -> None:
        self._targets = targets
        self._timeout = timeout

    def is_reachable(self) -> bool:
        return any(can_reach(h, p, self._timeout) for h, p in self._targets)


class RouterReachability:
    """Router check: ping the gateway. False if we never found the gateway."""

    def __init__(self, gateway: str | None, timeout_s: int = 1) -> None:
        self._gateway = gateway
        self._timeout_s = timeout_s

    def is_reachable(self) -> bool:
        return ping(self._gateway, self._timeout_s) if self._gateway else False


# =====================================================================
#  Per-OS helpers (gateway + ping). Chosen by the running OS.
# =====================================================================
def _gateway_linux() -> str | None:
    tokens = subprocess.run(
        ["ip", "route", "show", "default"],
        capture_output=True,
        text=True,
        timeout=3,
    ).stdout.split()
    if "via" in tokens:
        return tokens[tokens.index("via") + 1]
    return None


def _gateway_macos() -> str | None:
    out = subprocess.run(
        ["route", "-n", "get", "default"],
        capture_output=True,
        text=True,
        timeout=3,
    ).stdout
    for line in out.splitlines():
        if line.strip().startswith("gateway:"):
            return line.split(":", 1)[1].strip()
    return None


def _gateway_windows() -> str | None:
    out = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0').NextHop",
        ],
        capture_output=True,
        text=True,
        timeout=8,
    ).stdout
    for line in out.splitlines():
        ip = line.strip()
        if ip and ip != "0.0.0.0" and "." in ip and ":" not in ip:
            return ip
    return None


def find_gateway() -> str | None:
    """Find the router's IP. Returns None if we can't work it out."""
    try:
        if _OS == "Windows":
            return _gateway_windows()
        if _OS == "Darwin":
            return _gateway_macos()
        return _gateway_linux()
    except Exception as exc:
        print(f"warning: could not determine router IP: {exc}", file=sys.stderr)
        return None


def ping(host: str, timeout_s: int = 1) -> bool:
    """Ping once to check the router is alive. Flags differ per OS."""
    if _OS == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_s * 1000), host]
    elif _OS == "Darwin":
        cmd = ["ping", "-c", "1", "-t", str(timeout_s), host]
    else:  # Linux
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), host]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 3,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    # Windows can exit 0 even when unreachable; a real reply shows "TTL=".
    if _OS == "Windows" and "TTL=" not in result.stdout.upper():
        return False
    return True


def cause_for(gateway: str | None, router_ok: bool) -> str:
    """Best guess at who caused the drop."""
    if gateway is None:
        return "unknown"
    return "ISP / upstream" if router_ok else "local network"


# =====================================================================
#  State machine — debounce. Turns up/down samples into real drops.
# =====================================================================
class Connectivity(Enum):
    UP = auto()
    DOWN = auto()


@dataclass(frozen=True)
class Transition:
    to: Connectivity
    at: datetime


@dataclass
class DropDetector:
    """Only believes a drop after `fail_threshold` failures in a row, so a
    single jittery packet is ignored. The drop is timed from the FIRST
    failure (honest), and ends at the FIRST success (does not over-count).

    O(1) time per sample, O(1) memory."""

    fail_threshold: int = FAIL_THRESHOLD
    recover_threshold: int = RECOVER_THRESHOLD
    _state: Connectivity = field(default=Connectivity.UP, init=False)
    _fails: int = field(default=0, init=False)
    _oks: int = field(default=0, init=False)
    _first_fail_at: datetime | None = field(default=None, init=False)
    _first_ok_at: datetime | None = field(default=None, init=False)

    def update(self, ok: bool, ts: datetime) -> Transition | None:
        if self._state is Connectivity.UP:
            if ok:
                self._fails = 0
                self._first_fail_at = None
                return None
            self._fails += 1
            if self._first_fail_at is None:
                self._first_fail_at = ts
            if self._fails >= self.fail_threshold:
                start = self._first_fail_at
                self._state = Connectivity.DOWN
                self._oks = 0
                self._first_ok_at = None
                return Transition(Connectivity.DOWN, start)
            return None

        # state is DOWN
        if not ok:
            self._oks = 0
            self._first_ok_at = None
            return None
        self._oks += 1
        if self._first_ok_at is None:
            self._first_ok_at = ts
        if self._oks >= self.recover_threshold:
            end = self._first_ok_at
            self._state = Connectivity.UP
            self._fails = 0
            self._first_fail_at = None
            return Transition(Connectivity.UP, end)
        return None


# =====================================================================
#  Repositories — where drops live
# =====================================================================
class CsvOutageLog:
    """Saves drops to a CSV file (opens straight in a spreadsheet)."""

    HEADER = ["down_at", "up_at", "seconds", "likely_cause"]

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, outage: Outage) -> None:
        new_file = not self._path.exists()
        with self._path.open("a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(self.HEADER)
            writer.writerow(
                [
                    outage.start.isoformat(),
                    outage.end.isoformat(),
                    round(outage.seconds, 1),
                    outage.cause,
                ]
            )

    def read_all(self) -> list[Outage]:
        if not self._path.exists():
            return []
        outages: list[Outage] = []
        with self._path.open(newline="") as f:
            for row in csv.DictReader(f):
                try:
                    outages.append(
                        Outage(
                            ensure_aware(datetime.fromisoformat(row["down_at"])),
                            ensure_aware(datetime.fromisoformat(row["up_at"])),
                            row["likely_cause"],
                        )
                    )
                except (KeyError, ValueError) as exc:
                    # A hand-edited or half-written row should not sink the
                    # whole report. Skip it, keep the good data, say so.
                    print(
                        f"warning: skipping unreadable row in {self._path}: {exc}",
                        file=sys.stderr,
                    )
        return outages


class InMemoryOutageLog:
    """Same shape, kept in memory. Handy for tests."""

    def __init__(self) -> None:
        self._items: list[Outage] = []

    def append(self, outage: Outage) -> None:
        self._items.append(outage)

    def read_all(self) -> list[Outage]:
        return list(self._items)


# =====================================================================
#  Reporters — add new ones without touching anything else
# =====================================================================
def _drops_label(n: int) -> str:
    """'1 drop' / '2 drops' — gets the singular right."""
    return f"{n} drop" if n == 1 else f"{n} drops"


class SummaryReporter:
    def report(self, outages: list[Outage]) -> None:
        if not outages:
            print("No drops logged yet.")
            return
        total = sum(o.seconds for o in outages)
        longest = max(outages, key=lambda o: o.seconds)
        isp = sum(1 for o in outages if o.cause.startswith("ISP"))
        print(f"Drops logged:     {len(outages)}")
        print(f"Total time down:  {total / 60:.1f} min ({total:.0f}s)")
        print(
            f"Longest drop:     {longest.seconds:.0f}s at {longest.start:%Y-%m-%d %H:%M}"
        )
        print(f"Likely ISP fault: {isp} of {len(outages)}")


class HourlyReporter:
    """Drops grouped by hour of day, as a little text chart.

    O(n) over the drops, O(1) memory (just 24 buckets)."""

    BAR_WIDTH = 30

    def report(self, outages: list[Outage]) -> None:
        if not outages:
            print("No drops logged yet.")
            return
        secs = {h: 0.0 for h in range(24)}
        count = {h: 0 for h in range(24)}
        for o in outages:
            # Count each drop once, in the hour it began, so the totals match
            # the real number of outages. Seconds are still split across the
            # hours the drop spanned, so the chart shows when downtime landed.
            count[o.start.hour] += 1
            current = o.start
            while current < o.end:
                next_hour = current.replace(
                    minute=0, second=0, microsecond=0
                ) + timedelta(hours=1)
                segment_end = min(next_hour, o.end)
                secs[current.hour] += (segment_end - current).total_seconds()
                current = segment_end
        worst = max(secs.values()) or 1.0
        print("Drops by hour of day (local time):\n")
        for hour in range(24):
            bar = "#" * int((secs[hour] / worst) * self.BAR_WIDTH)
            print(
                f"{hour:02d}:00  {bar:<{self.BAR_WIDTH}}  {secs[hour] / 60:5.1f} min  ({_drops_label(count[hour])})"
            )


class DailyReporter:
    """Drops grouped by calendar day, as a little text chart.

    Same convention as the hourly chart: a drop is counted once on the day it
    began, but its seconds are split across every day it spanned (so an
    outage over midnight shows downtime on both days). O(n) over the drops."""

    BAR_WIDTH = 30

    def report(self, outages: list[Outage]) -> None:
        if not outages:
            print("No drops logged yet.")
            return
        secs: dict[date, float] = {}
        count: dict[date, int] = {}
        for o in outages:
            count[o.start.date()] = count.get(o.start.date(), 0) + 1
            current = o.start
            while current < o.end:
                next_midnight = current.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                segment_end = min(next_midnight, o.end)
                day = current.date()
                secs[day] = secs.get(day, 0.0) + (segment_end - current).total_seconds()
                current = segment_end
        # `secs` is only empty if every outage had end <= start (e.g. a
        # hand-edited CSV); `default` keeps the chart from crashing on `max`.
        worst = max(secs.values(), default=0.0) or 1.0
        print("Drops by day (local time):\n")
        for day in sorted(secs):
            bar = "#" * int((secs[day] / worst) * self.BAR_WIDTH)
            print(
                f"{day:%Y-%m-%d}  {bar:<{self.BAR_WIDTH}}  {secs[day] / 60:5.1f} min  ({_drops_label(count.get(day, 0))})"
            )


class ReportReporter:
    """A single, self-contained summary you can hand to your ISP.

    Plain text on stdout, so you can read it, paste it into a letter, or
    redirect it to a file with '> complaint.txt'. The window it was asked for
    is shown so the period is unambiguous, even when no drops fell inside it.
    """

    WORST_N = 5

    def __init__(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> None:
        self._since = since
        self._until = until  # exclusive upper bound, as resolved by the CLI

    def _period(self, outages: list[Outage]) -> str:
        start = self._since or (min(o.start for o in outages) if outages else None)
        if self._until is not None:
            end = self._until - timedelta(seconds=1)  # back to the inclusive day
        elif outages:
            end = max(o.end for o in outages)
        else:
            end = None
        if start is None or end is None:
            return "all time"
        return f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"

    def report(self, outages: list[Outage]) -> None:
        print("net-watch outage report")
        print(f"Period:           {self._period(outages)}")
        if not outages:
            print("\nNo drops recorded in this period.")
            return
        total = sum(o.seconds for o in outages)
        longest = max(outages, key=lambda o: o.seconds)
        isp = sum(1 for o in outages if o.cause.startswith("ISP"))
        pct = isp / len(outages) * 100
        print(f"Drops logged:     {len(outages)}")
        print(f"Total time down:  {total / 60:.1f} min ({total:.0f}s)")
        print(f"Likely ISP fault: {isp} of {len(outages)} ({pct:.0f}%)")
        print(
            f"Longest drop:     {longest.seconds:.0f}s at {longest.start:%Y-%m-%d %H:%M}"
        )
        worst = sorted(outages, key=lambda o: o.seconds, reverse=True)[: self.WORST_N]
        print(f"\nWorst {len(worst)} outages:")
        for o in worst:
            print(f"  {o.start:%Y-%m-%d %H:%M}  {o.seconds:6.0f}s  {o.cause}")


# =====================================================================
#  The orchestrator
# =====================================================================
@dataclass
class TickResult:
    event: str  # "none" | "drop_started" | "recovered"
    outage: Outage | None = None
    at: datetime | None = None


def _raise_keyboard_interrupt(*_: object) -> None:
    """Signal handler: turn SIGTERM into the same KeyboardInterrupt we already
    handle, so a service stop/restart still records an in-progress drop."""
    raise KeyboardInterrupt


class Monitor:
    """Ties the parts together. Depends only on the interfaces above."""

    def __init__(
        self,
        internet: Reachability,
        router: Reachability,
        detector: DropDetector,
        log: OutageLog,
        *,
        gateway: str | None,
        interval: int = CHECK_INTERVAL,
        clock: Callable[[], datetime] = now,
        log_path: Path | None = None,
    ) -> None:
        self._internet = internet
        self._router = router
        self._detector = detector
        self._log = log
        self._gateway = gateway
        self._interval = interval
        self._clock = clock
        self._log_path = log_path
        self._open_start: datetime | None = None
        self._router_ok_all = True

    def tick(self) -> TickResult:
        """One check. No sleeping. This is what tests drive."""
        ts = self._clock()
        ok = self._internet.is_reachable()
        router_ok = self._router.is_reachable() if not ok else True
        transition = self._detector.update(ok, ts)

        # Track router health across the current run of failures, starting at
        # the very first failure (where the outage is timestamped), not just
        # the tick that confirms the drop. A clean sample outside a drop
        # resets the run so an earlier blip can't poison a later attribution.
        if not ok:
            self._router_ok_all = self._router_ok_all and router_ok
        elif self._open_start is None:
            self._router_ok_all = True

        if transition is not None:
            if transition.to is Connectivity.DOWN:
                self._open_start = transition.at
                return TickResult("drop_started", at=transition.at)
            # came back up
            assert self._open_start is not None
            outage = Outage(
                self._open_start,
                transition.at,
                cause_for(self._gateway, self._router_ok_all),
            )
            self._log.append(outage)
            self._open_start = None
            self._router_ok_all = True
            return TickResult("recovered", outage=outage, at=transition.at)

        return TickResult("none")

    def flush_open_drop(self) -> Outage | None:
        """Log a still-open drop (e.g. on shutdown) and return it, or None if
        nothing was in progress. Safe to call twice: the drop is cleared once
        logged, so a second call is a no-op."""
        if self._open_start is None:
            return None
        outage = Outage(
            self._open_start,
            self._clock(),
            cause_for(self._gateway, self._router_ok_all),
        )
        self._log.append(outage)
        self._open_start = None
        self._router_ok_all = True
        return outage

    def run_forever(self) -> None:
        where = self._log_path.resolve() if self._log_path else "(in memory)"
        print(f"Watching on {_OS}. Router = {self._gateway or 'unknown'}.")
        print(f"Logging to {where}. Press Ctrl+C to stop.\n")

        # systemd sends SIGTERM on stop/restart; treat it like Ctrl+C so an
        # in-progress drop is still recorded before we exit.
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

        try:
            while True:
                result = self.tick()
                if result.event == "drop_started":
                    print(f"[{result.at:%H:%M:%S}] DROP")
                elif result.event == "recovered":
                    assert result.outage is not None
                    o = result.outage
                    print(
                        f"[{result.at:%H:%M:%S}] back up — was down {o.seconds:.0f}s ({o.cause})"
                    )
                time.sleep(self._interval)
        except KeyboardInterrupt:
            outage = self.flush_open_drop()
            if outage is not None:  # still down when you stopped
                print(f"\nLogged the in-progress drop ({outage.seconds:.0f}s).")
            print("Stopped.")


# =====================================================================
#  Composition root — the one place that builds real objects
# =====================================================================
def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_date(value: str) -> datetime:
    """A YYYY-MM-DD date as a local-aware datetime at midnight."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").astimezone()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a date like 2026-06-01, got {value!r}"
        )


def parse_window(value: str) -> timedelta:
    """A look-back window like '7d' (days) or '24h' (hours). A bare number is days."""
    text = value.strip().lower()
    unit = "d"
    if text[-1:] in {"d", "h"}:
        unit, text = text[-1], text[:-1]
    try:
        amount = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a window like 7d or 24h, got {value!r}"
        )
    if amount < 1:
        raise argparse.ArgumentTypeError("window must be at least 1")
    return timedelta(days=amount) if unit == "d" else timedelta(hours=amount)


def filter_by_range(
    outages: list[Outage],
    since: datetime | None,
    until: datetime | None,
) -> list[Outage]:
    """Keep drops that began within [since, until). Each bound is aware or None."""
    result = outages
    if since is not None:
        result = [o for o in result if o.start >= since]
    if until is not None:
        result = [o for o in result if o.start < until]
    return result


def _resolve_range(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[datetime | None, datetime | None]:
    """Turn the CLI date flags into an aware, half-open [since, until) window."""
    if args.last is not None:
        if args.since is not None or args.until is not None:
            parser.error("--last cannot be combined with --since/--until")
        # Pin both ends so the report names the real window even with no drops.
        current = now()
        return current - args.last, current
    since = args.since
    # --until names an inclusive day; widen it to the start of the next day.
    until = args.until + timedelta(days=1) if args.until is not None else None
    if since is not None and until is not None and since >= until:
        parser.error("--since must be on or before --until")
    return since, until


def main() -> None:
    parser = argparse.ArgumentParser(description="Log internet drops.")
    parser.add_argument(
        "--version", action="version", version=f"net-watch {__version__}"
    )

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--summary", action="store_true", help="show totals and exit")
    modes.add_argument(
        "--hourly", action="store_true", help="show drops by hour of day and exit"
    )
    modes.add_argument(
        "--daily", action="store_true", help="show drops by calendar day and exit"
    )
    modes.add_argument(
        "--report",
        action="store_true",
        help="print a hand-to-your-ISP summary and exit",
    )

    parser.add_argument(
        "--since",
        type=parse_date,
        metavar="YYYY-MM-DD",
        help="only include drops on or after this date (reports only)",
    )
    parser.add_argument(
        "--until",
        type=parse_date,
        metavar="YYYY-MM-DD",
        help="only include drops on or before this date (reports only)",
    )
    parser.add_argument(
        "--last",
        type=parse_window,
        metavar="7d",
        help="only include drops from the last window, e.g. 7d or 24h (reports only)",
    )

    parser.add_argument(
        "--interval",
        type=positive_int,
        default=CHECK_INTERVAL,
        help="seconds between checks",
    )
    parser.add_argument(
        "--fails",
        type=positive_int,
        default=FAIL_THRESHOLD,
        help="failures in a row before a real drop",
    )
    parser.add_argument(
        "--logfile",
        type=Path,
        default=LOG_FILE,
        help="where to write the CSV log (default: net_outages.csv)",
    )
    args = parser.parse_args()

    log = CsvOutageLog(args.logfile)

    reporting = args.summary or args.hourly or args.daily or args.report
    filtering = (
        args.since is not None or args.until is not None or args.last is not None
    )
    if filtering and not reporting:
        parser.error(
            "--since/--until/--last only apply to a report "
            "(--summary, --hourly, --daily or --report)"
        )

    if reporting:
        since, until = _resolve_range(args, parser)
        outages = filter_by_range(log.read_all(), since, until)
        if args.summary:
            SummaryReporter().report(outages)
        elif args.hourly:
            HourlyReporter().report(outages)
        elif args.daily:
            DailyReporter().report(outages)
        else:  # args.report
            ReportReporter(since, until).report(outages)
        return

    gateway = find_gateway()
    monitor = Monitor(
        internet=SocketReachability(TARGETS, SOCKET_TIMEOUT),
        router=RouterReachability(gateway),
        detector=DropDetector(
            fail_threshold=args.fails, recover_threshold=RECOVER_THRESHOLD
        ),
        log=log,
        gateway=gateway,
        interval=args.interval,
        log_path=args.logfile,
    )
    monitor.run_forever()


if __name__ == "__main__":
    main()
