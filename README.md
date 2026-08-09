# SDR-Based 10 GHz Beacon Monitoring System

An SDR-based system for monitoring the NTMS 10 GHz beacon at volunteer receive stations across the North Texas Microwave Society network.

## Overview

This project provides three Python scripts that together form a complete beacon monitoring pipeline:

| Script | Hardware | Purpose |
|--------|----------|---------|
| `beacon_calibrate.py` | RTL-SDR Blog V3 (or any RTL2832U dongle) | Sweep gain settings, find optimal gain and detection threshold |
| `beacon_monitor.py` | RTL-SDR Blog V3 (or any RTL2832U dongle) | Capture, FFT analysis, phase detection, CSV logging |
| `beacon_reporter.py` | (hardware-agnostic) | Tail CSV log, POST observations to NTMS API with retry/backoff |

Three convenience shell scripts (`run_*.sh`) provide pre-configured launchers for Raspberry Pi deployments — see [Convenience Scripts](#convenience-scripts) below.

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
# Create and activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

Windows users also need the librtlsdr DLL from https://github.com/librtlsdr/librtlsdr/releases

## Configuration

All settings live in a single Python config file. First, create your copy of the example:

```bash
cp beacon_config.example.py beacon_config.py
```

Then edit `beacon_config.py` and set the values for your station. Every script auto-detects `beacon_config.py` in the current directory — no environment variables needed. CLI args still override config values when you need a one-off change.

### Required settings — you must change these

| Setting | What to put |
|---------|-------------|
| `SDR_DEVICE` | `0` for the first dongle, or a serial string for a specific device. Run `rtl_test` to list devices. |
| `SDR_FREQ_MHZ` | Your IF center frequency. Default `618.245` MHz is correct for a 9750 MHz LO and a 10368.370 MHz beacon. |
| `SDR_LO_MHZ` | Your LNB LO frequency. `9750.0` for the Bullseye LNB. |
| `SDR_PPM` | `0` for a TCXO dongle, `1`–`2` for a standard crystal dongle. |
| `THRESHOLD_DBFS` | Detection threshold from calibration output. See [Station Calibration](#station-calibration) below. |
| `LOCATION` | Your station identifier (e.g. `"W5ISP"`). Tagged on every CSV row. |
| `API_URL` | API endpoint. Default points at the NTMS production API. |
| `MONITOR_TOKEN` | Your station's API token from the beacon-monitor setup page. |
| `BEACON_ID` | UUID of the beacon this station monitors. |
| `REPORT` | `True` to enable inline API reporting, `False` for collection-only. |

### Optional settings — defaults are fine for most stations

| Setting | Default | Description |
|---------|---------|-------------|
| `SDR_GAIN` | `"auto"` | R820T2/R828D gain in dB, or `"auto"` for per-sweep AGC. Set to a fixed value (e.g. `33.8`) from calibration for consistency. |
| `SDR_FFT_SIZE` | `2048` | FFT bins. Larger values give finer frequency resolution but need more CPU. |
| `SWEEP_INTERVAL_S` | `10` | Seconds between sweeps. |
| `SWEEP_DURATION_S` | `0` | Seconds to run before exiting. `0` = run forever. |
| `CW_END_S` | `10` | Seconds into the odd minute where CW ID ends. |
| `SPAN_KHZ` | `2000` | Analysis span in kHz around center. |
| `PASSBAND_KHZ` | `5` | ± bandwidth for in-band vs out-of-band power comparison. |
| `CSV_PATH` | `"beacon_log.csv"` | Where to write the CSV log. Override with `--output`. |
| `PHASE_FILTER` | `"CARRIER"` | Only upload rows matching this phase (`CARRIER`, `CW`, `Q65`, or `""` for all). |
| `REPORTER_POLL_S` | `5` | Poll interval for the standalone reporter. |
| `REPORTER_STATE_PATH` | `"beacon_reporter_state.json"` | State file for the standalone reporter. |

### After editing

Run the calibrator to find your optimal gain and threshold, then start monitoring:

```bash
python beacon_calibrate.py          # find gain + threshold
python beacon_monitor.py            # start collecting + uploading
```

## Station Calibration

Before monitoring, run the calibrator to find the optimal gain and detection threshold for your LNB-SDR system:

```bash
# Point dish at cold sky (away from beacon, sun, and ground clutter), then:
python beacon_calibrate.py
```

The script reads SDR settings from `beacon_config.py`. You can override any setting on the command line:
```bash
python beacon_calibrate.py --freq 618.245 --lo 9750.0 --margin 12.0
```

The script steps through all R820T2/R828D gain settings (0–49.6 dB), measures the noise floor at each, and identifies the "knee" — the gain where LNB thermal noise begins to dominate over SDR ADC quantization noise. The optimal operating point is just past the knee; the threshold is set a fixed margin above that floor.

| Option | Default | Description |
|--------|---------|-------------|
| `--freq` | 618.245 MHz | SDR center frequency (IF after LNB) |
| `--lo` | 9750.0 MHz | LNB LO frequency |
| `--ppm` | 1 | PPM correction |
| `--fft` | 4096 | FFT size, power of 2 |
| `--dwell` | 2.0 s | Seconds of data collected per gain step |
| `--settle` | 0.5 s | Settle time after each gain change |
| `--exclude` | 500 kHz | Exclusion zone around center when measuring noise |
| `--margin` | 10.0 dB | Threshold margin above noise floor |
| `--gains` | all | Gain values to test — `all` or comma-separated dB values |
| `--device` | 0 | Device index or serial string |
| `--output` | auto | CSV output file (default: `beacon_cal_<timestamp>.csv`) |

Read the recommendation at the bottom of the output — it provides `--gain` and `--threshold` values to plug into `beacon_monitor.py`.

## Usage

### Monitor (data collection + optional API reporting)

All settings come from `beacon_config.py`. Just run:

```bash
python beacon_monitor.py
```

With `REPORT = True` in your config, the monitor handles both data collection and API upload in a single process — no separate reporter needed. For one-off overrides:

```bash
python beacon_monitor.py --gain 36.4 --threshold -35.0 --output /tmp/test.csv
```

| Option | Default | Description |
|--------|---------|-------------|
| `--freq` | 618.245 MHz | SDR center frequency (IF after LNB) |
| `--lo` | 9750.0 MHz | LNB LO frequency |
| `--interval` | 10 s | Sweep interval |
| `--threshold` | −50.0 dBFS | Detection threshold |
| `--gain` | auto | Gain in dB or `auto` |
| `--ppm` | 1 | PPM correction (Windows LIBUSB workaround) |
| `--passband-khz` | 5 | ± bandwidth for signal vs noise separation |
| `--duration` | 0 (forever) | Run time in seconds |

R820T2 / R828D gain steps (dB): `0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4 28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6` — starting point for 10 GHz beacon work is typically 28–38 dB.

### Reporter (standalone — optional)

The monitor's built-in `--report` mode handles uploading inline. Use the standalone reporter only when you need a separate process (backfilling old CSV data, or running monitor-only mode):

```bash
python beacon_reporter.py
```

Settings come from `beacon_config.py`. Override as needed:
```bash
python beacon_reporter.py --phase-filter CARRIER --poll 5
```

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
| `gain_db` | Actual SDR gain used (resolved from 'auto') |
| `noise_floor_dbfs` | Median out-of-band power (dBFS) |
| `signal_avg_dbfs` | Mean in-band power (dBFS) |
| `snr_peak_db` | Peak SNR (peak − noise floor) |
| `snr_avg_db` | Average SNR (mean − noise floor) |
| `signal_active_fraction` | Fraction of FFT frames where signal > noise+3dB |
| `integration_s` | Sweep integration time in seconds |
| `passband_hz` | Passband bandwidth used for metrics (Hz) |

## LNB Drift Tracking

Because the beacon is GPS-locked, any sweep-to-sweep shift in `peak_freq_hz` reflects LNB LO thermal drift rather than beacon frequency instability. The `freq_drift_hz` column tracks this, using successive `CARRIER`-phase readings as reference points.

## Running

With `REPORT = True` in `beacon_config.py`, the monitor handles everything in one process:

```bash
python beacon_monitor.py          # collect + upload, all from config
```

For a station that wants separate processes:
```bash
# Terminal 1 — collect only (set REPORT = False in config, or use --no-report override)
python beacon_monitor.py

# Terminal 2 — backfill or separate reporter
python beacon_reporter.py
```

## Convenience Scripts

Three shell scripts in the repo root provide one-command launchers for Raspberry Pi deployments. All settings come from `beacon_config.py` — no editing of shell scripts needed:

```bash
bash run_calibrate.sh    # Sweep gain settings, find optimal threshold
bash run_monitor.sh      # Start data collection (+ API report if enabled in config)
bash run_reporter.sh     # Standalone reporter (optional — monitor handles this via config)
```

These use the standard Pi deployment paths (`/opt/ntms-beacon/`). On a workstation, run the Python scripts directly.

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
2. Blacklist the DVB kernel module so the SDR dongle is available to librtlsdr
3. Create a `ntms-beacon` system user with USB device access
4. Create `/opt/ntms-beacon/` (scripts + venv) and `/var/lib/ntms-beacon/` (CSV + state)
5. Install dependencies from `requirements.txt` in an isolated virtualenv
6. Prompt for site-specific values and write systemd drop-in override files
7. Install and enable `beacon-monitor` and `beacon-reporter` as systemd services
8. Start both services and print a status summary

> **Note:** The DVB module blacklist takes effect after a reboot. If the monitor fails to open the device on the first run, reboot the Pi.

### What runs automatically

Both services start on boot and restart automatically if they crash:

```
beacon-monitor.service  →  beacon_monitor.py   (sweeps every 10 s, writes CSV)
beacon-reporter.service →  beacon_reporter.py   (tails CSV, POSTs to NTMS API)
```

### Useful commands on the Pi

```bash
# Live log from both services
sudo journalctl -u beacon-monitor -u beacon-reporter -f

# Check service status
sudo systemctl status beacon-monitor beacon-reporter

# Restart services
sudo systemctl restart beacon-monitor beacon-reporter

# List connected SDR devices
/opt/ntms-beacon/venv/bin/python3 /opt/ntms-beacon/beacon_monitor.py --list-devices
```

### Reconfiguring a station

Edit your `beacon_config.py` and restart:

```bash
sudo systemctl restart beacon-monitor beacon-reporter
```

## License

MIT License — see LICENSE file for details.

## Contributing

This project is maintained by volunteer stations of the [North Texas Microwave Society](https://ntms.org). Issues and pull requests are welcome.
