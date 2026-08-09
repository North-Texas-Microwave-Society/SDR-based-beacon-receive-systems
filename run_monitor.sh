#!/bin/bash
# NTMS Beacon Monitor — one-command start
# All settings come from beacon_config.py in the current directory.
# Copy beacon_config.example.py → beacon_config.py and edit it for your station.
#
# Run:  bash ~/SDR-based-beacon-receive-systems/run_monitor.sh

PYTHON=/opt/ntms-beacon/venv/bin/python3
SCRIPT=/opt/ntms-beacon/beacon_monitor.py

# On Pi, CSV output goes to the standard data directory.
# Remove --output to use the path from beacon_config.py instead.
sudo $PYTHON $SCRIPT --output /var/lib/ntms-beacon/beacon_log.csv
