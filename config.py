"""net-watch settings — the values you tune from time to time, in one place.

Plain Python, so there is nothing extra to install or parse: edit a value
here and it becomes the new default. The command-line flags still override
the matching settings for a single run, so you rarely need to touch this file
for one-offs:

    --interval  ->  CHECK_INTERVAL
    --fails     ->  FAIL_THRESHOLD
    --logfile   ->  LOG_FILE
"""

from __future__ import annotations

from pathlib import Path

# =====================================================================
#  Release — bump this when you cut a new version
# =====================================================================
VERSION = "0.2.0"

# =====================================================================
#  Settings — safe to edit
# =====================================================================

# What counts as "the internet is up": we open a TCP socket to these
# (host, port) pairs and call it up if ANY of them answers. 1.1.1.1 is
# Cloudflare DNS and 8.8.8.8 is Google DNS — both very reliable.
TARGETS: list[tuple[str, int]] = [("1.1.1.1", 53), ("8.8.8.8", 53)]

# Timing
CHECK_INTERVAL = 3  # seconds between checks
SOCKET_TIMEOUT = 2.0  # seconds to wait for each target

# Debounce — ignore tiny one-off blips
FAIL_THRESHOLD = 2  # failures in a row before we believe a real drop
RECOVER_THRESHOLD = 1  # successes in a row before we call it back up

# Storage
LOG_FILE = Path("net_outages.csv")  # where drops are written (CSV)
