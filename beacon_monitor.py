#!/usr/bin/env python3
"""
NTMS 10 GHz Beacon Monitor
===========================
Monitors a downconverted 10 GHz beacon signal via RTL-SDR.

Hardware assumption:
  - "Bullseye" LNB (LO = 9750 MHz low-band, no 22 kHz tone)
  - RTL-SDR Blog V3 (or any RTL2832U dongle)
  - 10368.370 MHz beacon -> 618.370 MHz IF (LNB may offset ±several hundred kHz)
  - ±1 MHz span captured in a single FFT (no retuning needed)

Beacon cycle awareness (WSJT Q65 / CW / carrier pattern):
  The beacon transmits on a 2-minute UTC cycle:
    Even minutes (0,2,4...): Q65 digital mode — 500 kHz wide wandering tones
    Odd minutes 0-10s:       CW ID — narrow carrier, frequency-stable
    Odd minutes 10-60s:      Steady carrier — best power measurement window
  Each CSV row is tagged with: Q65 | CW | CARRIER | IDLE
  PropAnalyzer should filter to CARRIER rows for cleanest propagation data.

LNB drift tracking:
  The peak IF frequency is logged each sweep. Since the beacon is GPS-locked,
  any sweep-to-sweep shift in peak_freq_hz reflects LNB LO thermal drift.
  freq_drift_hz column shows change from the previous CARRIER reading.

Output:
  - CSV log: one row per sweep interval
  - Columns: timestamp, beacon_phase, peak_freq_hz, peak_power_dbfs,
             freq_drift_hz, above_threshold, rf_freq_hz, lo_freq_mhz

Usage:
  python beacon_monitor.py [options]

  --freq       Center frequency in MHz           (default: 618.245)
  --lo         LNB LO frequency in MHz           (default: 9750.0)
  --interval   Sweep interval in seconds         (default: 10)
  --threshold  Detection threshold in dBFS       (default: -50.0)
  --gain       RTL-SDR gain in dB, or 'auto'     (default: auto)
  --fft        FFT size (power of 2)             (default: 2048)
  --output     Output CSV file path              (default: beacon_log.csv)
  --duration   Run duration in seconds, 0=forever (default: 0)
  --ppm        PPM correction (default: 1)
  --cw-end     Seconds into odd minute where CW ends / carrier begins (default: 10)
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
DEFAULT_CW_END_S     = 10          # seconds into odd minute where CW ends and carrier begins
SAMPLE_RATE_HZ       = 2_048_000   # 2.048 MSPS — fits ±1 MHz easily
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

    # Trim to integer number of frames
    samples = samples[:n_frames * fft_size].reshape(n_frames, fft_size)

    # Hann window to reduce spectral leakage
    window     = np.hanning(fft_size)
    windowed   = samples * window

    # FFT, average power across frames
    fft_result = np.fft.fft(windowed, axis=1)
    power      = np.mean(np.abs(fft_result) ** 2, axis=0)

    # FFT shift so DC is in the center
    power_shifted = np.fft.fftshift(power)

    # Normalize to dBFS (0 dBFS = full-scale complex amplitude of 1.0+1.0j)
    # RTL-SDR returns 8-bit unsigned samples scaled to [-1, 1] by pyrtlsdr
    power_db = 10 * np.log10(power_shifted / (fft_size ** 2) + 1e-12)

    # Frequency axis (offset from center, in Hz)
    freqs_offset = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / SAMPLE_RATE_HZ))

    return freqs_offset, power_db


def find_peak(freqs_offset: np.ndarray, power_db: np.ndarray,
              center_hz: float, span_hz: float = 2_000_000) -> tuple[float, float]:
    """
    Find the peak signal within ±(span/2) of the center frequency.

    Returns:
        peak_freq_hz:   absolute frequency of the peak (Hz)
        peak_power_db:  power at the peak (dBFS)
    """
    half = span_hz / 2
    mask = (freqs_offset >= -half) & (freqs_offset <= half)
    if not np.any(mask):
        return center_hz, -999.0

    local_power = power_db[mask]
    local_freqs = freqs_offset[mask]

    peak_idx      = np.argmax(local_power)
    peak_offset   = local_freqs[peak_idx]
    peak_power    = local_power[peak_idx]
    peak_freq_abs = center_hz + peak_offset

    return peak_freq_abs, peak_power


def beacon_phase(utc_dt: datetime.datetime, cw_end_s: int = DEFAULT_CW_END_S) -> str:
    """
    Classify the current UTC time within the 2-minute WSJT beacon cycle.

    Cycle (repeats every 2 minutes):
      Even minute, 0-60s  -> 'Q65'     (digital mode, 500 kHz wide tones)
      Odd minute,  0-Ns   -> 'CW'      (morse ID, narrow carrier)
      Odd minute,  N-60s  -> 'CARRIER' (steady carrier — best measurement)

    N = cw_end_s (default 10, adjustable via --cw-end)

    Returns one of: 'Q65' | 'CW' | 'CARRIER'
    """
    minute  = utc_dt.minute
    second  = utc_dt.second + utc_dt.microsecond / 1e6

    if minute % 2 == 0:
        return "Q65"
    else:
        if second < cw_end_s:
            return "CW"
        else:
            return "CARRIER"


class DriftTracker:
    """
    Tracks LNB frequency drift between successive CARRIER-phase measurements.

    Since the beacon is GPS-locked, any change in the measured IF peak
    frequency is entirely due to LNB LO thermal drift.

    Only CARRIER readings are used as reference points because:
      - Q65 peak jumps around within the 500 kHz tone span
      - CW peak is reliable but brief; carrier is the cleanest steady-state
    """

    def __init__(self):
        self._last_carrier_freq_hz: float | None = None

    def update(self, phase: str, peak_freq_hz: float) -> int | None:
        """
        Record a new measurement and return drift in Hz from last CARRIER reading.

        Returns:
            drift_hz (int):  signed Hz shift from last CARRIER reference,
                             or None if no prior CARRIER reading exists yet.
        """
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

    We collect enough for at least 8 FFT frames, or enough to fill the
    interval at the sample rate — whichever is larger — capped at 4M samples
    to avoid memory pressure on a Pi.
    """
    interval_samples = int(SAMPLE_RATE_HZ * min(interval_s, 2.0))   # max 2s of data
    min_samples      = fft_size * 8
    n                = max(interval_samples, min_samples)
    # Round down to nearest multiple of fft_size
    n = (n // fft_size) * fft_size
    return n


def open_sdr(center_mhz: float, gain, ppm: int = 1) -> RtlSdr:
    """Initialize and return a configured RtlSdr instance."""
    sdr = RtlSdr()
    sdr.sample_rate    = SAMPLE_RATE_HZ
    sdr.center_freq    = int(center_mhz * 1e6)
    sdr.freq_correction = ppm  # ppm correction (default 1 avoids Windows LIBUSB_ERROR_INVALID_PARAM)

    if gain == "auto":
        sdr.gain = "auto"
    else:
        sdr.gain = float(gain)

    # Short settle time after tuning
    time.sleep(0.1)
    return sdr


def init_csv(path: str) -> bool:
    """
    Create the CSV file with a header row if it does not already exist.
    Returns True if header was written (new file), False if file existed.
    """
    exists = os.path.isfile(path)
    if not exists:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp_utc",
                "beacon_phase",      # Q65 | CW | CARRIER
                "peak_freq_hz",      # IF peak frequency (Hz) — reflects LNB drift
                "peak_power_dbfs",   # signal power at peak (dBFS)
                "freq_drift_hz",     # Hz shift from last CARRIER reading (LNB drift proxy)
                "above_threshold",   # 1 if peak_power >= threshold, else 0
                "center_freq_hz",    # SDR center frequency (Hz)
                "lo_freq_mhz",       # LNB LO (MHz)
                "rf_freq_hz"         # reconstructed RF = peak IF + LO
            ])
    return not exists


