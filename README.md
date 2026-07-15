# net-watch

A command-line tool that logs every time your internet drops — so you have hard evidence when you talk to your provider.

Works on **Linux**, **macOS**, and **Windows**. No extra packages needed. Pure Python 3.9+.

---

## Why this exists

"It drops a lot" is easy to ignore. A CSV with timestamps, durations, and likely causes is not.

Every logged drop also says who probably caused it:

- **ISP / upstream** — your router was fine but the internet was gone. This is the useful one for complaints.
- **local network** — your own router or cable was unreachable too. Worth checking at home first.
- **unknown** — the tool could not find your router's IP (rare).

---

## Files

```
netwatch.py           # the main script
config.py             # settings you can edit (targets, timing, thresholds, version)
tests.py              # pytest tests (no real network needed)
net-watch.service     # systemd unit for running it on a Linux server or Raspberry Pi
net_outages.csv       # created automatically when the first drop is logged
docs/                 # operational notes and runbook pointer
deploy.sh             # generic Ansible deploy wrapper (see below)
```

### Deploying with Ansible

`deploy.sh` is deployment-agnostic — it takes no hardcoded paths, hosts, or
usernames, so it's safe to keep in this public repo. Point it at your own
Ansible project (with a `net_watch`-tagged role/play) and host:

```bash
NET_WATCH_IAC_DIR=/path/to/your/ansible/project \
NET_WATCH_HOST=user@host \
./deploy.sh
```

It runs a `--check --diff` dry run first, asks for confirmation before
applying for real (skip with `--yes`), then checks the service status over
SSH. Run `./deploy.sh --help` for all options.

For running and querying a real deployment, see [docs/](docs/).

---

## Quick start

```bash
# watch and log (runs until you press Ctrl+C)
python3 netwatch.py

# see totals
python3 netwatch.py --summary

# see drops grouped by hour of day
python3 netwatch.py --hourly
```

On Windows use `python` instead of `python3`.

---

## What it logs

Every drop gets one row in `net_outages.csv`:

```
down_at,up_at,seconds,likely_cause
2025-06-10T20:03:12+01:00,2025-06-10T20:04:47+01:00,95.0,ISP / upstream
2025-06-10T22:11:05+01:00,2025-06-10T22:11:22+01:00,17.0,local network
```

The file opens straight in a spreadsheet (Excel, LibreOffice, Google Sheets). You can sort by duration, filter by cause, add up total downtime — whatever you need for your complaint.

---

## Reports

### Summary

```
$ python3 netwatch.py --summary

Drops logged:     14
Total time down:  18.3 min (1098s)
Longest drop:     312s at 2025-06-10 20:03
Likely ISP fault: 11 of 14
```

### Hourly chart

```
$ python3 netwatch.py --hourly

Drops by hour of day (local time):

00:00  ##                              0.4 min  (1 drop)
...
20:00  ##############################  8.1 min  (5 drops)
21:00  ##################              4.3 min  (3 drops)
22:00  ####                            1.1 min  (2 drops)
```

If it is always bad at the same time of day, that chart makes the pattern very obvious.

### Daily chart

```
$ python3 netwatch.py --daily

Drops by day (local time):

2026-06-10  ##############################    5.9 min  (2 drops)
2026-06-20  ##########                        2.0 min  (1 drop)
```

Good for showing "it failed on N separate days". A drop is counted once on the
day it began; its downtime is split across every day it spanned.

### Hand-to-your-ISP report

A single, self-contained summary you can read out, paste into a letter, or save
to a file. Pair it with the date flags below to scope it to your complaint
period.

```
$ python3 netwatch.py --report --since 2026-06-01 --until 2026-06-30

net-watch outage report
Period:           2026-06-01 to 2026-06-30
Drops logged:     3
Total time down:  7.9 min (472s)
Likely ISP fault: 2 of 3 (67%)
Longest drop:     335s at 2026-06-10 20:03

Worst 3 outages:
  2026-06-10 20:03     335s  ISP / upstream
  2026-06-20 09:00     120s  ISP / upstream
  2026-06-10 22:11      17s  local network
```

Save it to a file to attach to an email:

```bash
python3 netwatch.py --report --last 30d > complaint.txt
```

### Narrowing a report to a period

All four reports accept the same date filters:

```bash
python3 netwatch.py --summary --last 7d          # the last 7 days
python3 netwatch.py --hourly --since 2026-06-01  # from a date onward
python3 netwatch.py --daily --since 2026-06-01 --until 2026-06-30
```

---

## Options

| Flag | Default | What it does |
|------|---------|--------------|
| `--interval N` | `5` | Seconds between checks. Lower = more precise timing. Higher = lighter on the line. |
| `--fails N` | `2` | How many failures in a row before it counts as a real drop. `1` is most sensitive. `3` ignores more flicker. |
| `--logfile PATH` | `net_outages.csv` | Where to write the CSV log. Use an absolute path when running as a service. |
| `--summary` | — | Print totals and exit. |
| `--hourly` | — | Print the hour-by-hour chart and exit. |
| `--daily` | — | Print the day-by-day chart and exit. |
| `--report` | — | Print a hand-to-your-ISP summary and exit. |
| `--since DATE` | — | Reports only: include drops on or after `YYYY-MM-DD`. |
| `--until DATE` | — | Reports only: include drops on or before `YYYY-MM-DD` (inclusive). |
| `--last WINDOW` | — | Reports only: include drops from the last `7d` (days) or `24h` (hours). |
| `--version` | — | Print the version and exit. |

