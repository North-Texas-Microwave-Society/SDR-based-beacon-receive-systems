#!/usr/bin/env python3
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

Gridsquare tagging:
  --gridsquare Maidenhead grid square of the RECEIVER location (up to 20 chars)
  written to every CSV row and included in API payloads for receiver identification.

Output:
  - CSV log: one row per detected signal per sweep (long format)
  - Columns: timestamp_utc, gridsquare, beacon_phase, signal_rank,
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
   --gridsquare   Maidenhead grid square of the RECEIVER location (up to 20 chars)
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

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
DEFAULT_CENTER_MHZ    = 618.245    # 10368.370 - 9750.000 = 618.370; tuned to SA reading
DEFAULT_LO_MHZ        = 9750.0    # Bullseye LNB low-band LO (no 22 kHz tone)
DEFAULT_INTERVAL_S    = 10
DEFAULT_THRESHOLD_DB  = -50.0
DEFAULT_GAIN          = "auto"
DEFAULT_FFT_SIZE      = 2048
DEFAULT_OUTPUT        = "beacon_log.csv"
DEFAULT_CW_END_S      = 10        # seconds into odd minute where CW ends
DEFAULT_MAX_SIGNALS   = 1
DEFAULT_GRIDSQUARE     = ""
DEFAULT_SPAN_KHZ      = 2000       # analysis span in kHz (default = full 2 MHz capture)
DEFAULT_PASSBAND_KHZ   = 5         # ± bandwidth for signal vs noise separation (kHz)
DEFAULT_API_URL        = "https://prop.w5isp.com/api/v1/beacon-monitor/measurements"
REPORTER_VERSION       = "2.0.0"
DEFAULT_PPM           = 1
DEFAULT_DEVICE        = 0
SAMPLE_RATE_HZ        = 2_048_000  # 2.048 MSPS -- fits +/-1 MHz easily
SUPPRESS_HZ           = 5_000      # peak suppression window (+/-5 kHz)
CSV_FIELDS            = [
    "timestamp_utc", "gridsquare", "beacon_phase", "signal_rank",
    "peak_freq_hz", "peak_power_dbfs", "freq_drift_hz", "freq_sep_hz",
    "above_threshold", "center_freq_hz", "lo_freq_mhz", "rf_freq_hz",
    "gain_db", "noise_floor_dbfs", "signal_avg_dbfs", "snr_peak_db",
    "snr_avg_db", "signal_active_fraction", "integration_s", "passband_hz"
]
# ---------------------------------------------------------------------------


def _env(name, default):
    """Return env var as string, or str(default) if unset. Lets systemd EnvironmentFile drive the script."""
    return os.environ.get(name, str(default))


def load_config(path=None):
    """Load beacon_config.py from cwd (or explicit path). Returns dict of settings.
    Silently returns empty dict if the file doesn't exist."""
    if path is None:
        path = "beacon_config.py"
    if not os.path.isfile(path):
        return {}
    namespace = {}
    try:
        with open(path) as f:
            exec(f.read(), namespace)
    except Exception as e:
        print(f"WARNING: Could not load config file {path}: {e}")
        return {}
    return namespace


def _cfg(config, key, env_var, default):
    """Resolve a setting: config file > env var > hardcoded default."""
    if config and key in config:
        return config[key]
    env_val = os.environ.get(env_var)
    if env_val is not None:
        return env_val
    return default


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


