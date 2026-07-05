#!/bin/bash
# NTMS Beacon Station Calibrator — manual start script
# Point dish at cold sky, then run:  bash ~/SDR-based-beacon-receive-systems/run_calibrate.sh

PYTHON=/opt/ntms-beacon/venv/bin/python3
SCRIPT=/opt/ntms-beacon/beacon_calibrate.py

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

sudo $PYTHON $SCRIPT \
    --freq    $FREQ \
    --lo      $LO \
    --ppm     $PPM \
    --dwell   $DWELL \
    --settle  $SETTLE \
    --exclude $EXCLUDE \
    --margin  $MARGIN \
    --gains   $GAINS