Example — check every 3 seconds, need 3 failures before believing a drop:

```bash
python3 netwatch.py --interval 3 --fails 3
```

---

## Settings

The defaults live in `config.py`. Edit that one file to change them for good:

| Setting | Default | What it does |
|---------|---------|--------------|
| `TARGETS` | Cloudflare + Google DNS | The `(host, port)` pairs probed to decide if the internet is up. |
| `CHECK_INTERVAL` | `5` | Seconds between checks. |
| `SOCKET_TIMEOUT` | `2.0` | Seconds to wait for each target. |
| `FAIL_THRESHOLD` | `2` | Failures in a row before a real drop. |
| `RECOVER_THRESHOLD` | `1` | Successes in a row before recovery. |
| `LOG_FILE` | `net_outages.csv` | Default CSV path. |
| `VERSION` | — | The release version. |

The `--interval`, `--fails` and `--logfile` flags override the matching
settings for a single run, so you don't have to edit the file for one-offs.

---

## How it decides if you are down

The tool does **not** ping a website by name, because DNS can lie. Instead it tries to open a raw TCP socket to two very reliable DNS servers:

- Cloudflare `1.1.1.1:53`
- Google `8.8.8.8:53`

If **either** answers, you are up. Both must fail before the tool considers you down.

When the internet looks down, it also pings your **router** (the default gateway). This is how it splits the blame:

```
Internet down + router OK  →  ISP / upstream
Internet down + router down  →  local network
```

---

## Debounce — why tiny blips are ignored

A single failed check does not log a drop straight away. The tool waits for `--fails` failures in a row (default 2) before calling it a real drop.

With 5-second checks and `--fails 2`, the smallest drop you can log is about **10 seconds**. Anything shorter is treated as normal internet jitter and thrown away.

The drop timer starts at the **first** failure, not the `--fails`-th one, so the duration is always honest.

---

## Run it on a Raspberry Pi (recommended)

The Pi is the best place to run this. It is on all day, uses almost no power, and catches drops that happen while your laptop is asleep.

**Copy the files:**

```bash
mkdir -p /home/pi/net-watch
cp netwatch.py config.py net-watch.service /home/pi/net-watch/
```

**Install as a service** (starts automatically on boot, restarts if it crashes):

```bash
# edit net-watch.service first if your username is not "pi"
sudo cp /home/pi/net-watch/net-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now net-watch
```

**Useful commands:**

```bash
sudo systemctl status net-watch     # is it running?
journalctl -u net-watch -f          # watch the live output
sudo systemctl stop net-watch       # stop it
sudo systemctl disable net-watch    # stop it starting on boot
```

The CSV is saved in `/home/pi/net-watch/net_outages.csv`.

---

## Run the tests

```bash
pip install pytest
pytest tests.py -v
```

No real network is touched. The tests feed fake up/down samples and a fake clock, so they run in under a second.

```
test_single_blip_is_ignored                        PASSED
test_real_drop_is_timed_from_first_failure         PASSED
test_recovery_is_timed_from_first_success          PASSED
test_monitor_logs_one_isp_drop                     PASSED
test_monitor_blames_local_when_router_also_down    PASSED
```

---

## Design (for the curious)

The code is split into small parts that each do one job. This makes it easy to change one thing without breaking another.

| Part | Pattern | Job |
|------|---------|-----|
| `SocketReachability` / `RouterReachability` | Strategy | "Can I reach X?" One class per check. |
| `DropDetector` | State machine | Turns up/down samples into real drops. Handles debounce. |
| `CsvOutageLog` / `InMemoryOutageLog` | Repository | Where drops are saved. CSV for real use, memory for tests. |
| `SummaryReporter` / `HourlyReporter` / `DailyReporter` / `ReportReporter` | Strategy + Open/Closed | Each report is its own class. Add a new one without touching the rest. |
| `Monitor` | Orchestrator | Ties everything together. Depends only on the interfaces, not the real code. |
| `main()` | Composition root | The one place that builds real objects and wires them up. |

---

## Talking to your provider

Collect at least a week of data, ideally two. Then run the report for that period:

```bash
python3 netwatch.py --report --last 14d > complaint.txt
```

It gives you the numbers providers take seriously:

- Total number of drops
- Total minutes of downtime
- How many drops were "ISP / upstream" (the important number)
- The worst individual outages, with timestamps

Add the hourly or daily chart if a time-of-day or day-by-day pattern helps your
case. That is a solid, factual complaint — numbers carry more weight than
descriptions.

---

## Requirements

- Python 3.9 or newer
- No third-party packages needed
- `pytest` only if you want to run the tests