def append_row(path: str, row: dict) -> None:
    """Append one measurement row to the CSV."""
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp_utc", "beacon_phase", "peak_freq_hz", "peak_power_dbfs",
            "freq_drift_hz", "above_threshold", "center_freq_hz", "lo_freq_mhz", "rf_freq_hz"
        ])
        writer.writerow(row)


def run_monitor(args) -> None:
    center_hz   = args.freq * 1e6
    lo_mhz      = args.lo
    interval_s  = args.interval
    threshold   = args.threshold
    fft_size    = args.fft
    output_path = args.output
    duration    = args.duration
    cw_end_s    = args.cw_end

    n_samples = samples_needed(interval_s, fft_size)

    print(f"NTMS Beacon Monitor")
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
    print()

    init_csv(output_path)
    drift_tracker = DriftTracker()

    print("Opening SDR... ", end="", flush=True)
    try:
        sdr = open_sdr(args.freq, args.gain, args.ppm)
    except Exception as e:
        print(f"\nERROR: Could not open RTL-SDR: {e}")
        print("Check that the dongle is plugged in and drivers are installed.")
        sys.exit(1)
    print(f"OK  (gain={sdr.gain})")
    print("Starting sweep loop. Press Ctrl+C to stop.\n")
    print(f"  {'Timestamp':<26} {'Phase':<8} {'IF freq (MHz)':<16} {'Power':>8}  {'Drift':>8}  Status")
    print(f"  {'-'*26} {'-'*7:<8} {'-'*14:<16} {'-'*8}  {'-'*8}  {'-'*16}")

    sweep_count  = 0
    start_time   = time.monotonic()
    next_sweep   = start_time

    try:
        while True:
            now = time.monotonic()

            # Wait until next scheduled sweep
            sleep_for = next_sweep - now
            if sleep_for > 0:
                time.sleep(sleep_for)

            sweep_start = time.monotonic()
            utc_dt      = datetime.datetime.now(datetime.timezone.utc)
            utc_now     = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            # --- Classify beacon phase ---
            phase = beacon_phase(utc_dt, cw_end_s)

            # --- Collect samples ---
            try:
                samples = sdr.read_samples(n_samples)
            except Exception as e:
                print(f"  [{utc_now}] WARNING: read_samples failed: {e} — skipping sweep")
                next_sweep += interval_s
                continue

            # --- Compute spectrum ---
            freqs_offset, power_db = compute_power_spectrum(samples, fft_size)

            # --- Find peak ---
            peak_freq_hz, peak_power = find_peak(freqs_offset, power_db, center_hz)
            above = 1 if peak_power >= threshold else 0

            # --- LNB drift ---
            drift_hz = drift_tracker.update(phase, peak_freq_hz)
            drift_str = f"{drift_hz:+d}" if drift_hz is not None else "---"

            # Reconstruct RF frequency
            rf_freq_hz = peak_freq_hz + (lo_mhz * 1e6)

            # --- Log ---
            row = {
                "timestamp_utc"   : utc_now,
                "beacon_phase"    : phase,
                "peak_freq_hz"    : f"{peak_freq_hz:.0f}",
                "peak_power_dbfs" : f"{peak_power:.2f}",
                "freq_drift_hz"   : drift_hz if drift_hz is not None else "",
                "above_threshold" : above,
                "center_freq_hz"  : f"{center_hz:.0f}",
                "lo_freq_mhz"     : f"{lo_mhz:.3f}",
                "rf_freq_hz"      : f"{rf_freq_hz:.0f}"
            }
            append_row(output_path, row)

            sweep_count += 1
            elapsed = time.monotonic() - sweep_start
            status  = "*** DETECTED ***" if above else "below threshold"

            # Phase label with padding for alignment
            phase_label = f"[{phase}]"
            print(f"  {utc_now:<26} {phase_label:<8} {peak_freq_hz/1e6:<16.4f} "
                  f"{peak_power:>+8.1f}  {drift_str:>8}  {status}  ({elapsed*1000:.0f}ms)")

            # Schedule next sweep relative to when this one started (avoids drift)
            next_sweep += interval_s

            # Duration check
            if duration > 0 and (time.monotonic() - start_time) >= duration:
                print(f"\nDuration {duration}s reached. {sweep_count} sweeps logged.")
                break

    except KeyboardInterrupt:
        print(f"\nStopped by user. {sweep_count} sweeps logged to {output_path}")

    finally:
        sdr.close()


