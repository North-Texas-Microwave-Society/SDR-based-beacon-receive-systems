# SDR-Based 10 GHz Beacon Monitoring System

An SDR-based system for monitoring the NTMS 10 GHz beacon at volunteer receive stations across the North Texas Microwave Society network.

## Overview

This project provides three Python scripts that together form a complete beacon monitoring pipeline:

| Script | Hardware | Purpose |
|--------|----------|---------|
| `beacon_monitor.py` | RTL-SDR Blog V3 (or any RTL2832U dongle) | Capture, FFT analysis, phase detection, CSV logging |
| `beacon_monitor_nesdr.py` | NooElec NESDR Smart / Smart XTR / Smart v5 | Same pipeline with NESDR device enumeration, serial tracking, and TCXO-optimized PPM defaults |
| `beacon_reporter.py` | (hardware-agnostic) | Tail CSV log, POST observations to NTMS API with retry/backoff |

Both monitor scripts produce the same CSV format and feed the same reporter.

## Hardware

### Generic RTL-SDR (beacon_monitor.py)

| Component | Details |
|-----------|---------|
| SDR Dongle | RTL-SDR Blog V3 (RTL2832U) or compatible |
| Downconverter | "Bullseye" LNB, LO = 9750 MHz (no 22 kHz tone required) |
| Beacon frequency | 10368.370 MHz → 618.370 MHz IF |
| Capture bandwidth | ±1 MHz (no retuning needed) |

### NooElec NESDR Smart (beacon_monitor_nesdr.py)

| Variant | Tuner | Oscillator | PPM |
|---------|-------|------------|-----|
| NESDR Smart | R820T2 | Standard crystal | ~1–2 ppm |
| NESDR Smart XTR | R820T2 | TCXO 0.5 ppm | **0** (default) |
| NESDR Smart v5 | R828D | TCXO | **0** (default) |
| NESDR SMArt | R820T2 | Standard crystal | ~1–2 ppm |

All NESDR Smart variants use the same pyrtlsdr/librtlsdr driver. The NESDR-specific script adds device enumeration, selection by serial number, and records the device serial in the CSV.

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

### Generic RTL-SDR monitor

```bash
python beacon_monitor.py \
    --freq 618.245 \
    --lo 9750.0 \
    --interval 10 \
    --threshold -50.0 \
    --output beacon_log.csv
```

| Option | Default | Description |
|--------|---------|-------------|
| `--freq` | 618.245 MHz | SDR center frequency (IF after LNB) |
| `--lo` | 9750.0 MHz | LNB LO frequency |
| `--interval` | 10 s | Sweep interval |
| `--threshold` | −50.0 dBFS | Detection threshold |
| `--gain` | auto | Gain in dB or `auto` |
| `--ppm` | 1 | PPM correction (Windows LIBUSB workaround) |
| `--duration` | 0 (forever) | Run time in seconds |

### NooElec NESDR Smart monitor

```bash
# List connected RTL-SDR devices
python beacon_monitor_nesdr.py --list-devices

# Run with default device (index 0)
python beacon_monitor_nesdr.py \
    --freq 618.245 \
    --lo 9750.0 \
    --output beacon_log.csv

# Target a specific unit by serial number
python beacon_monitor_nesdr.py --device 00000001 --output beacon_log.csv
```

| Option | Default | Description |
|--------|---------|-------------|
| `--device` | `0` | Device index (int) or serial number string |
| `--list-devices` | — | Print connected devices and exit |
| `--freq` | 618.245 MHz | SDR center frequency (IF after LNB) |
| `--lo` | 9750.0 MHz | LNB LO frequency |
| `--interval` | 10 s | Sweep interval |
| `--threshold` | −50.0 dBFS | Detection threshold |
| `--gain` | auto | Gain in dB (R820T2/R828D steps) or `auto` |
| `--ppm` | **0** | PPM correction (0 suits TCXO variants; set to 1–2 for standard crystal) |
| `--duration` | 0 (forever) | Run time in seconds |

R820T2 / R828D gain steps (dB): `0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4 28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6` — starting point for 10 GHz beacon work is typically 28–38 dB.

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

| Column | Description | NESDR variant |
|--------|-------------|---------------|
| `timestamp_utc` | ISO-8601 UTC timestamp of the sweep | both |
| `beacon_phase` | `Q65`, `CW`, or `CARRIER` | both |
| `peak_freq_hz` | IF peak frequency (Hz) — reflects LNB drift | both |
| `peak_power_dbfs` | Signal power at peak (dBFS) | both |
| `freq_drift_hz` | Hz shift from last `CARRIER` reading (LNB thermal drift proxy) | both |
| `above_threshold` | `1` if detected, `0` if below threshold | both |
| `center_freq_hz` | SDR center frequency (Hz) | both |
| `lo_freq_mhz` | LNB LO (MHz) | both |
| `rf_freq_hz` | Reconstructed RF = peak IF + LO | both |
| `device_serial` | Serial number of the NESDR Smart unit | NESDR only |

