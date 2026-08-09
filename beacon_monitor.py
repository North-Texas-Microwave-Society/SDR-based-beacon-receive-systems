#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "numpy>=1.21",
#     "pyrtlsdr>=0.3.0",
#     # Prebuilt librtlsdr binaries, so no system package is needed on the
#     # common platforms. pyrtlsdr loads this before searching the system for
#     # librtlsdr, and the marker skips architectures with no wheel (notably
#     # 32-bit Raspberry Pi OS), which fall back to the system library.
#     "pyrtlsdrlib>=0.0.5; platform_machine in 'x86_64 AMD64 aarch64 arm64 x86'",
# ]
# ///
"""
NTMS 10 GHz Beacon Monitor
===========================
Monitors a downconverted 10 GHz beacon signal via RTL-SDR.

Hardware assumption:
  - "Bullseye" LNB (LO = 9750 MHz low-band, no 22 kHz tone)
  - RTL-SDR Blog V3 (or any RTL2832U dongle)
  - 10368.370 MHz beacon -> 618.370 MHz IF (LNB may offset +/-several hundred kHz)
  - +/-1 MHz span captured in a single FFT (no retuning needed)

Beacon cycle awareness (WSJT Q65 / CW / carrier pattern):
  The beacon transmits on a 2-minute UTC cycle:
    Even minutes (0,2,4...): Q65 digital mode -- 500 kHz wide wandering tones
    Odd minutes 0-10s:       CW ID -- narrow carrier, frequency-stable
    Odd minutes 10-60s:      Steady carrier -- best power measurement window
  Each CSV row is tagged with: Q65 | CW | CARRIER
  PropAnalyzer should filter to CARRIER rows for cleanest propagation data.

LNB drift tracking:
  The peak IF frequency is logged each sweep. Since the beacon is GPS-locked,
  any change in the measured IF peak frequency reflects LNB LO thermal drift.
  freq_drift_hz column shows change from the previous CARRIER reading.
  Drift is only tracked on signal_rank=1 (strongest signal).

Multi-signal detection:
  Up to --max-signals peaks can be detected per sweep (default 1).
  Each detected signal above threshold is written as a separate CSV row
  with signal_rank=1 for strongest, signal_rank=2 for second strongest, etc.
  Peak suppression window of +/-5 kHz prevents re-detecting the same signal.
  If no signals exceed threshold, one row is still written with
  signal_rank=1, above_threshold=0 to record the noise floor measurement.

Location tagging:
  --location string is written to every CSV row for field session tracking.
  Use grid square, site name, or both: e.g. "EM12IL-BURLESON-TOWER"

Output:
  - CSV log: one row per detected signal per sweep (long format)
  - Columns: timestamp_utc, location, beacon_phase, signal_rank,
             peak_freq_hz, peak_power_dbfs, freq_drift_hz, freq_sep_hz,
             above_threshold, center_freq_hz, lo_freq_mhz, rf_freq_hz
  - freq_sep_hz: frequency difference rank1 minus rank2 (Hz), rank-1 row only,
                 populated only when --max-signals 2 and both peaks detected.

Usage:
  python beacon_monitor.py [options]

  --freq         Center frequency in MHz            (default: 618.245)
  --lo           LNB LO frequency in MHz            (default: 9750.0)
  --interval     Sweep interval in seconds          (default: 10)
  --threshold    Detection threshold in dBFS        (default: -50.0)
  --gain         RTL-SDR gain in dB, or 'auto'      (default: auto)
  --fft          FFT size (power of 2)              (default: 2048)
  --output       Output CSV file path               (default: beacon_log.csv)
  --duration     Run duration in seconds, 0=forever (default: 0)
  --ppm          PPM correction                     (default: 1)
  --cw-end       Seconds into odd minute where CW ends (default: 10)
  --max-signals  Max signals to detect per sweep    (default: 1, max: 5)
  --location     Site identifier string             (default: UNKNOWN)
"""

