#!/bin/bash
# NTMS Beacon Station Calibrator — manual start script (Linux / Raspberry Pi / macOS)
# Point dish at cold sky, then run:  bash ~/SDR-based-beacon-receive-systems/run_calibrate.sh
#
# Windows users: use run_calibrate.ps1 instead.
#
# Runner selection:
#   1. The venv created by pi/install.sh, if present.
#   2. uv, which installs the dependencies declared in beacon_calibrate.py.
#
# If you get a USB permission error, install the rtl-sdr udev rules
# (sudo apt install rtl-sdr) or re-run this script with:  SUDO=sudo bash run_calibrate.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDO="${SUDO:-}"

if [[ -x /opt/ntms-beacon/venv/bin/python3 ]]; then
    RUNNER=(/opt/ntms-beacon/venv/bin/python3 /opt/ntms-beacon/beacon_calibrate.py)
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
    RUNNER=("$(command -v uv)" run --script "${REPO_DIR}/beacon_calibrate.py")
fi

if [[ -n "$SUDO" ]]; then
    RUNNER=(sudo -E "${RUNNER[@]}")
fi

# --- RF chain ---
FREQ=618.245        # IF center frequency (MHz) after Bullseye LNB
LO=9750.0           # LNB LO frequency (MHz)
PPM=0               # PPM correction

# --- Calibration sweep settings ---
DWELL=2.0           # Seconds of IQ data collected per gain step
SETTLE=0.5          # Seconds to wait after each gain change
EXCLUDE=500         # Exclusion zone (kHz) around center when measuring noise
MARGIN=10.0         # dB above noise floor for suggested threshold
GAINS=all           # 'all' for full R820T2 sweep, or e.g. '28.0,29.7,32.8,33.8,36.4'

# --- Output ---
# Timestamped CSV saved automatically if --output not specified

"${RUNNER[@]}" \
    --freq    $FREQ \
    --lo      $LO \
    --ppm     $PPM \
    --dwell   $DWELL \
    --settle  $SETTLE \
    --exclude $EXCLUDE \
    --margin  $MARGIN \
    --gains   $GAINS
