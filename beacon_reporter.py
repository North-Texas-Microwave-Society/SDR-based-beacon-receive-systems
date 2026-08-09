#!/usr/bin/env python3
"""
NTMS Beacon Reporter
=====================
Watches beacon_log.csv for new rows and POSTs each one to the NTMS Prop API.

Designed to run alongside beacon_monitor.py — either as a separate process
or started automatically. Tracks its position in the CSV so it never sends
the same row twice, survives restarts (position is saved to a state file),
and retries failed POSTs with exponential backoff.

Usage:
  python beacon_reporter.py [options]

  --input           CSV file to watch
  --api             NTMS API endpoint URL
  --monitor-token   Monitor token from prop.w5isp.com (or NTMS_MONITOR_TOKEN)
  --beacon-id       Beacon UUID (or NTMS_BEACON_ID)
  --phase-filter    Only upload rows matching this beacon_phase (e.g. CARRIER)
  --passband-hz     Passband in Hz (fallback if CSV lacks the field)
  --poll            Poll interval in seconds   (default: 5)
  --state           State file path            (default: beacon_reporter_state.json)
  --dry-run         Print payloads, don't POST (default: False)

Environment variables (override defaults, overridden by CLI args):
  NTMS_API_URL         API endpoint
  NTMS_MONITOR_TOKEN   Monitor token / bearer token
  NTMS_BEACON_ID       Beacon UUID string
  NTMS_PHASE_FILTER    Phase filter (e.g. CARRIER)
  NTMS_PASSBAND_HZ     Passband in Hz fallback

Example:
  python beacon_reporter.py \\
      --monitor-token  YOUR_MONITOR_TOKEN \\
      --beacon-id      550e8400-e29b-41d4-a716-446655440000
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_INPUT   = "beacon_log.csv"
DEFAULT_POLL    = 5
DEFAULT_STATE   = "beacon_reporter_state.json"
DEFAULT_API_URL = "https://prop.w5isp.com/api/v1/beacon-monitor/measurements"
DEFAULT_PASSBAND_HZ = 5000

MAX_RETRY_DELAY = 300   # seconds — cap backoff at 5 minutes
INITIAL_BACKOFF = 5     # seconds


# ---------------------------------------------------------------------------
# Config file helpers (shared with beacon_monitor.py / beacon_calibrate.py)
# ---------------------------------------------------------------------------

def load_config(path=None):
    """Load beacon_config.py from cwd (or explicit path). Returns dict of settings."""
    if path is None:
        path = "beacon_config.py"
    if not os.path.isfile(path):
        return {}
    namespace = {}
    try:
        with open(path) as f:
            exec(f.read(), namespace)
    except Exception as e:
        print(f"WARNING: Could not load config file {path}: {e}")
        return {}
    return namespace


def _cfg(config, key, env_var, default):
    """Resolve a setting: config file > env var > hardcoded default."""
    if config and key in config:
        return config[key]
    env_val = os.environ.get(env_var)
    if env_val is not None:
        return env_val
    return default


# ---------------------------------------------------------------------------
# State management (persists CSV read position across restarts)
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    """Load reporter state from JSON file, or return defaults."""
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"file_offset": 0, "rows_sent": 0, "last_sent_utc": None}


def save_state(path: str, state: dict) -> None:
    """Persist reporter state to JSON file atomically."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# CSV tailing
# ---------------------------------------------------------------------------

def read_new_rows(csv_path: str, offset: int) -> tuple[list[dict], int]:
    """
    Read any rows added to the CSV since `offset` bytes.

    Returns:
        rows:       list of row dicts (DictReader format)
        new_offset: file offset after reading (pass back next call)
    """
    if not os.path.isfile(csv_path):
        return [], offset

    rows = []
    with open(csv_path, newline="") as f:
        # Peek at header without advancing our offset tracking
        header_line = f.readline()
        if not header_line:
            return [], offset

        fieldnames = next(csv.reader([header_line]))
        header_offset = f.tell()

        # A smaller/replaced file invalidates the persisted position. Restart
        # after its header instead of seeking beyond EOF forever.
        f.seek(0, os.SEEK_END)
        file_end = f.tell()
        if offset < header_offset or offset > file_end:
            offset = header_offset

        f.seek(offset)
        new_offset = offset
        while True:
            line = f.readline()
            if not line:
                break
            # The monitor and reporter are separate processes. Do not consume
            # a row until the writer has completed it with a newline.
            if not line.endswith(("\n", "\r")):
                break
            values = next(csv.reader([line]))
            row = dict(zip(fieldnames, values))
            # Skip blank lines
            if not any(row.values()):
                new_offset = f.tell()
                continue
            rows.append(row)
            new_offset = f.tell()

    return rows, new_offset


# ---------------------------------------------------------------------------
# API payload construction
# ---------------------------------------------------------------------------

