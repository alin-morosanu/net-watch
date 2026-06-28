# net-watch

A command-line tool that logs every time your internet drops — so you have hard evidence when you talk to your provider.

Works on **Linux**, **macOS**, and **Windows**. No extra packages needed. Pure Python 3.14+.

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
tests.py              # pytest tests (no real network needed)
net-watch.service     # systemd unit for running it on a Linux server or Raspberry Pi
net_outages.csv       # created automatically when the first drop is logged
```

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

00:00  ##                              0.4 min  (1 drops)
...
20:00  ##############################  8.1 min  (5 drops)
21:00  ##################              4.3 min  (3 drops)
22:00  ####                            1.1 min  (2 drops)
```

If it is always bad at the same time of day, that chart makes the pattern very obvious.

---

## Options

| Flag | Default | What it does |
|------|---------|--------------|
| `--interval N` | `5` | Seconds between checks. Lower = more precise timing. Higher = lighter on the line. |
| `--fails N` | `2` | How many failures in a row before it counts as a real drop. `1` is most sensitive. `3` ignores more flicker. |
| `--logfile PATH` | `net_outages.csv` | Where to write the CSV log. Use an absolute path when running as a service. |
| `--summary` | — | Print totals and exit. |
| `--hourly` | — | Print the hour-by-hour chart and exit. |

Example — check every 3 seconds, need 3 failures before believing a drop:

```bash
python3 netwatch.py --interval 3 --fails 3
```

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
cp netwatch.py /home/pi/net-watch/
```

**Install as a service** (starts automatically on boot, restarts if it crashes):

```bash
# edit net-watch.service first if your username is not "pi"
sudo cp net-watch.service /etc/systemd/system/
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
| `SummaryReporter` / `HourlyReporter` | Strategy + Open/Closed | Each report is its own class. Add a new one without touching the rest. |
| `Monitor` | Orchestrator | Ties everything together. Depends only on the interfaces, not the real code. |
| `main()` | Composition root | The one place that builds real objects and wires them up. |

---

## Talking to your provider

Collect at least a week of data, ideally two. Then run both reports and write down:

- Total number of drops
- Total minutes of downtime
- How many drops were "ISP / upstream" (the important number)
- The worst hours from the hourly chart

That is a solid, factual complaint. Providers take numbers more seriously than descriptions.

---

## Requirements

- Python 3.8 or newer
- No third-party packages needed
- `pytest` only if you want to run the tests