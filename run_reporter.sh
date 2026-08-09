#!/bin/bash
PYTHON=/opt/ntms-beacon/venv/bin/python3
SCRIPT=/opt/ntms-beacon/beacon_reporter.py
INPUT=${NTMS_INPUT:-/var/lib/ntms-beacon/beacon_log.csv}
STATE=${NTMS_STATE:-/var/lib/ntms-beacon/beacon_reporter_state.json}

sudo $PYTHON $SCRIPT \
    --input "$INPUT" \
    --state "$STATE" \
    --poll 5