import argparse
import csv
import datetime
import os
import sys
import time

import numpy as np

def _add_local_dll_dir() -> None:
    """Let setup.ps1's lib\\ directory satisfy the librtlsdr load on Windows.

    Python 3.8+ no longer searches PATH for native DLL dependencies, so the
    directory has to be registered explicitly before pyrtlsdr loads it.
    No-op everywhere else.
    """
    if sys.platform != "win32":
        return
    lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
    if os.path.isdir(lib_dir):
        os.add_dll_directory(lib_dir)
        os.environ["PATH"] = lib_dir + os.pathsep + os.environ.get("PATH", "")


_add_local_dll_dir()

try:
    from rtlsdr import RtlSdr
except ImportError as exc:
    if isinstance(exc, ModuleNotFoundError) and (exc.name or "").split(".")[0] == "rtlsdr":
        print("ERROR: the pyrtlsdr package is not installed.")
        print("  Run this script with uv and it installs its own dependencies:")
        print("    uv run beacon_monitor.py")
        print("  Or install manually:  pip install pyrtlsdr numpy")
    else:
        # pyrtlsdr imported but could not load the native library behind it.
        print(f"ERROR: {exc}")
        print("  pyrtlsdr is installed, but the native librtlsdr library was not found.")
        print("  Raspberry Pi OS / Debian / Ubuntu:  sudo apt install librtlsdr-dev rtl-sdr")
        print("  macOS:                              brew install librtlsdr")
        print("  Windows: download the DLLs from")
        print("             https://github.com/librtlsdr/librtlsdr/releases")
        print("           put them on PATH (or beside this script), then run Zadig")
        print("           to install the WinUSB driver for the dongle.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_CENTER_MHZ    = 618.245    # 10368.370 - 9750.000 = 618.370; tuned to SA reading
DEFAULT_LO_MHZ        = 9750.0    # Bullseye LNB low-band LO (no 22 kHz tone)
DEFAULT_INTERVAL_S    = 10
DEFAULT_THRESHOLD_DB  = -50.0
DEFAULT_GAIN          = "auto"
DEFAULT_FFT_SIZE      = 2048
DEFAULT_OUTPUT        = "beacon_log.csv"
DEFAULT_CW_END_S      = 10        # seconds into odd minute where CW ends
DEFAULT_MAX_SIGNALS   = 1
DEFAULT_LOCATION      = "UNKNOWN"
DEFAULT_SPAN_KHZ      = 2000       # analysis span in kHz (default = full 2 MHz capture)
DEFAULT_PPM           = 1
DEFAULT_DEVICE        = 0
SAMPLE_RATE_HZ        = 2_048_000  # 2.048 MSPS -- fits +/-1 MHz easily
SUPPRESS_HZ           = 5_000      # peak suppression window (+/-5 kHz)
CSV_FIELDS            = [
    "timestamp_utc", "location", "beacon_phase", "signal_rank",
    "peak_freq_hz", "peak_power_dbfs", "freq_drift_hz", "freq_sep_hz",
    "above_threshold", "center_freq_hz", "lo_freq_mhz", "rf_freq_hz"
]
# ---------------------------------------------------------------------------


def _env(name, default):
    """Return env var as string, or str(default) if unset. Lets systemd EnvironmentFile drive the script."""
    return os.environ.get(name, str(default))


def get_device_list() -> list:
    devices = []
    try:
        serials = RtlSdr.get_device_serial_addresses()
        for idx, serial in enumerate(serials):
            devices.append({"index": idx, "serial": serial})
    except Exception:
        try:
            count = RtlSdr.get_device_count()
            for idx in range(count):
                devices.append({"index": idx, "serial": "unknown"})
        except Exception:
            pass
    return devices


def print_device_list() -> None:
    devices = get_device_list()
    if not devices:
        print("No RTL-SDR devices found.")
        return
    print(f"Found {len(devices)} RTL-SDR device(s):")
    print(f"  {'Index':<8} Serial")
    print(f"  {'------':<8} ----------------")
    for d in devices:
        print(f"  {d['index']:<8} {d['serial']}")


def resolve_device_serial(device_spec) -> str:
    try:
        serials = RtlSdr.get_device_serial_addresses()
        if isinstance(device_spec, int) and device_spec < len(serials):
            return serials[device_spec]
        elif isinstance(device_spec, str):
            return device_spec
    except Exception:
        pass
    return "unknown"


def compute_power_spectrum(samples: np.ndarray, fft_size: int):
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
    fft_out  = np.fft.fft(samples * window, axis=1)
    power    = np.mean(np.abs(fft_out) ** 2, axis=0)
    power_s  = np.fft.fftshift(power)
    power_db = 10 * np.log10(power_s / (fft_size ** 2) + 1e-12)
    freqs    = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / SAMPLE_RATE_HZ))

    return freqs, power_db


