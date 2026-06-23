#!/usr/bin/env python3
"""
NTMS 10 GHz Beacon Monitor — NooElec NESDR Smart Edition
=========================================================
Monitors a downconverted 10 GHz beacon signal via a NooElec NESDR Smart
family device.

Supported hardware:
  - NESDR Smart      RTL2832U + R820T2, standard crystal    (~1-2 ppm)
  - NESDR Smart XTR  RTL2832U + R820T2, TCXO 0.5 ppm      (0 ppm default)
  - NESDR Smart v5   RTL2832U + R828D,  TCXO               (0 ppm default)
  - NESDR SMArt      Same as Smart but with SMA connector
  All variants are driven via pyrtlsdr / librtlsdr.

RF chain:
  - "Bullseye" LNB (LO = 9750 MHz low-band, no 22 kHz tone)
  - 10368.370 MHz beacon -> 618.370 MHz IF (LNB may offset ±several hundred kHz)
  - ±1 MHz span captured in a single FFT (no retuning needed)

Device selection:
  By default the first RTL-SDR device found is opened. If you have multiple
  dongles connected, use --device to target the NESDR Smart by index (0, 1, ...)
  or by serial number string (e.g. --device 00000001). Run --list-devices to
  see what is connected.

Beacon cycle (WSJT Q65 / CW / carrier):
  The beacon transmits on a 2-minute UTC cycle:
    Even minutes (0,2,4...): Q65 digital mode — 500 kHz wide wandering tones
    Odd minutes 0-10s:       CW ID — narrow carrier, frequency-stable
    Odd minutes 10-60s:      Steady carrier — best power measurement window
  Each CSV row is tagged with: Q65 | CW | CARRIER
  PropAnalyzer should filter to CARRIER rows for cleanest propagation data.

LNB drift tracking:
  The peak IF frequency is logged each sweep. Since the beacon is GPS-locked,
  any sweep-to-sweep shift in peak_freq_hz reflects LNB LO thermal drift.
  freq_drift_hz column shows change from the previous CARRIER reading.

Output:
  - CSV log: one row per sweep interval
  - Columns: timestamp, beacon_phase, peak_freq_hz, peak_power_dbfs,
             freq_drift_hz, above_threshold, rf_freq_hz, lo_freq_mhz,
             device_serial

Usage:
  python beacon_monitor_nesdr.py [options]

  --freq         Center frequency in MHz             (default: 618.245)
  --lo           LNB LO frequency in MHz             (default: 9750.0)
  --interval     Sweep interval in seconds           (default: 10)
  --threshold    Detection threshold in dBFS         (default: -50.0)
  --gain         Gain in dB, or 'auto'               (default: auto)
  --fft          FFT size (power of 2)               (default: 2048)
  --output       Output CSV file path                (default: beacon_log.csv)
  --duration     Run duration in seconds, 0=forever  (default: 0)
  --ppm          PPM correction                      (default: 0)
  --cw-end       Seconds into odd minute CW ends     (default: 10)
  --device       Device index (int) or serial string (default: 0)
  --list-devices Print connected RTL-SDR devices and exit

R820T2 / R828D gain steps (dB) — use one of these with --gain:
  0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4
  28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6
  Starting point for 10 GHz beacon work: 28–38 dB.
"""

import argparse
import csv
import datetime
import os
import sys
import time

import numpy as np

try:
    from rtlsdr import RtlSdr
except ImportError:
    print("ERROR: pyrtlsdr not installed.")
    print("  Install with:  pip install pyrtlsdr")
    print("  Windows also needs: https://github.com/librtlsdr/librtlsdr/releases")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_CENTER_MHZ   = 618.245     # 10368.370 - 9750.000 = 618.370; tuned to spectrum analyzer reading
DEFAULT_LO_MHZ       = 9750.0      # Bullseye LNB low-band LO (no 22 kHz tone)
DEFAULT_INTERVAL_S   = 10
DEFAULT_THRESHOLD_DB = -50.0
DEFAULT_GAIN         = "auto"
DEFAULT_FFT_SIZE     = 2048
DEFAULT_OUTPUT       = "beacon_log.csv"
DEFAULT_CW_END_S     = 10
DEFAULT_PPM          = 0           # NESDR Smart oscillators are stable; XTR TCXO = 0 ppm
DEFAULT_DEVICE       = 0           # device index; override with serial string
SAMPLE_RATE_HZ       = 2_048_000   # 2.048 MSPS — fits ±1 MHz easily
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def get_device_list() -> list[dict]:
    """
    Return a list of connected RTL-SDR devices.

    Each entry is a dict with keys: index, serial.
    Returns an empty list if no devices are found or if the library
    does not expose a device-count method.
    """
    devices = []
    try:
        serials = RtlSdr.get_device_serial_addresses()
        for idx, serial in enumerate(serials):
            devices.append({"index": idx, "serial": serial})
    except Exception:
        # Older pyrtlsdr builds may not have get_device_serial_addresses
        try:
            count = RtlSdr.get_device_count()
            for idx in range(count):
                devices.append({"index": idx, "serial": "unknown"})
        except Exception:
            pass
    return devices


