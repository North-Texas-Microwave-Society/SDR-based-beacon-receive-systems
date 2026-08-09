#!/bin/bash
# NTMS Beacon Reporter — optional standalone script
# The monitor now handles reporting inline via --report.
# Use this script only when you need a separate reporter process
# (e.g. backfilling old CSV data, or a station running monitor-only mode).
PYTHON=/opt/ntms-beacon/venv/bin/python3
SCRIPT=/opt/ntms-beacon/beacon_reporter.py
INPUT=${NTMS_INPUT:-/var/lib/ntms-beacon/beacon_log.csv}
STATE=${NTMS_STATE:-/var/lib/ntms-beacon/beacon_reporter_state.json}

sudo $PYTHON $SCRIPT \
    --input "$INPUT" \
    --state "$STATE" \
    --poll 5
