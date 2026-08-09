#!/bin/bash
# NTMS Beacon Monitor — manual start script (Linux / Raspberry Pi / macOS)
# Edit the options below, then run:  bash ~/SDR-based-beacon-receive-systems/run_monitor.sh
#
# Windows users: use run_monitor.ps1 instead.
#
# Runner selection:
#   1. The venv created by pi/install.sh, if present.
#   2. uv, which installs the dependencies declared in beacon_monitor.py.
#
# If you get a USB permission error, install the rtl-sdr udev rules
# (sudo apt install rtl-sdr) or re-run this script with:  SUDO=sudo bash run_monitor.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDO="${SUDO:-}"

if [[ -x /opt/ntms-beacon/venv/bin/python3 ]]; then
    RUNNER=(/opt/ntms-beacon/venv/bin/python3 /opt/ntms-beacon/beacon_monitor.py)
    DEFAULT_OUTPUT=/var/lib/ntms-beacon/beacon_log.csv
else
    if ! command -v uv >/dev/null 2>&1; then
        # setup.sh installs uv, librtlsdr, and the USB permissions.
        echo "uv is not installed — running setup.sh to install the dependencies."
        bash "${REPO_DIR}/setup.sh" || exit 1
        if [[ -f "${HOME}/.local/bin/env" ]]; then source "${HOME}/.local/bin/env"; fi
        export PATH="${HOME}/.local/bin:${PATH}"
        command -v uv >/dev/null 2>&1 || {
            echo "ERROR: uv still not on PATH. Open a new terminal and re-run." >&2
            exit 1
        }
    fi
    # Absolute path so the sudo case below still finds uv.
    RUNNER=("$(command -v uv)" run --script "${REPO_DIR}/beacon_monitor.py")
    DEFAULT_OUTPUT="${REPO_DIR}/beacon_log.csv"
fi

if [[ -n "$SUDO" ]]; then
    RUNNER=(sudo -E "${RUNNER[@]}")
fi

# --- Site ---
LOCATION="KM5PO-10G-BURLESON"

# --- RF chain ---
FREQ=618.245        # IF center frequency (MHz) after Bullseye LNB
LO=9750.0           # LNB LO frequency (MHz)
PPM=0               # PPM correction (0 for TCXO; 1-2 for standard crystal)

# --- SDR gain and detection ---
# Run beacon_calibrate.py to find optimal values for your setup.
GAIN=36.4           # R820T2 gain in dB (or: auto)
THRESHOLD=-35.0     # Detection threshold in dBFS

# --- Sweep ---
INTERVAL=10         # Sweep interval in seconds
MAX_SIGNALS=1       # Max signals to detect per sweep (1-5)
SPAN=2000           # Analysis span in kHz (2000 = full 2 MHz capture)

# --- Output ---
# Defaults to /var/lib/ntms-beacon/beacon_log.csv when installed via pi/install.sh,
# otherwise to beacon_log.csv next to this script.
OUTPUT="$DEFAULT_OUTPUT"

# --- Run ---
"${RUNNER[@]}" \
    --location   "$LOCATION" \
    --freq       $FREQ \
    --lo         $LO \
    --ppm        $PPM \
    --gain       $GAIN \
    --threshold  $THRESHOLD \
    --interval   $INTERVAL \
    --max-signals $MAX_SIGNALS \
    --span       $SPAN \
    --output     "$OUTPUT"
