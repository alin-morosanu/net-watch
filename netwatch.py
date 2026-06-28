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
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

# --- settings you can change ---
TARGETS: list[tuple[str, int]] = [("1.1.1.1", 53), ("8.8.8.8", 53)]
CHECK_INTERVAL = 5  # seconds between checks
SOCKET_TIMEOUT = 2.0  # seconds to wait for each target
FAIL_THRESHOLD = 2  # failures in a row before we believe it is a real drop
RECOVER_THRESHOLD = 1  # successes in a row before we call it back
LOG_FILE = Path("net_outages.csv")

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
        with self._path.open(newline="") as f:
            return [
                Outage(
                    ensure_aware(datetime.fromisoformat(row["down_at"])),
                    ensure_aware(datetime.fromisoformat(row["up_at"])),
                    row["likely_cause"],
                )
                for row in csv.DictReader(f)
            ]


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
            current = o.start
            while current < o.end:
                next_hour = current.replace(
                    minute=0, second=0, microsecond=0
                ) + timedelta(hours=1)
                segment_end = min(next_hour, o.end)
                secs[current.hour] += (segment_end - current).total_seconds()
                count[current.hour] += 1
                current = segment_end
        worst = max(secs.values()) or 1.0
        print("Drops by hour of day (local time):\n")
        for hour in range(24):
            bar = "#" * int((secs[hour] / worst) * self.BAR_WIDTH)
            print(
                f"{hour:02d}:00  {bar:<{self.BAR_WIDTH}}  {secs[hour] / 60:5.1f} min  ({count[hour]} drops)"
            )


# =====================================================================
#  The orchestrator
# =====================================================================
@dataclass
class TickResult:
    event: str  # "none" | "drop_started" | "recovered"
    outage: Outage | None = None
    at: datetime | None = None


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

        if transition is not None:
            if transition.to is Connectivity.DOWN:
                self._open_start = transition.at
                self._router_ok_all = router_ok
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

        if self._open_start is not None and not ok:
            self._router_ok_all = self._router_ok_all and router_ok
        return TickResult("none")

    def run_forever(self) -> None:
        where = self._log_path.resolve() if self._log_path else "(in memory)"
        print(f"Watching on {_OS}. Router = {self._gateway or 'unknown'}.")
        print(f"Logging to {where}. Press Ctrl+C to stop.\n")
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
            if self._open_start is not None:  # still down when you stopped
                o = Outage(
                    self._open_start,
                    self._clock(),
                    cause_for(self._gateway, self._router_ok_all),
                )
                self._log.append(o)
                print(f"\nLogged the in-progress drop ({o.seconds:.0f}s).")
            print("Stopped.")


# =====================================================================
#  Composition root — the one place that builds real objects
# =====================================================================
def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Log internet drops.")
    parser.add_argument("--summary", action="store_true", help="show totals and exit")
    parser.add_argument(
        "--hourly", action="store_true", help="show drops by hour and exit"
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

    if args.summary:
        SummaryReporter().report(log.read_all())
        return
    if args.hourly:
        HourlyReporter().report(log.read_all())
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