def build_payload(row: dict, beacon_id: str, passband_hz_default: float,
                  version: str) -> dict:
    """
    Convert a CSV row into the NTMS Prop API measurement payload.

    All numeric fields use safe defaults when the CSV column is missing,
    which maintains compatibility with logs produced by older monitor versions.
    """
    peak_power = float(row.get("peak_power_dbfs", -999))
    return {
        "beacon_id"             : beacon_id,
        "frequency_hz"          : int(float(row["center_freq_hz"])),
        "measured_at"           : row.get("timestamp_utc", ""),
        "integration_s"         : int(float(row.get("integration_s", 60))),
        "passband_hz"           : float(row.get("passband_hz", passband_hz_default)),
        "gain_db"               : float(row.get("gain_db", 0)),
        "noise_floor_dbfs"      : float(row.get("noise_floor_dbfs", -999)),
        "signal_peak_dbfs"      : float(peak_power),
        "signal_avg_dbfs"       : float(row.get("signal_avg_dbfs", peak_power)),
        "snr_peak_db"           : float(row.get("snr_peak_db", 0)),
        "snr_avg_db"            : float(row.get("snr_avg_db", 0)),
        "signal_active_fraction": float(row.get("signal_active_fraction", 0)),
        "propmonitor_version"   : version
    }


# ---------------------------------------------------------------------------
# API posting
# ---------------------------------------------------------------------------

def post_row(url: str, monitor_token: str, payload: dict,
             dry_run: bool) -> str:
    """
    POST a single measurement to the NTMS API.

    Returns a status string:
        "ok"    — success (HTTP 204)
        "fatal" — unrecoverable (HTTP 401 — bad token)
        "skip"  — don't retry (HTTP 404/409/422, other 4xx)
        "retry" — transient failure (HTTP 429, 5xx, network error)
    """
    if dry_run:
        print(f"  [DRY RUN] Would POST: {json.dumps(payload)}")
        return "ok"

    data    = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type"  : "application/json",
        "Authorization" : f"Bearer {monitor_token}",
        "User-Agent"    : "NTMS-BeaconReporter/2.0"
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            if status == 204:
                return "ok"
            else:
                body = resp.read(200).decode("utf-8", errors="replace")
                print(f"  WARNING: API returned unexpected HTTP {status}: {body}")
                return "retry"
    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", errors="replace")
        print(f"  ERROR: HTTP {e.code} from API: {body}")
        if e.code == 401:
            return "fatal"
        elif e.code in (404, 409, 422):
            return "skip"
        elif e.code == 429:
            return "retry"
        elif 400 <= e.code < 500:
            return "skip"
        else:
            return "retry"
    except urllib.error.URLError as e:
        print(f"  ERROR: Network error posting to API: {e.reason}")
        return "retry"
    except Exception as e:
        print(f"  ERROR: Unexpected error: {e}")
        return "retry"


def send_with_retry(url: str, monitor_token: str, payload: dict,
                    dry_run: bool, row_desc: str) -> bool:
    """
    Attempt to send a row.  Only "retry" responses enter the backoff loop.
    "fatal" exits the process; "skip" returns False immediately.
    """
    result = post_row(url, monitor_token, payload, dry_run)

    if result == "ok":
        return True
    elif result == "skip":
        return False
    elif result == "fatal":
        print("FATAL: Authentication failed (HTTP 401). "
              "Check your monitor token.")
        sys.exit(1)

    # result == "retry" — exponential backoff
    delay   = INITIAL_BACKOFF
    attempt = 1

    while True:
        if dry_run:
            return True

        if delay > MAX_RETRY_DELAY:
            print(f"  Giving up on row {row_desc} after {attempt} attempts.")
            return False

        print(f"  Retry {attempt} in {delay}s for row {row_desc}...")
        time.sleep(delay)

        result = post_row(url, monitor_token, payload, dry_run)
        if result == "ok":
            return True
        elif result == "skip":
            return False
        elif result == "fatal":
            print("FATAL: Authentication failed (HTTP 401). "
                  "Check your monitor token.")
            sys.exit(1)

        attempt += 1
        delay = min(delay * 2, MAX_RETRY_DELAY)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_reporter(args) -> None:
    api_url            = args.api
    monitor_token      = args.monitor_token
    beacon_id          = args.beacon_id
    poll_s             = args.poll
    dry_run            = args.dry_run
    phase_filter       = args.phase_filter
    passband_hz_default = args.passband_hz
    version            = args.propmonitor_version

    print("NTMS Beacon Reporter")
    print(f"  Watching  : {args.input}")
    print(f"  API URL   : {api_url}")
    print(f"  Beacon ID : {beacon_id}")
    print(f"  Poll      : {poll_s}s")
    print(f"  Dry run   : {dry_run}")
    print(f"  State file: {args.state}")
    if phase_filter:
        print(f"  Phase flt : {phase_filter}")
    print()

    state = load_state(args.state)
    print(f"  Resuming from offset {state['file_offset']}, "
          f"{state['rows_sent']} rows previously sent.")
    print("Watching for new observations... (Ctrl+C to stop)\n")

    try:
        while True:
            rows, new_offset = read_new_rows(args.input, state["file_offset"])

            if rows:
                for row in rows:
                    # --- Phase filter ---
                    if phase_filter and row.get("beacon_phase", "") != phase_filter:
                        continue

                    ts = row.get("timestamp_utc", "?")
                    payload = build_payload(row, beacon_id,
                                            passband_hz_default, version)

                    ok = send_with_retry(api_url, monitor_token, payload,
                                         dry_run, ts)

                    if ok:
                        state["rows_sent"]    += 1
                        state["last_sent_utc"] = ts
                        marker = "SENT" if not dry_run else "DRY"
                        above  = ("*** DETECTED ***"
                                  if int(row.get("above_threshold", 0))
                                  else "below threshold")
                        print(f"[{ts}]  {marker}  "
                              f"{float(row.get('peak_freq_hz',0))/1e6:.4f} MHz  "
                              f"{float(row.get('peak_power_dbfs',-999)):+.1f} dBFS  {above}")

                # Advance offset only after all rows in this batch processed
                state["file_offset"] = new_offset
                save_state(args.state, state)

            time.sleep(poll_s)

    except KeyboardInterrupt:
        print(f"\nStopped. {state['rows_sent']} total rows sent.")
        save_state(args.state, state)