def compute_power_spectrum(samples: np.ndarray, fft_size: int,
                           return_frames: bool = False):
    """
    Compute averaged power spectrum from IQ samples.

    Splits the sample buffer into non-overlapping FFT frames, windows each,
    computes magnitude squared, and averages across frames.

    Args:
        return_frames: if True, also return the raw complex FFT output
                       (shape: n_frames x fft_size) before averaging.

    Returns:
        freqs_offset: array of frequency offsets from center (Hz), FFT-shifted
        power_db:     array of power values in dBFS (dB relative to full scale)
        fft_frames:   (only if return_frames=True) raw complex FFT,
                       shape (n_frames, fft_size)
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

    if return_frames:
        return freqs, power_db, fft_out
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


def compute_signal_metrics(freqs_offset: np.ndarray, power_db: np.ndarray,
                           peak_freqs: list, passband_hz: float,
                           center_hz: float) -> list:
    """
    Compute noise floor, signal average, and SNR for each detected peak.

    For each peak, bins within ±passband_hz/2 of the peak frequency are
    considered "in-band"; everything else is "out-of-band".  The noise floor
    is the median of out-of-band power (ignoring bins ≤ −900 dBFS).

    Args:
        freqs_offset: FFT-shifted frequency offset array (Hz from center).
        power_db:     averaged power spectrum in dBFS.
        peak_freqs:   list of (peak_freq_hz, peak_power) from find_peaks.
        passband_hz:  half-width in Hz for in-band mask.
        center_hz:    SDR center frequency in Hz.

    Returns:
        list of (noise_floor_dbfs, signal_avg_dbfs, snr_peak_db, snr_avg_db)
        tuples, one per peak.
    """
    if isinstance(peak_freqs, tuple):
        peak_freqs = [peak_freqs]

    results = []
    for peak_freq_hz, _peak_power in peak_freqs:
        peak_offset = peak_freq_hz - center_hz
        in_band = np.abs(freqs_offset - peak_offset) <= passband_hz / 2

        in_band_power  = power_db[in_band]
        out_of_band_power = power_db[~in_band]

        # Only consider real bins for noise floor
        valid_noise = out_of_band_power[out_of_band_power > -900]
        noise_floor = float(np.median(valid_noise)) if len(valid_noise) > 0 else -999.0

        signal_peak = float(np.max(in_band_power)) if len(in_band_power) > 0 else -999.0
        signal_avg  = float(np.mean(in_band_power)) if len(in_band_power) > 0 else -999.0

        snr_peak = signal_peak - noise_floor
        snr_avg  = signal_avg  - noise_floor

        results.append((noise_floor, signal_avg, snr_peak, snr_avg))

    return results


def compute_active_fraction(fft_frames: np.ndarray, freqs_offset: np.ndarray,
                            fft_size: int, peak_freq_hz: float,
                            passband_hz: float, noise_floor_dbfs: float,
                            center_hz: float) -> float:
    """
    Fraction of FFT frames where the peak in-band power exceeds the noise floor.

    For each frame the per-frame power spectrum is computed (dBFS), shifted,
    and the maximum value within the passband is compared to
    noise_floor_dbfs + 3 dB.

    Args:
        fft_frames:      raw complex FFT output, shape (n_frames, fft_size).
        freqs_offset:    FFT-shifted frequency offsets (Hz from center).
        fft_size:        FFT size (samples per frame).
        peak_freq_hz:    absolute peak frequency in Hz.
        passband_hz:     passband half-width (Hz).
        noise_floor_dbfs: reference noise floor in dBFS.
        center_hz:       SDR center frequency in Hz.

    Returns:
        active fraction (0.0 to 1.0).
    """
    n_frames = fft_frames.shape[0]
    if n_frames == 0:
        return 0.0

    peak_offset  = peak_freq_hz - center_hz
    in_band_mask = np.abs(freqs_offset - peak_offset) <= passband_hz / 2

    if not np.any(in_band_mask):
        return 0.0

    # Per-frame power in dBFS, shifted to align with freqs_offset
    power       = np.abs(fft_frames) ** 2
    power_db    = 10 * np.log10(power / (fft_size ** 2) + 1e-12)
    power_db_s  = np.fft.fftshift(power_db, axes=1)

    in_band_max = np.max(power_db_s[:, in_band_mask], axis=1)
    active      = np.sum(in_band_max > noise_floor_dbfs + 3.0)

    return float(active) / n_frames


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
        if phase != "CARRIER":
            return None

        drift = None
        if self._last_carrier_freq_hz is not None:
            drift = round(peak_freq_hz - self._last_carrier_freq_hz)
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


# ---------------------------------------------------------------------------
# API reporting (inline, single-attempt POST — CSV is the durable record)
# ---------------------------------------------------------------------------

def build_report_payload(row: dict, beacon_id: str,
                         passband_hz_default: float, version: str) -> dict:
    """Convert a CSV row dict into the NTMS Prop API measurement payload."""
    peak_power = float(row.get("peak_power_dbfs", -999))
    return {
        "beacon_id"             : beacon_id,
        "gridsquare"            : (row.get("gridsquare", "") or "").strip(),
        "frequency_hz"          : int(float(row.get("center_freq_hz", 0))),
        "measured_at"           : row.get("timestamp_utc", ""),
        "integration_s"         : int(float(row.get("integration_s", 60))),
        "passband_hz"           : float(row.get("passband_hz", passband_hz_default)),
        "gain_db"               : float(row.get("gain_db", 0)),
        "noise_floor_dbfs"      : float(row.get("noise_floor_dbfs", -999)),
        "signal_peak_dbfs"      : float(peak_power),
        "signal_avg_dbfs"       : float(row.get("signal_avg_dbfs", peak_power)),
        "snr_peak_db"           : float(row.get("snr_peak_db", 0)),
        "snr_avg_db"            : float(row.get("snr_avg_db", 0)),
        "signal_active_fraction": float(row.get("signal_active_fraction", 0)),
        "propmonitor_version"   : version
    }


def post_observation(url: str, monitor_token: str, payload: dict,
                     dry_run: bool) -> bool:
    """
    POST a single measurement to the NTMS API.  Single attempt, no retry.
    Returns True if the POST succeeded (204), False otherwise.
    CSV is the durable record — failed POSTs can be backfilled later.
    """
    if dry_run:
        print(f"  [DRY RUN] Would POST: {json.dumps(payload)}")
        return True

    data    = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type"  : "application/json",
        "Authorization" : f"Bearer {monitor_token}",
        "User-Agent"    : "NTMS-BeaconReporter/2.0"
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.getcode() == 204:
                return True
            body = resp.read(200).decode("utf-8", errors="replace")
            print(f"  WARNING: API returned unexpected HTTP {resp.getcode()}: {body[:120]}")
            return False
    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", errors="replace")
        if e.code == 401:
            print(f"  FATAL: HTTP {e.code} — bad monitor token. Check NTMS_MONITOR_TOKEN.")
            sys.exit(1)
        print(f"  ERROR: HTTP {e.code} from API: {body[:120]}")
        return False
    except urllib.error.URLError as e:
        print(f"  ERROR: Network error: {e.reason}")
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


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
    gridsquare  = args.gridsquare
    span_hz     = args.span_khz * 1000
    passband_hz = int(args.passband_khz * 1000)

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
    print(f"  Gridsquare    : {gridsquare}")
    print(f"  Samples/sweep : {n_samples:,}")
    print(f"  Output file   : {output_path}")
    print(f"  Duration      : {'forever' if duration == 0 else f'{duration}s'}")

    report     = args.report
    report_cnt = 0
    if report:
        print(f"  API endpoint  : {args.api_url}")
        print(f"  Phase filter  : {args.phase_filter or '(none — uploading all phases)'}")
        if args.dry_run:
            print("  Dry run       : YES (no data sent)")
        gs = gridsquare.strip() if gridsquare else ""
        if not gs:
            print()
            print("  ERROR: No receiver gridsquare set. Reporting is disabled without receiver location.")
            print('    Fix: Set GRIDSQUARE in beacon_config.py or pass --gridsquare "EM12il"')
            print("    This must be the grid square where your SDR receiver is physically located.")
            sys.exit(1)
        elif len(gs) < 4:
            print()
            print(f"  WARNING: Receiver gridsquare '{gs}' is shorter than 4 characters — is this correct?")
        elif len(gs) > 20:
            print()
            print(f"  WARNING: Receiver gridsquare '{gs}' exceeds 20 characters. It will be truncated by the API.")
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
    gain_db = float(sdr.gain)
    integration_s_val = int(interval_s)
    print(f"OK  (device={serial}, gain={gain_db} dB)")
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
            freqs_offset, power_db, fft_frames = compute_power_spectrum(
                samples, fft_size, return_frames=True)

            # --- Find up to max_signals peaks within analysis span ---
            peaks = find_peaks(freqs_offset, power_db, center_hz, max_signals,
                               span_hz=span_hz)

            # --- Per-peak signal metrics ---
            metrics = compute_signal_metrics(freqs_offset, power_db, peaks,
                                             passband_hz, center_hz)

            elapsed   = time.monotonic() - sweep_start
            csv_rows  = []

            # Frequency separation between rank-1 and rank-2 (only when max_signals==2)
            freq_sep_hz = None
            if max_signals == 2 and len(peaks) == 2:
                freq_sep_hz = round(peaks[0][0] - peaks[1][0])

            for rank, (peak_freq_hz, peak_power) in enumerate(peaks, start=1):
                metric_idx = rank - 1
                noise_floor, signal_avg, snr_peak, snr_avg = metrics[metric_idx]

                above = 1 if peak_power >= threshold else 0

                # Drift tracking only on rank-1 signal
                if rank == 1:
                    drift_hz  = drift_tracker.update(phase, peak_freq_hz)
                    drift_str = f"{drift_hz:+d}" if drift_hz is not None else "---"

                    # Active fraction only for rank-1
                    active_frac = compute_active_fraction(
                        fft_frames, freqs_offset, fft_size,
                        peak_freq_hz, passband_hz, noise_floor, center_hz)
                else:
                    drift_hz   = None
                    drift_str  = "---"
                    active_frac = ""

                rf_freq_hz = peak_freq_hz + (lo_mhz * 1e6)
                status     = "*** DETECTED ***" if above else "below threshold"
                phase_lbl  = f"[{phase}]"

                csv_rows.append({
                    "timestamp_utc"        : utc_now,
                    "gridsquare"           : gridsquare,
                    "beacon_phase"         : phase,
                    "signal_rank"          : rank,
                    "peak_freq_hz"         : f"{peak_freq_hz:.0f}",
                    "peak_power_dbfs"      : f"{peak_power:.2f}",
                    "freq_drift_hz"        : drift_hz if drift_hz is not None else "",
                    "freq_sep_hz"          : freq_sep_hz if rank == 1 else "",
                    "above_threshold"      : above,
                    "center_freq_hz"       : f"{center_hz:.0f}",
                    "lo_freq_mhz"          : f"{lo_mhz:.3f}",
                    "rf_freq_hz"           : f"{rf_freq_hz:.0f}",
                    "gain_db"              : f"{gain_db:.1f}",
                    "noise_floor_dbfs"     : f"{noise_floor:.2f}",
                    "signal_avg_dbfs"      : f"{signal_avg:.2f}",
                    "snr_peak_db"          : f"{snr_peak:.2f}",
                    "snr_avg_db"           : f"{snr_avg:.2f}",
                    "signal_active_fraction": f"{active_frac:.3f}" if isinstance(active_frac, float) else "",
                    "integration_s"        : f"{integration_s_val}",
                    "passband_hz"          : f"{passband_hz}"
                })

                sep_str = (f"  sep={freq_sep_hz/1e3:+.0f}kHz"
                           if rank == 1 and freq_sep_hz is not None else "")
                snr_str = f"  SNR={snr_peak:.1f}dB" if rank == 1 else ""
                print(f"  {utc_now:<26} {phase_lbl:<8} {rank}  "
                      f"{peak_freq_hz/1e6:<16.4f} {peak_power:>+8.1f}  "
                      f"{drift_str:>8}  {status}"
                      + (f"  ({elapsed*1000:.0f}ms){snr_str}{sep_str}" if rank == 1 else ""))

            append_rows(output_path, csv_rows)
            row_count   += len(csv_rows)
            sweep_count += 1

            if report:
                for row in csv_rows:
                    phase = row.get("beacon_phase", "")
                    if args.phase_filter and phase != args.phase_filter:
                        continue
                    payload = build_report_payload(row, args.beacon_id,
                                                   passband_hz, REPORTER_VERSION)
                    if post_observation(args.api_url, args.monitor_token,
                                       payload, args.dry_run):
                        report_cnt += 1
                        pwr = float(row.get("peak_power_dbfs", -999))
                        rf  = row.get("rf_freq_hz", "?")
                        tag = "*** DETECTED ***" if pwr >= threshold else ""
                        print(f"  \u2191 SENT  {rf} Hz  {pwr:+.1f} dBFS  {tag}".rstrip())

            next_sweep += interval_s

            if duration > 0 and (time.monotonic() - start_time) >= duration:
                msg = f"\nDuration {duration}s reached. {sweep_count} sweeps, {row_count} rows logged to {output_path}"
                if report and report_cnt:
                    msg += f"  ({report_cnt} reported)"
                print(msg)
                break

    except KeyboardInterrupt:
        msg = f"\nStopped by user. {sweep_count} sweeps, {row_count} rows logged to {output_path}"
        if report and report_cnt:
            msg += f"  ({report_cnt} reported)"
        print(msg)

    finally:
        sdr.close()


def parse_args():
    # --- Auto-load beacon_config.py from cwd (or explicit --config path) ---
    config_path = "beacon_config.py"
    for i, a in enumerate(sys.argv):
        if i == 0:
            continue
        if a == "--config" and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]
            break
        if a.startswith("--config="):
            config_path = a.split("=", 1)[1]
            break
    config = load_config(config_path) if config_path else load_config()

    def c(key, env_var, default):
        return _cfg(config, key, env_var, default)

    p = argparse.ArgumentParser(description="NTMS 10 GHz Beacon Monitor via RTL-SDR")
    p.add_argument("--config", default=None,
                   help="Optional path to beacon_config.py (default: auto-detect in cwd)")
    p.add_argument("--list-devices", action="store_true",
                   help="List connected RTL-SDR devices and exit")
    p.add_argument("--device",
                   default=c("SDR_DEVICE", "BEACON_DEVICE", DEFAULT_DEVICE),
                   help="Device index (int) or serial number string")
    p.add_argument("--freq", type=float,
                   default=float(c("SDR_FREQ_MHZ", "BEACON_FREQ_MHZ", DEFAULT_CENTER_MHZ)),
                   help=f"IF center frequency in MHz (default: {DEFAULT_CENTER_MHZ})")
    p.add_argument("--lo", type=float,
                   default=float(c("SDR_LO_MHZ", "BEACON_LO_MHZ", DEFAULT_LO_MHZ)),
                   help=f"LNB LO frequency in MHz (default: {DEFAULT_LO_MHZ})")
    p.add_argument("--interval", type=float,
                   default=float(c("SWEEP_INTERVAL_S", "BEACON_INTERVAL_S", DEFAULT_INTERVAL_S)),
                   help=f"Sweep interval in seconds (default: {DEFAULT_INTERVAL_S})")
    p.add_argument("--threshold", type=float,
                   default=float(c("THRESHOLD_DBFS", "BEACON_THRESHOLD_DBFS", DEFAULT_THRESHOLD_DB)),
                   help=f"Detection threshold in dBFS (default: {DEFAULT_THRESHOLD_DB})")
    p.add_argument("--gain", type=str,
                   default=str(c("SDR_GAIN", "BEACON_GAIN", DEFAULT_GAIN)),
                   help=f"RTL-SDR gain in dB, or 'auto' (default: {DEFAULT_GAIN})")
    p.add_argument("--fft", type=int,
                   default=int(c("SDR_FFT_SIZE", "BEACON_FFT_SIZE", DEFAULT_FFT_SIZE)),
                   help=f"FFT size, power of 2 (default: {DEFAULT_FFT_SIZE})")
    p.add_argument("--output", type=str,
                   default=str(c("CSV_PATH", "BEACON_OUTPUT", DEFAULT_OUTPUT)),
                   help=f"Output CSV file path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--duration", type=float,
                   default=float(c("SWEEP_DURATION_S", "", 0)),
                   help="Run for this many seconds then exit (0=forever)")
    p.add_argument("--ppm", type=int,
                   default=int(c("SDR_PPM", "BEACON_PPM", DEFAULT_PPM)),
                   help=f"PPM frequency correction (default: {DEFAULT_PPM})")
    p.add_argument("--cw-end", type=int,
                   default=int(c("CW_END_S", "BEACON_CW_END_S", DEFAULT_CW_END_S)),
                   dest="cw_end",
                   help=f"Seconds into odd minute where CW ends (default: {DEFAULT_CW_END_S})")
    p.add_argument("--max-signals", type=int,
                   default=int(c("MAX_SIGNALS", "BEACON_MAX_SIGNALS", DEFAULT_MAX_SIGNALS)),
                   dest="max_signals",
                   help=f"Max signals to detect per sweep, 1-5 (default: {DEFAULT_MAX_SIGNALS})")
    p.add_argument("--gridsquare", type=str,
                   default=str(c("GRIDSQUARE", "NTMS_GRIDSQUARE", DEFAULT_GRIDSQUARE)),
                   help="Maidenhead grid square of the RECEIVER location (up to 20 chars)")
    p.add_argument("--span", type=float,
                   default=float(c("SPAN_KHZ", "BEACON_SPAN_KHZ", DEFAULT_SPAN_KHZ)),
                   dest="span_khz",
                   help=f"Analysis span in kHz centered on --freq (default: {DEFAULT_SPAN_KHZ})")
    p.add_argument("--passband-khz", type=float,
                   default=float(c("PASSBAND_KHZ", "BEACON_PASSBAND_KHZ", DEFAULT_PASSBAND_KHZ)),
                   dest="passband_khz",
                   help=f"± bandwidth in kHz for signal vs noise separation (default: {DEFAULT_PASSBAND_KHZ})")
    p.add_argument("--report", action="store_true",
                   default=c("REPORT", "", False),
                   help="After each sweep, POST observations to the NTMS API")
    p.add_argument("--monitor-token",
                   default=str(c("MONITOR_TOKEN", "NTMS_MONITOR_TOKEN", "")),
                   help="Monitor token for NTMS API authentication")
    p.add_argument("--beacon-id",
                   default=str(c("BEACON_ID", "NTMS_BEACON_ID", "")),
                   help="Beacon UUID for NTMS API")
    p.add_argument("--api-url",
                   default=str(c("API_URL", "NTMS_API_URL", DEFAULT_API_URL)),
                   help="NTMS API endpoint URL")
    p.add_argument("--phase-filter",
                   default=str(c("PHASE_FILTER", "NTMS_PHASE_FILTER", "")),
                   help="Only upload rows matching this beacon_phase (e.g. CARRIER)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print payloads but do not POST to API")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.list_devices:
        print_device_list()
        sys.exit(0)
    if isinstance(args.device, str) and args.device.lstrip("-").isdigit():
        args.device = int(args.device)
    if (args.report and not args.dry_run
            and (not args.monitor_token or not args.beacon_id)):
        print("ERROR: --report requires --monitor-token and --beacon-id "
              "(set them in beacon_config.py or via env vars)")
        sys.exit(1)
    run_monitor(args)
