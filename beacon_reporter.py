#!/usr/bin/env python3
"""
NTMS Beacon Reporter
=====================
Watches beacon_log.csv for new rows and POSTs each one to the NTMS API.

Designed to run alongside beacon_monitor.py — either as a separate process
or started automatically. Tracks its position in the CSV so it never sends
the same row twice, survives restarts (position is saved to a state file),
and retries failed POSTs with exponential backoff.

Usage:
  python beacon_reporter.py [options]

  --input      CSV file to watch          (default: beacon_log.csv)
  --api        NTMS API endpoint URL      (required, or set NTMS_API_URL env var)
  --key        NTMS API key               (required, or set NTMS_API_KEY env var)
  --site       Site/station identifier    (required, or set NTMS_SITE_ID env var)
  --poll       Poll interval in seconds   (default: 5)
  --state      State file path            (default: beacon_reporter_state.json)
  --dry-run    Print payloads, don't POST (default: False)

Environment variables (override defaults, overridden by CLI args):
  NTMS_API_URL   API endpoint
  NTMS_API_KEY   API key / bearer token
  NTMS_SITE_ID   Site identifier string

Example:
  python beacon_reporter.py \\
      --api  https://api.ntms.org/beacon/observation \\
      --key  YOUR_API_KEY \\
      --site KM5PO-10G-BURLESON
"""

import argparse
import json
import os
import sys
import time
import csv
import datetime
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_INPUT   = "beacon_log.csv"
DEFAULT_POLL    = 5
DEFAULT_STATE   = "beacon_reporter_state.json"

MAX_RETRY_DELAY = 300   # seconds — cap backoff at 5 minutes
INITIAL_BACKOFF = 5     # seconds


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

        fieldnames = [h.strip() for h in header_line.strip().split(",")]

        # If we've never read past the header, start at end of header line
        if offset == 0:
            offset = f.tell()

        # Seek to where we left off
        if offset > f.tell():
            f.seek(offset)
        else:
            f.seek(offset)

        reader = csv.DictReader(f, fieldnames=fieldnames)
        for row in reader:
            # Skip blank lines
            if not any(row.values()):
                continue
            rows.append(dict(row))

        new_offset = f.tell()

    return rows, new_offset


# ---------------------------------------------------------------------------
# API posting
# ---------------------------------------------------------------------------

def build_payload(row: dict, site_id: str) -> dict:
    """
    Convert a CSV row into the NTMS API payload.

    Adjust field names here to match your actual API schema.
    """
    return {
        "site_id"          : site_id,
        "timestamp_utc"    : row.get("timestamp_utc", ""),
        "peak_freq_hz"     : int(float(row.get("peak_freq_hz", 0))),
        "peak_power_dbfs"  : float(row.get("peak_power_dbfs", -999)),
        "above_threshold"  : int(row.get("above_threshold", 0)),
        "center_freq_hz"   : int(float(row.get("center_freq_hz", 0))),
        "lo_freq_mhz"      : float(row.get("lo_freq_mhz", 0)),
        "rf_freq_hz"       : int(float(row.get("rf_freq_hz", 0))),
        "reporter_version" : "1.0"
    }


def post_row(url: str, api_key: str, payload: dict, dry_run: bool) -> bool:
    """
    POST a single observation to the NTMS API.

    Returns True on success, False on failure.
    """
    if dry_run:
        print(f"  [DRY RUN] Would POST: {json.dumps(payload)}")
        return True

    data    = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type"  : "application/json",
        "Authorization" : f"Bearer {api_key}",
        "User-Agent"    : "NTMS-BeaconReporter/1.0"
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            if 200 <= status < 300:
                return True
            else:
                body = resp.read(200).decode("utf-8", errors="replace")
                print(f"  WARNING: API returned HTTP {status}: {body}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", errors="replace")
        print(f"  ERROR: HTTP {e.code} from API: {body}")
        return False
    except urllib.error.URLError as e:
        print(f"  ERROR: Network error posting to API: {e.reason}")
        return False
    except Exception as e:
        print(f"  ERROR: Unexpected error: {e}")
        return False


def send_with_retry(url: str, api_key: str, payload: dict,
                    dry_run: bool, row_desc: str) -> bool:
    """
    Attempt to send a row, retrying with exponential backoff on failure.
    Returns True only after successful delivery.
    """
    delay = INITIAL_BACKOFF
    attempt = 0

    while True:
        attempt += 1
        ok = post_row(url, api_key, payload, dry_run)
        if ok:
            return True

        # Don't retry dry runs
        if dry_run:
            return True

        if delay > MAX_RETRY_DELAY:
            print(f"  Giving up on row {row_desc} after {attempt} attempts.")
            return False

        print(f"  Retry {attempt} in {delay}s for row {row_desc}...")
        time.sleep(delay)
        delay = min(delay * 2, MAX_RETRY_DELAY)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_reporter(args) -> None:
    api_url  = args.api
    api_key  = args.key
    site_id  = args.site
    poll_s   = args.poll
    dry_run  = args.dry_run

    print(f"NTMS Beacon Reporter")
    print(f"  Watching  : {args.input}")
    print(f"  API URL   : {api_url}")
    print(f"  Site ID   : {site_id}")
    print(f"  Poll      : {poll_s}s")
    print(f"  Dry run   : {dry_run}")
    print(f"  State file: {args.state}")
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
                    ts = row.get("timestamp_utc", "?")
                    payload = build_payload(row, site_id)

                    ok = send_with_retry(api_url, api_key, payload, dry_run, ts)

                    if ok:
                        state["rows_sent"]    += 1
                        state["last_sent_utc"] = ts
                        marker = "SENT" if not dry_run else "DRY"
                        above  = "*** DETECTED ***" if int(row.get("above_threshold", 0)) else "below threshold"
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
    p = argparse.ArgumentParser(description="NTMS Beacon Reporter — posts CSV log to NTMS API")

    p.add_argument("--input",   default=os.environ.get("NTMS_INPUT",   DEFAULT_INPUT),
                   help=f"CSV file to watch (default: {DEFAULT_INPUT})")
    p.add_argument("--api",     default=os.environ.get("NTMS_API_URL",  ""),
                   help="NTMS API endpoint URL (or set NTMS_API_URL)")
    p.add_argument("--key",     default=os.environ.get("NTMS_API_KEY",  ""),
                   help="NTMS API key (or set NTMS_API_KEY)")
    p.add_argument("--site",    default=os.environ.get("NTMS_SITE_ID",  ""),
                   help="Site/station identifier (or set NTMS_SITE_ID)")
    p.add_argument("--poll",    type=float, default=DEFAULT_POLL,
                   help=f"Poll interval in seconds (default: {DEFAULT_POLL})")
    p.add_argument("--state",   default=DEFAULT_STATE,
                   help=f"State file path (default: {DEFAULT_STATE})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print payloads without actually POSTing")

    args = p.parse_args()

    # Validate required fields
    missing = []
    if not args.api:    missing.append("--api (or NTMS_API_URL)")
    if not args.key:    missing.append("--key (or NTMS_API_KEY)")
    if not args.site:   missing.append("--site (or NTMS_SITE_ID)")

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
        # Supply dummy values so argparse doesn't fail
        if "--api"  not in sys.argv: sys.argv += ["--api",  "http://localhost/dry-run"]
        if "--key"  not in sys.argv: sys.argv += ["--key",  "dryrun"]
        if "--site" not in sys.argv: sys.argv += ["--site", "TEST-SITE"]

    run_reporter(parse_args())