def parse_args():
    # --- Auto-load beacon_config.py from cwd (or explicit --config path) ---
    config_path = "beacon_config.py"
    for i, a in enumerate(sys.argv):
        if i == 0:
            continue
        if a == "--config" and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]
            break
        if a.startswith("--config="):
            config_path = a.split("=", 1)[1]
            break
    config = load_config(config_path)

    def c(key, env_var, default):
        return _cfg(config, key, env_var, default)

    p = argparse.ArgumentParser(
        description="NTMS Beacon Reporter — posts CSV log to NTMS Prop API")
    p.add_argument("--config", default=None,
                   help="Optional path to beacon_config.py (default: auto-detect in cwd)")
    p.add_argument("--input",
                   default=str(c("CSV_PATH", "NTMS_INPUT", DEFAULT_INPUT)),
                   help=f"CSV file to watch (default: {DEFAULT_INPUT})")
    p.add_argument("--api",
                   default=str(c("API_URL", "NTMS_API_URL", DEFAULT_API_URL)),
                   help="NTMS API endpoint URL")
    p.add_argument("--monitor-token",
                   default=str(c("MONITOR_TOKEN", "NTMS_MONITOR_TOKEN", "")),
                   help="Monitor token from prop.w5isp.com")
    p.add_argument("--beacon-id",
                   default=str(c("BEACON_ID", "NTMS_BEACON_ID", "")),
                   help="Beacon UUID string")
    p.add_argument("--phase-filter",
                   default=str(c("PHASE_FILTER", "NTMS_PHASE_FILTER", "")),
                   help="Only upload rows matching this beacon_phase (e.g. CARRIER)")
    passband_hz = c("PASSBAND_HZ", "NTMS_PASSBAND_HZ", None)
    if passband_hz is None:
        passband_hz = float(c("PASSBAND_KHZ", "", DEFAULT_PASSBAND_HZ / 1000)) * 1000
    p.add_argument("--passband-hz", type=float,
                   default=float(passband_hz),
                   help=f"Passband in Hz, fallback if CSV lacks field (default: {DEFAULT_PASSBAND_HZ})")
    p.add_argument("--version", type=str,
                   default=str(c("REPORTER_VERSION", "", "2.0.0")),
                   dest="propmonitor_version",
                   help="PropMonitor version string (default: 2.0.0)")
    p.add_argument("--poll", type=float,
                   default=float(c("REPORTER_POLL_S", "", DEFAULT_POLL)),
                   help=f"Poll interval in seconds (default: {DEFAULT_POLL})")
    p.add_argument("--state",
                   default=str(c("REPORTER_STATE_PATH", "", DEFAULT_STATE)),
                   help=f"State file path (default: {DEFAULT_STATE})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print payloads without actually POSTing")

    args = p.parse_args()

    # Validate required fields
    missing = []
    if not args.monitor_token:
        missing.append("--monitor-token (set MONITOR_TOKEN in beacon_config.py)")
    if not args.beacon_id:
        missing.append("--beacon-id (set BEACON_ID in beacon_config.py)")

    if missing:
        print("ERROR: Missing required arguments:")
        for m in missing:
            print(f"  {m}")
        print("\nRun with --dry-run to test without API credentials.")
        sys.exit(1)

    return args


if __name__ == "__main__":
    # Allow --dry-run to skip the credential check
    if "--dry-run" in sys.argv:
        if "--monitor-token" not in sys.argv:
            sys.argv += ["--monitor-token", "dryrun-token"]
        if "--beacon-id" not in sys.argv:
            sys.argv += ["--beacon-id",
                         "00000000-0000-0000-0000-000000000000"]

    run_reporter(parse_args())