def find_peaks(freqs_offset: np.ndarray, power_db: np.ndarray,
               center_hz: float, max_signals: int,
               span_hz: float = 2_000_000, suppress_hz: float = SUPPRESS_HZ):
    """
    Find up to max_signals peaks within +/-(span/2) of center using
    iterative peak suppression.

    Algorithm:
      1. Find strongest peak in span
      2. Suppress +/-suppress_hz bins around it (zero them out in a copy)
      3. Repeat up to max_signals times

    Returns:
        list of (peak_freq_hz, peak_power_dbfs) tuples,
        strongest first. Always returns at least one entry.
    """
    half = span_hz / 2
    mask = (freqs_offset >= -half) & (freqs_offset <= half)

    if not np.any(mask):
        return [(center_hz, -999.0)]

    # Work on a masked copy so we don't modify the original
    local_power = power_db.copy()
    local_power[~mask] = -999.0

    bin_hz    = SAMPLE_RATE_HZ / len(freqs_offset)
    suppress_bins = max(1, int(suppress_hz / bin_hz))

    results = []
    for _ in range(max_signals):
        idx = int(np.argmax(local_power))
        peak_offset = freqs_offset[idx]
        peak_power  = local_power[idx]

        if peak_power <= -900:   # no more real signals
            break

        peak_freq_abs = center_hz + peak_offset
        results.append((peak_freq_abs, float(peak_power)))

        # Suppress window around this peak
        lo = max(0, idx - suppress_bins)
        hi = min(len(local_power), idx + suppress_bins + 1)
        local_power[lo:hi] = -999.0

    if not results:
        results = [(center_hz, -999.0)]

    return results


def beacon_phase(utc_dt: datetime.datetime, cw_end_s: int = DEFAULT_CW_END_S) -> str:
    """
    Classify the current UTC time within the 2-minute WSJT beacon cycle.

    Cycle (repeats every 2 minutes):
      Even minute, 0-60s  -> 'Q65'     (digital mode, 500 kHz wide tones)
      Odd minute,  0-Ns   -> 'CW'      (morse ID, narrow carrier)
      Odd minute,  N-60s  -> 'CARRIER' (steady carrier -- best measurement)
    """
    minute = utc_dt.minute
    second = utc_dt.second + utc_dt.microsecond / 1e6

    if minute % 2 == 0:
        return "Q65"
    elif second < cw_end_s:
        return "CW"
    else:
        return "CARRIER"


class DriftTracker:
    """
    Tracks LNB frequency drift between successive CARRIER-phase measurements
    of the rank-1 (strongest) signal.

    Since the beacon is GPS-locked, any change in measured IF peak frequency
    is entirely due to LNB LO thermal drift.
    """

    def __init__(self):
        self._last_carrier_freq_hz = None

    def update(self, phase: str, peak_freq_hz: float):
        """
        Record a new rank-1 measurement and return drift in Hz.

        Returns drift_hz (int) or None if no prior CARRIER reading exists.
        """
        drift = None
        if self._last_carrier_freq_hz is not None:
            drift = int(round(peak_freq_hz - self._last_carrier_freq_hz))
        if phase == "CARRIER":
            self._last_carrier_freq_hz = peak_freq_hz
        return drift