def parse_args():
    p = argparse.ArgumentParser(description="NTMS 10 GHz Beacon Monitor via RTL-SDR")
    p.add_argument("--freq",      type=float, default=DEFAULT_CENTER_MHZ,
                   help=f"IF center frequency in MHz (default: {DEFAULT_CENTER_MHZ})")
    p.add_argument("--lo",        type=float, default=DEFAULT_LO_MHZ,
                   help=f"LNB LO frequency in MHz (default: {DEFAULT_LO_MHZ})")
    p.add_argument("--interval",  type=float, default=DEFAULT_INTERVAL_S,
                   help=f"Sweep interval in seconds (default: {DEFAULT_INTERVAL_S})")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_DB,
                   help=f"Detection threshold in dBFS (default: {DEFAULT_THRESHOLD_DB})")
    p.add_argument("--gain",      type=str,   default=DEFAULT_GAIN,
                   help=f"RTL-SDR gain in dB, or 'auto' (default: {DEFAULT_GAIN})")
    p.add_argument("--fft",       type=int,   default=DEFAULT_FFT_SIZE,
                   help=f"FFT size, power of 2 (default: {DEFAULT_FFT_SIZE})")
    p.add_argument("--output",    type=str,   default=DEFAULT_OUTPUT,
                   help=f"Output CSV file path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--duration",  type=float, default=0,
                   help="Run for this many seconds then exit (0 = forever)")
    p.add_argument("--ppm",       type=int,   default=1,
                   help="PPM frequency correction (default: 1, workaround for Windows LIBUSB bug)")
    p.add_argument("--cw-end",    type=int,   default=DEFAULT_CW_END_S,
                   dest="cw_end",
                   help=f"Seconds into odd minute where CW ends and carrier begins (default: {DEFAULT_CW_END_S})")
    return p.parse_args()


if __name__ == "__main__":
    run_monitor(parse_args())
