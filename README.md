# SDR-Based 10 GHz Beacon Monitoring System

An SDR-based system for monitoring the NTMS 10 GHz beacon at volunteer receive stations across the North Texas Microwave Society network.

## Overview

This project provides two Python scripts that together form a complete beacon monitoring pipeline:

- **`beacon_monitor.py`** — captures and analyzes the downconverted beacon signal using an RTL-SDR dongle, classifies beacon phases (Q65 / CW / CARRIER), tracks LNB drift, and logs measurements to a CSV file.
- **`beacon_reporter.py`** — tails the CSV log and forwards each observation to the NTMS central API, with persistent state, retry logic, and exponential backoff.

## Hardware

| Component | Details |
|-----------|---------|
| SDR Dongle | RTL-SDR Blog V3 (RTL2832U) or compatible |
| Downconverter | "Bullseye" LNB, LO = 9750 MHz (no 22 kHz tone required) |
| Beacon frequency | 10368.370 MHz → 618.370 MHz IF |
| Capture bandwidth | ±1 MHz (no retuning needed) |

## Beacon Cycle

The beacon transmits on a 2-minute UTC cycle synchronized to the WSJT Q65 protocol:

| Phase | Timing | Description |
|-------|--------|-------------|
| `Q65` | Even minutes, 0–60 s | Digital mode, 500 kHz wide wandering tones |
| `CW` | Odd minutes, 0–10 s | CW Morse ID, narrow carrier |
| `CARRIER` | Odd minutes, 10–60 s | Steady carrier — best power measurement window |

Each CSV row is tagged with the current phase. Propagation analysis should filter to `CARRIER` rows for the cleanest signal-strength data.

## Installation

```bash
pip install pyrtlsdr numpy
```

Windows users also need the librtlsdr DLL from https://github.com/librtlsdr/librtlsdr/releases

## Usage

### Monitor (data collection)

```bash
python beacon_monitor.py \
    --freq 618.245 \
    --lo 9750.0 \
    --interval 10 \
    --threshold -50.0 \
    --output beacon_log.csv
```

Key options:

| Option | Default | Description |
|--------|---------|-------------|
| `--freq` | 618.245 MHz | SDR center frequency (IF after LNB) |
| `--lo` | 9750.0 MHz | LNB LO frequency |
| `--interval` | 10 s | Sweep interval |
| `--threshold` | −50.0 dBFS | Detection threshold |
| `--gain` | auto | RTL-SDR gain (dB or `auto`) |
| `--ppm` | 1 | PPM correction (Windows LIBUSB workaround) |
| `--duration` | 0 (forever) | Run time in seconds |

### Reporter (data upload)

```bash
python beacon_reporter.py \
    --api  https://api.ntms.org/beacon/observation \
    --key  YOUR_API_KEY \
    --site KM5PO-10G-BURLESON
```

Credentials can also be supplied via environment variables:

```bash
export NTMS_API_URL=https://api.ntms.org/beacon/observation
export NTMS_API_KEY=YOUR_API_KEY
export NTMS_SITE_ID=KM5PO-10G-BURLESON
python beacon_reporter.py
```

Use `--dry-run` to verify operation without sending real data.

## CSV Log Format

| Column | Description |
|--------|-------------|
| `timestamp_utc` | ISO-8601 UTC timestamp of the sweep |
| `beacon_phase` | `Q65`, `CW`, or `CARRIER` |
| `peak_freq_hz` | IF peak frequency (Hz) — reflects LNB drift |
| `peak_power_dbfs` | Signal power at peak (dBFS) |
| `freq_drift_hz` | Hz shift from last `CARRIER` reading (LNB thermal drift proxy) |
| `above_threshold` | `1` if detected, `0` if below threshold |
| `center_freq_hz` | SDR center frequency (Hz) |
| `lo_freq_mhz` | LNB LO (MHz) |
| `rf_freq_hz` | Reconstructed RF = peak IF + LO |

## LNB Drift Tracking

Because the beacon is GPS-locked, any sweep-to-sweep shift in `peak_freq_hz` reflects LNB LO thermal drift rather than beacon frequency instability. The `freq_drift_hz` column tracks this, using successive `CARRIER`-phase readings as reference points.

## Running Both Scripts Together

```bash
# Terminal 1 — collect data
python beacon_monitor.py --output beacon_log.csv

# Terminal 2 — upload data
python beacon_reporter.py --site YOUR-CALLSIGN-10G-CITY
```

## License

MIT License — see LICENSE file for details.

## Contributing

This project is maintained by volunteer stations of the [North Texas Microwave Society](https://ntms.org). Issues and pull requests are welcome.