def samples_needed(interval_s: float, fft_size: int) -> int:
    """
    How many IQ samples to collect per sweep interval.
    At least 8 FFT frames, up to 2 seconds worth, rounded to fft_size.
    """
    interval_samples = int(SAMPLE_RATE_HZ * min(interval_s, 2.0))
    min_samples      = fft_size * 8
    n = max(interval_samples, min_samples)
    n = (n // fft_size) * fft_size
    return n


def open_sdr(center_mhz: float, gain, ppm: int = 1, device_spec=0) -> RtlSdr:
    """Initialize and return a configured RtlSdr instance."""
    if isinstance(device_spec, str) and not device_spec.lstrip("-").isdigit():
        sdr = RtlSdr(serial_number=device_spec)
    else:
        sdr = RtlSdr(device_index=int(device_spec))

    sdr.sample_rate = SAMPLE_RATE_HZ
    sdr.center_freq = int(center_mhz * 1e6)
    if ppm != 0:
        sdr.freq_correction = ppm

    if gain == "auto":
        sdr.gain = "auto"
    else:
        sdr.gain = float(gain)

    time.sleep(0.1)
    return sdr


def init_csv(path: str) -> None:
    """Create the CSV file with header row if it does not already exist."""
    if not os.path.isfile(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(CSV_FIELDS)


def append_rows(path: str, rows: list) -> None:
    """Append one or more measurement rows to the CSV."""
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        for row in rows:
            w.writerow(row)


def run_monitor(args) -> None:
    center_hz   = args.freq * 1e6
    lo_mhz      = args.lo
    interval_s  = args.interval
    threshold   = args.threshold
    fft_size    = args.fft
    output_path = args.output
    duration    = args.duration
    cw_end_s    = args.cw_end
    max_signals = args.max_signals
    location    = args.location
    span_hz     = args.span_khz * 1000

    n_samples = samples_needed(interval_s, fft_size)

    print("NTMS Beacon Monitor")
    print(f"  IF center     : {args.freq:.3f} MHz")
    print(f"  LNB LO        : {lo_mhz:.3f} MHz")
    print(f"  RF (approx)   : {args.freq + lo_mhz:.3f} MHz")
    print(f"  Sample rate   : {SAMPLE_RATE_HZ/1e6:.3f} MSPS")
    print(f"  FFT size      : {fft_size} bins  ({SAMPLE_RATE_HZ/fft_size:.0f} Hz/bin)")
    print(f"  Analysis span : +/-{span_hz/2/1e3:.0f} kHz  ({span_hz/1e3:.0f} kHz total)")
    print(f"  Sweep interval: {interval_s} s")
    print(f"  Threshold     : {threshold:.1f} dBFS")
    print(f"  Max signals   : {max_signals}")
    print(f"  Suppress win  : +/-{SUPPRESS_HZ/1e3:.0f} kHz around each peak")
    print(f"  CW/carrier    : CW ends at +{cw_end_s}s into odd minute")
    print(f"  Location      : {location}")
    print(f"  Samples/sweep : {n_samples:,}")
    print(f"  Output file   : {output_path}")
    print(f"  Duration      : {'forever' if duration == 0 else f'{duration}s'}")
    print()

    init_csv(output_path)
    drift_tracker = DriftTracker()

    print("Opening SDR... ", end="", flush=True)
    try:
        sdr = open_sdr(args.freq, args.gain, args.ppm, args.device)
    except Exception as e:
        print(f"\nERROR: Could not open RTL-SDR: {e}")
        print("Check that the dongle is plugged in and drivers are installed.")
        sys.exit(1)
    serial = resolve_device_serial(args.device)
    print(f"OK  (device={serial}, gain={sdr.gain})")
    print("Starting sweep loop. Press Ctrl+C to stop.\n")
    print(f"  {'Timestamp':<26} {'Ph':<8} {'Rk'} {'IF freq (MHz)':<16} {'Power':>8}  {'Drift':>8}  Status")
    print(f"  {'-'*26} {'-'*7:<8} {'--'} {'-'*14:<16} {'-'*8}  {'-'*8}  {'-'*16}")

    sweep_count = 0
    row_count   = 0
    start_time  = time.monotonic()
    next_sweep  = start_time

    try:
        while True:
            now = time.monotonic()
            sleep_for = next_sweep - now
            if sleep_for > 0:
                time.sleep(sleep_for)

            sweep_start = time.monotonic()
            utc_dt      = datetime.datetime.now(datetime.timezone.utc)
            utc_now     = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            phase       = beacon_phase(utc_dt, cw_end_s)

            # --- Collect samples ---
            try:
                samples = sdr.read_samples(n_samples)
            except Exception as e:
                print(f"  [{utc_now}] WARNING: read_samples failed: {e} -- skipping")
                next_sweep += interval_s
                continue

            # --- Compute spectrum ---
            freqs_offset, power_db = compute_power_spectrum(samples, fft_size)

            # --- Find up to max_signals peaks within analysis span ---
            peaks = find_peaks(freqs_offset, power_db, center_hz, max_signals,
                               span_hz=span_hz)

            elapsed   = time.monotonic() - sweep_start
            csv_rows  = []

            # Frequency separation between rank-1 and rank-2 (only when max_signals==2)
            freq_sep_hz = None
            if max_signals == 2 and len(peaks) == 2:
                freq_sep_hz = int(round(peaks[0][0] - peaks[1][0]))

            for rank, (peak_freq_hz, peak_power) in enumerate(peaks, start=1):
                above = 1 if peak_power >= threshold else 0

                # Drift tracking only on rank-1 signal
                if rank == 1:
                    drift_hz  = drift_tracker.update(phase, peak_freq_hz)
                    drift_str = f"{drift_hz:+d}" if drift_hz is not None else "---"
                else:
                    drift_hz  = None
                    drift_str = "---"

                rf_freq_hz = peak_freq_hz + (lo_mhz * 1e6)
                status     = "*** DETECTED ***" if above else "below threshold"
                phase_lbl  = f"[{phase}]"

                csv_rows.append({
                    "timestamp_utc"   : utc_now,
                    "location"        : location,
                    "beacon_phase"    : phase,
                    "signal_rank"     : rank,
                    "peak_freq_hz"    : f"{peak_freq_hz:.0f}",
                    "peak_power_dbfs" : f"{peak_power:.2f}",
                    "freq_drift_hz"   : drift_hz if drift_hz is not None else "",
                    "freq_sep_hz"     : freq_sep_hz if rank == 1 else "",
                    "above_threshold" : above,
                    "center_freq_hz"  : f"{center_hz:.0f}",
                    "lo_freq_mhz"     : f"{lo_mhz:.3f}",
                    "rf_freq_hz"      : f"{rf_freq_hz:.0f}"
                })

                sep_str = (f"  sep={freq_sep_hz/1e3:+.0f}kHz"
                           if rank == 1 and freq_sep_hz is not None else "")
                print(f"  {utc_now:<26} {phase_lbl:<8} {rank}  "
                      f"{peak_freq_hz/1e6:<16.4f} {peak_power:>+8.1f}  "
                      f"{drift_str:>8}  {status}"
                      + (f"  ({elapsed*1000:.0f}ms){sep_str}" if rank == 1 else ""))

            append_rows(output_path, csv_rows)
            row_count   += len(csv_rows)
            sweep_count += 1

            next_sweep += interval_s

            if duration > 0 and (time.monotonic() - start_time) >= duration:
                print(f"\nDuration {duration}s reached. {sweep_count} sweeps, {row_count} rows logged.")
                break

    except KeyboardInterrupt:
        print(f"\nStopped by user. {sweep_count} sweeps, {row_count} rows logged to {output_path}")

    finally:
        sdr.close()


def parse_args():
    p = argparse.ArgumentParser(description="NTMS 10 GHz Beacon Monitor via RTL-SDR")
    p.add_argument("--list-devices", action="store_true",
                   help="List connected RTL-SDR devices and exit")
    p.add_argument("--device",
                   default=_env("BEACON_DEVICE", DEFAULT_DEVICE),
                   help="Device index (int) or serial number string (default: 0)")
    p.add_argument("--freq",        type=float,
                   default=float(_env("BEACON_FREQ_MHZ",       DEFAULT_CENTER_MHZ)),
                   help=f"IF center frequency in MHz (default: {DEFAULT_CENTER_MHZ})")
    p.add_argument("--lo",          type=float,
                   default=float(_env("BEACON_LO_MHZ",         DEFAULT_LO_MHZ)),
                   help=f"LNB LO frequency in MHz (default: {DEFAULT_LO_MHZ})")
    p.add_argument("--interval",    type=float,
                   default=float(_env("BEACON_INTERVAL_S",     DEFAULT_INTERVAL_S)),
                   help=f"Sweep interval in seconds (default: {DEFAULT_INTERVAL_S})")
    p.add_argument("--threshold",   type=float,
                   default=float(_env("BEACON_THRESHOLD_DBFS", DEFAULT_THRESHOLD_DB)),
                   help=f"Detection threshold in dBFS (default: {DEFAULT_THRESHOLD_DB})")
    p.add_argument("--gain",        type=str,
                   default=_env("BEACON_GAIN",                  DEFAULT_GAIN),
                   help=f"RTL-SDR gain in dB, or 'auto' (default: {DEFAULT_GAIN})")
    p.add_argument("--fft",         type=int,
                   default=int(_env("BEACON_FFT_SIZE",          DEFAULT_FFT_SIZE)),
                   help=f"FFT size, power of 2 (default: {DEFAULT_FFT_SIZE})")
    p.add_argument("--output",      type=str,
                   default=_env("BEACON_OUTPUT",                DEFAULT_OUTPUT),
                   help=f"Output CSV file path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--duration",    type=float, default=0,
                   help="Run for this many seconds then exit (0=forever)")
    p.add_argument("--ppm",         type=int,
                   default=int(_env("BEACON_PPM",               DEFAULT_PPM)),
                   help=f"PPM frequency correction (default: {DEFAULT_PPM})")
    p.add_argument("--cw-end",      type=int,
                   default=int(_env("BEACON_CW_END_S",          DEFAULT_CW_END_S)),
                   dest="cw_end",
                   help=f"Seconds into odd minute where CW ends (default: {DEFAULT_CW_END_S})")
    p.add_argument("--max-signals", type=int,
                   default=int(_env("BEACON_MAX_SIGNALS",       DEFAULT_MAX_SIGNALS)),
                   dest="max_signals",
                   help=f"Max signals to detect per sweep, 1-5 (default: {DEFAULT_MAX_SIGNALS})")
    p.add_argument("--location",    type=str,
                   default=_env("BEACON_LOCATION",              DEFAULT_LOCATION),
                   help=f"Site identifier for CSV tagging (default: {DEFAULT_LOCATION})")
    p.add_argument("--span",        type=float,
                   default=float(_env("BEACON_SPAN_KHZ",        DEFAULT_SPAN_KHZ)),
                   dest="span_khz",
                   help=f"Analysis span in kHz centered on --freq (default: {DEFAULT_SPAN_KHZ} kHz = full capture)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.list_devices:
        print_device_list()
        sys.exit(0)
    if isinstance(args.device, str) and args.device.lstrip("-").isdigit():
        args.device = int(args.device)
    run_monitor(args)