def print_device_list() -> None:
    """Print a formatted table of connected RTL-SDR devices."""
    devices = get_device_list()
    if not devices:
        print("No RTL-SDR devices found.")
        print("Check that the NESDR Smart is plugged in and drivers are installed.")
        return

    print(f"Found {len(devices)} RTL-SDR device(s):")
    print(f"  {'Index':<8} {'Serial'}")
    print(f"  {'-'*6:<8} {'-'*16}")
    for d in devices:
        print(f"  {d['index']:<8} {d['serial']}")


def resolve_device_serial(device_spec) -> str:
    """
    Return the serial number string for the opened device.

    device_spec is either an integer index or a serial string.
    Returns 'unknown' if the library cannot report serial numbers.
    """
    try:
        serials = RtlSdr.get_device_serial_addresses()
        if isinstance(device_spec, int):
            if device_spec < len(serials):
                return serials[device_spec]
        else:
            # Already a serial string
            return str(device_spec)
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Signal processing (identical to beacon_monitor.py)
# ---------------------------------------------------------------------------

def compute_power_spectrum(samples: np.ndarray, fft_size: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute averaged power spectrum from IQ samples.

    Splits the sample buffer into non-overlapping FFT frames, windows each,
    computes magnitude squared, and averages across frames.

    Returns:
        freqs_offset: array of frequency offsets from center (Hz), FFT-shifted
        power_db:     array of power values in dBFS (dB relative to full scale)
    """
    n_samples = len(samples)
    n_frames  = n_samples // fft_size

    if n_frames == 0:
        raise ValueError(f"Not enough samples ({n_samples}) for FFT size {fft_size}")

    samples  = samples[:n_frames * fft_size].reshape(n_frames, fft_size)
    window   = np.hanning(fft_size)
    windowed = samples * window

    fft_result    = np.fft.fft(windowed, axis=1)
    power         = np.mean(np.abs(fft_result) ** 2, axis=0)
    power_shifted = np.fft.fftshift(power)
    power_db      = 10 * np.log10(power_shifted / (fft_size ** 2) + 1e-12)
    freqs_offset  = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / SAMPLE_RATE_HZ))

    return freqs_offset, power_db


def find_peak(freqs_offset: np.ndarray, power_db: np.ndarray,
              center_hz: float, span_hz: float = 2_000_000) -> tuple[float, float]:
    """
    Find the peak signal within ±(span/2) of the center frequency.

    Returns:
        peak_freq_hz:  absolute frequency of the peak (Hz)
        peak_power_db: power at the peak (dBFS)
    """
    half = span_hz / 2
    mask = (freqs_offset >= -half) & (freqs_offset <= half)
    if not np.any(mask):
        return center_hz, -999.0

    local_power   = power_db[mask]
    local_freqs   = freqs_offset[mask]
    peak_idx      = np.argmax(local_power)
    peak_freq_abs = center_hz + local_freqs[peak_idx]
    peak_power    = local_power[peak_idx]

    return peak_freq_abs, peak_power


def beacon_phase(utc_dt: datetime.datetime, cw_end_s: int = DEFAULT_CW_END_S) -> str:
    """
    Classify the current UTC time within the 2-minute WSJT beacon cycle.

    Returns one of: 'Q65' | 'CW' | 'CARRIER'
    """
    minute = utc_dt.minute
    second = utc_dt.second + utc_dt.microsecond / 1e6

    if minute % 2 == 0:
        return "Q65"
    return "CW" if second < cw_end_s else "CARRIER"


class DriftTracker:
    """
    Tracks LNB frequency drift between successive CARRIER-phase measurements.

    Since the beacon is GPS-locked, any IF frequency shift is purely LNB
    LO thermal drift. Only CARRIER readings are used as reference points.
    """

    def __init__(self):
        self._last_carrier_freq_hz: float | None = None

    def update(self, phase: str, peak_freq_hz: float) -> int | None:
        drift = None
        if self._last_carrier_freq_hz is not None:
            drift = int(round(peak_freq_hz - self._last_carrier_freq_hz))
        if phase == "CARRIER":
            self._last_carrier_freq_hz = peak_freq_hz
        return drift

    @property
    def has_reference(self) -> bool:
        return self._last_carrier_freq_hz is not None


def samples_needed(interval_s: float, fft_size: int) -> int:
    """
    How many IQ samples to collect per sweep interval.

    Collects enough for at least 8 FFT frames, or up to 2 seconds of data,
    capped and rounded to an integer multiple of fft_size.
    """
    interval_samples = int(SAMPLE_RATE_HZ * min(interval_s, 2.0))
    min_samples      = fft_size * 8
    n                = max(interval_samples, min_samples)
    return (n // fft_size) * fft_size


# ---------------------------------------------------------------------------
# NESDR Smart device open
# ---------------------------------------------------------------------------

def open_sdr(center_mhz: float, gain, ppm: int, device_spec) -> tuple:
    """
    Open and configure the NESDR Smart.

    device_spec: int index  -> open by device index
                 str serial -> open by serial number (e.g. '00000001')

    Returns (sdr, serial_string).
    """
    if isinstance(device_spec, str) and not device_spec.lstrip("-").isdigit():
        # Non-numeric string: treat as serial number
        sdr    = RtlSdr(serial_number=device_spec)
        serial = device_spec
    else:
        idx    = int(device_spec)
        sdr    = RtlSdr(device_index=idx)
        serial = resolve_device_serial(idx)

    sdr.sample_rate     = SAMPLE_RATE_HZ
    sdr.center_freq     = int(center_mhz * 1e6)
    sdr.freq_correction = ppm

    if gain == "auto":
        sdr.gain = "auto"
    else:
        sdr.gain = float(gain)

    time.sleep(0.1)
    return sdr, serial


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "timestamp_utc",
    "beacon_phase",
    "peak_freq_hz",
    "peak_power_dbfs",
    "freq_drift_hz",
    "above_threshold",
    "center_freq_hz",
    "lo_freq_mhz",
    "rf_freq_hz",
    "device_serial",    # added: identifies which NESDR Smart unit logged this row
]


def init_csv(path: str) -> None:
    if not os.path.isfile(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(CSV_FIELDS)


def append_row(path: str, row: dict) -> None:
    with open(path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def run_monitor(args) -> None:
    center_hz   = args.freq * 1e6
    lo_mhz      = args.lo
    interval_s  = args.interval
    threshold   = args.threshold
    fft_size    = args.fft
    output_path = args.output
    duration    = args.duration
    cw_end_s    = args.cw_end
    device_spec = args.device

    n_samples = samples_needed(interval_s, fft_size)

    print("NTMS Beacon Monitor — NooElec NESDR Smart")
    print(f"  IF center     : {args.freq:.3f} MHz")
    print(f"  LNB LO        : {lo_mhz:.3f} MHz")
    print(f"  RF (approx)   : {args.freq + lo_mhz:.3f} MHz")
    print(f"  Sample rate   : {SAMPLE_RATE_HZ/1e6:.3f} MSPS")
    print(f"  FFT size      : {fft_size} bins")
    print(f"  Sweep interval: {interval_s} s")
    print(f"  Threshold     : {threshold:.1f} dBFS")
    print(f"  CW/carrier    : CW ends at +{cw_end_s}s into odd minute")
    print(f"  Samples/sweep : {n_samples:,}")
    print(f"  Output file   : {output_path}")
    print(f"  Duration      : {'forever' if duration == 0 else f'{duration}s'}")
    print(f"  Device        : {device_spec}")
    print()

    init_csv(output_path)
    drift_tracker = DriftTracker()

    print("Opening NESDR Smart... ", end="", flush=True)
    try:
        sdr, serial = open_sdr(args.freq, args.gain, args.ppm, device_spec)
    except Exception as e:
        print(f"\nERROR: Could not open device '{device_spec}': {e}")
        print("Run --list-devices to see connected RTL-SDR hardware.")
        sys.exit(1)
    print(f"OK  (serial={serial}, gain={sdr.gain}, ppm={args.ppm})")
    print("Starting sweep loop. Press Ctrl+C to stop.\n")
    print(f"  {'Timestamp':<26} {'Phase':<8} {'IF freq (MHz)':<16} {'Power':>8}  {'Drift':>8}  Status")
    print(f"  {'-'*26} {'-'*7:<8} {'-'*14:<16} {'-'*8}  {'-'*8}  {'-'*16}")

    sweep_count = 0
    start_time  = time.monotonic()
    next_sweep  = start_time

    try:
        while True:
            sleep_for = next_sweep - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

            sweep_start = time.monotonic()
            utc_dt      = datetime.datetime.now(datetime.timezone.utc)
            utc_now     = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            phase       = beacon_phase(utc_dt, cw_end_s)

            try:
                samples = sdr.read_samples(n_samples)
            except Exception as e:
                print(f"  [{utc_now}] WARNING: read_samples failed: {e} — skipping sweep")
                next_sweep += interval_s
                continue

            freqs_offset, power_db = compute_power_spectrum(samples, fft_size)
            peak_freq_hz, peak_power = find_peak(freqs_offset, power_db, center_hz)
            above      = 1 if peak_power >= threshold else 0
            drift_hz   = drift_tracker.update(phase, peak_freq_hz)
            drift_str  = f"{drift_hz:+d}" if drift_hz is not None else "---"
            rf_freq_hz = peak_freq_hz + (lo_mhz * 1e6)

            append_row(output_path, {
                "timestamp_utc"   : utc_now,
                "beacon_phase"    : phase,
                "peak_freq_hz"    : f"{peak_freq_hz:.0f}",
                "peak_power_dbfs" : f"{peak_power:.2f}",
                "freq_drift_hz"   : drift_hz if drift_hz is not None else "",
                "above_threshold" : above,
                "center_freq_hz"  : f"{center_hz:.0f}",
                "lo_freq_mhz"     : f"{lo_mhz:.3f}",
                "rf_freq_hz"      : f"{rf_freq_hz:.0f}",
                "device_serial"   : serial,
            })

            sweep_count += 1
            elapsed     = time.monotonic() - sweep_start
            status      = "*** DETECTED ***" if above else "below threshold"
            phase_label = f"[{phase}]"
            print(f"  {utc_now:<26} {phase_label:<8} {peak_freq_hz/1e6:<16.4f} "
                  f"{peak_power:>+8.1f}  {drift_str:>8}  {status}  ({elapsed*1000:.0f}ms)")

            next_sweep += interval_s

            if duration > 0 and (time.monotonic() - start_time) >= duration:
                print(f"\nDuration {duration}s reached. {sweep_count} sweeps logged.")
                break

    except KeyboardInterrupt:
        print(f"\nStopped by user. {sweep_count} sweeps logged to {output_path}")

    finally:
        sdr.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="NTMS 10 GHz Beacon Monitor — NooElec NESDR Smart",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--list-devices", action="store_true",
                   help="List connected RTL-SDR devices and exit")
    p.add_argument("--device",     default=DEFAULT_DEVICE,
                   help="Device index (int) or serial number string")
    p.add_argument("--freq",       type=float, default=DEFAULT_CENTER_MHZ,
                   help="IF center frequency in MHz")
    p.add_argument("--lo",         type=float, default=DEFAULT_LO_MHZ,
                   help="LNB LO frequency in MHz")
    p.add_argument("--interval",   type=float, default=DEFAULT_INTERVAL_S,
                   help="Sweep interval in seconds")
    p.add_argument("--threshold",  type=float, default=DEFAULT_THRESHOLD_DB,
                   help="Detection threshold in dBFS")
    p.add_argument("--gain",       type=str,   default=DEFAULT_GAIN,
                   help="Gain in dB (R820T2/R828D steps) or 'auto'")
    p.add_argument("--fft",        type=int,   default=DEFAULT_FFT_SIZE,
                   help="FFT size (power of 2)")
    p.add_argument("--output",     type=str,   default=DEFAULT_OUTPUT,
                   help="Output CSV file path")
    p.add_argument("--duration",   type=float, default=0,
                   help="Run for this many seconds then exit (0 = forever)")
    p.add_argument("--ppm",        type=int,   default=DEFAULT_PPM,
                   help="PPM frequency correction (0 for NESDR Smart XTR/v5 TCXO)")
    p.add_argument("--cw-end",     type=int,   default=DEFAULT_CW_END_S,
                   dest="cw_end",
                   help="Seconds into odd minute where CW ends and carrier begins")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.list_devices:
        print_device_list()
        sys.exit(0)

    # Convert --device to int if it looks like a plain integer
    if isinstance(args.device, str) and args.device.lstrip("-").isdigit():
        args.device = int(args.device)

    run_monitor(args)
