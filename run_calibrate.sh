#!/bin/bash
# NTMS Beacon Station Calibrator — one-command start
# All settings come from beacon_config.py in the current directory.
# Point dish at cold sky, then run:  bash ~/SDR-based-beacon-receive-systems/run_calibrate.sh

PYTHON=/opt/ntms-beacon/venv/bin/python3
SCRIPT=/opt/ntms-beacon/beacon_calibrate.py

sudo $PYTHON $SCRIPT