## LNB Drift Tracking

Because the beacon is GPS-locked, any sweep-to-sweep shift in `peak_freq_hz` reflects LNB LO thermal drift rather than beacon frequency instability. The `freq_drift_hz` column tracks this, using successive `CARRIER`-phase readings as reference points.

## Running Both Scripts Together

```bash
# Terminal 1 — collect data (generic RTL-SDR)
python beacon_monitor.py --output beacon_log.csv

# Terminal 1 — collect data (NESDR Smart)
python beacon_monitor_nesdr.py --output beacon_log.csv

# Terminal 2 — upload data (works with either monitor)
python beacon_reporter.py --site YOUR-CALLSIGN-10G-CITY
```

---

## Raspberry Pi Deployment

The `pi/` directory contains a one-command installer that configures a headless Pi as a fully automatic monitoring station.

### Recommended hardware per station

| Item | Notes | ~Cost |
|------|-------|-------|
| Raspberry Pi 4 Model B, 2 GB | 4 USB-A ports, WiFi + Ethernet | $35–45 |
| MicroSD card, 32 GB, Class 10 | SanDisk or Samsung recommended | $10 |
| USB-C power supply, 5V 3A | Official Pi supply preferred | $12 |
| Case | Passive cooling is fine for this workload | $8–12 |

Flash **Raspberry Pi OS Lite (64-bit)** using Raspberry Pi Imager. In the Imager advanced settings, pre-configure your WiFi credentials and enable SSH — the Pi will be network-accessible on first boot with no keyboard or monitor required.

### One-time install

Clone the repo on the Pi, then run the installer as root:

```bash
git clone https://github.com/North-Texas-Microwave-Society/SDR-based-beacon-receive-systems.git
cd SDR-based-beacon-receive-systems
sudo bash pi/install.sh
```

The installer will:

1. Install `librtlsdr`, `rtl-sdr`, and `python3-venv` from apt
2. Blacklist the DVB kernel module so the NESDR Smart is available to librtlsdr
3. Create a `ntms-beacon` system user with USB device access
4. Create `/opt/ntms-beacon/` (scripts + venv) and `/var/lib/ntms-beacon/` (CSV + state)
5. Install `pyrtlsdr` and `numpy` in an isolated virtualenv
6. Prompt for site-specific values and write `/opt/ntms-beacon/station.conf`
7. Install and enable `beacon-monitor` and `beacon-reporter` as systemd services
8. Start both services and print a status summary

> **Note:** The DVB module blacklist takes effect after a reboot. If the monitor fails to open the device on the first run, reboot the Pi.

### What runs automatically

Both services start on boot and restart automatically if they crash:

```
beacon-monitor.service  →  beacon_monitor_nesdr.py  (sweeps every 10 s, writes CSV)
beacon-reporter.service →  beacon_reporter.py        (tails CSV, POSTs to NTMS API)
```

### Useful commands on the Pi

```bash
# Live log from both services
sudo journalctl -u beacon-monitor -u beacon-reporter -f

# Check service status
sudo systemctl status beacon-monitor beacon-reporter

# Restart after changing station.conf
sudo systemctl restart beacon-monitor beacon-reporter

# List connected SDR devices
/opt/ntms-beacon/venv/bin/python3 /opt/ntms-beacon/beacon_monitor_nesdr.py --list-devices
```

### Reconfiguring a station

Edit `/opt/ntms-beacon/station.conf` directly, then restart:

```bash
sudo nano /opt/ntms-beacon/station.conf
sudo systemctl restart beacon-monitor beacon-reporter
```

Or re-run the installer — it detects the existing config and asks before overwriting it.

### Pi-specific notes vs Windows

| Topic | Windows | Raspberry Pi |
|-------|---------|--------------|
| Driver install | Zadig (WinUSB replacement) | Not needed — librtlsdr is a native apt package |
| DVB module conflict | Not applicable | Must blacklist `dvb_usb_rtl28xxu` (installer does this) |
| `ppm=0` bug | Yes — triggers LIBUSB_ERROR_INVALID_PARAM | Not present on Linux |
| Autostart | Task Scheduler (manual) | systemd (handled by installer) |

## License

MIT License — see LICENSE file for details.

## Contributing

This project is maintained by volunteer stations of the [North Texas Microwave Society](https://ntms.org). Issues and pull requests are welcome.
