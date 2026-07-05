#!/bin/bash
# NTMS Beacon Monitor — manual start script
# Edit the options below, then run:  bash ~/SDR-based-beacon-receive-systems/run_monitor.sh

PYTHON=/opt/ntms-beacon/venv/bin/python3
SCRIPT=/opt/ntms-beacon/beacon_monitor.py

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
OUTPUT=/var/lib/ntms-beacon/beacon_log.csv

# --- Run ---
sudo $PYTHON $SCRIPT \
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
