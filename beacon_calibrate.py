#!/usr/bin/env python3
"""
NTMS 10 GHz Beacon Station Calibrator
======================================
Sweeps RTL-SDR gain settings and measures the IF noise floor to find the
optimal gain and detection threshold for your LNB-SDR system.

HOW TO USE:
  1. Point the dish at cold sky (away from beacon, sun, and ground clutter).
  2. Run this script with your site's IF frequency and LNB LO.
  3. Read the recommendation at the bottom — it gives you --gain and --threshold
     values to plug into beacon_monitor.py.

The script steps through all R820T2 gain settings (0–49.6 dB), measures the
noise floor at each, and identifies the "knee" — the gain where LNB thermal
noise begins to dominate over SDR ADC quantization noise.  Past the knee,
noise floor rises ~1 dB per 1 dB of added gain.  The optimal operating point
is just past the knee; the threshold is set a fixed margin above that floor.

Usage:
  python beacon_calibrate.py [options]

  --freq      IF center frequency in MHz       (default: 618.245, or BEACON_FREQ_MHZ env)
  --lo        LNB LO frequency in MHz          (default: 9750.0,  or BEACON_LO_MHZ env)
  --ppm       PPM correction                   (default: 1,       or BEACON_PPM env)
  --fft       FFT size, power of 2             (default: 4096)
  --dwell     Seconds of data per gain step    (default: 2.0)
  --settle    Settle time after gain change, s (default: 0.5)
  --exclude   Exclusion zone around center, kHz (default: 500)
  --margin    Threshold margin above noise, dB  (default: 10.0)
  --gains     Comma-separated dB values, or 'all' (default: all R820T2 steps)
  --device    Device index or serial string    (default: 0)
  --output    CSV output file                  (default: beacon_cal_<timestamp>.csv)
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
    sys.exit(1)

# All valid R820T2 / R828D gain steps in dB
R820T2_GAINS = [
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6,
    19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2,
    38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6
]

SAMPLE_RATE_HZ      = 2_048_000
DEFAULT_CENTER_MHZ  = 618.245
DEFAULT_LO_MHZ      = 9750.0
DEFAULT_FFT_SIZE    = 4096
DEFAULT_DWELL_S     = 2.0
DEFAULT_SETTLE_S    = 0.5
DEFAULT_EXCLUDE_KHZ = 500.0
DEFAULT_MARGIN_DB   = 10.0
DEFAULT_PPM         = 1
DEFAULT_DEVICE      = 0

# dN/dG ratio at or above which we consider the system past the knee
KNEE_RATIO = 0.85


def _env(name, default):
    return os.environ.get(name, str(default))


def load_config(path=None):
    """Load beacon_config.py from cwd (or explicit path). Returns dict of settings."""
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


def compute_power_spectrum(samples: np.ndarray, fft_size: int):
    n_frames = len(samples) // fft_size
    if n_frames == 0:
        raise ValueError(f"Not enough samples for FFT size {fft_size}")
    samples  = samples[:n_frames * fft_size].reshape(n_frames, fft_size)
    window   = np.hanning(fft_size)
    fft_out  = np.fft.fft(samples * window, axis=1)
    power    = np.mean(np.abs(fft_out) ** 2, axis=0)
    power_db = 10 * np.log10(np.fft.fftshift(power) / (fft_size ** 2) + 1e-12)
    freqs    = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / SAMPLE_RATE_HZ))
    return freqs, power_db


def measure_noise_floor(sdr: RtlSdr, fft_size: int, dwell_s: float,
                        exclude_hz: float) -> float:
    """
    Collect dwell_s seconds of IQ samples, compute the averaged power spectrum,
    and return the median power (dBFS) excluding ±exclude_hz around center.
    """
    n_samples = int(SAMPLE_RATE_HZ * dwell_s)
    n_samples = (n_samples // fft_size) * fft_size

    samples = np.array(sdr.read_samples(n_samples))
    freqs, power_db = compute_power_spectrum(samples, fft_size)

    mask = np.abs(freqs) > exclude_hz
    if not np.any(mask):
        mask = np.ones(len(freqs), dtype=bool)

    return float(np.median(power_db[mask]))


def open_sdr(center_mhz: float, ppm: int, device_spec) -> RtlSdr:
    if isinstance(device_spec, str) and not device_spec.lstrip("-").isdigit():
        sdr = RtlSdr(serial_number=device_spec)
    else:
        sdr = RtlSdr(device_index=int(device_spec))
    sdr.sample_rate = SAMPLE_RATE_HZ
    sdr.center_freq = int(center_mhz * 1e6)
    if ppm != 0:
        sdr.freq_correction = ppm
    return sdr


def run_calibration(args) -> None:
    center_hz   = args.freq * 1e6
    exclude_hz  = args.exclude * 1e3
    timestamp   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = args.output or f"beacon_cal_{timestamp}.csv"

    if args.gains == "all":
        gains = R820T2_GAINS
    else:
        try:
            gains = sorted(float(g.strip()) for g in args.gains.split(","))
        except ValueError:
            print("ERROR: --gains must be 'all' or comma-separated dB values")
            sys.exit(1)

    est_s = len(gains) * (args.settle + args.dwell)

    print()
    print("NTMS Beacon Station Calibrator")
    print("================================")
    print(f"  IF center  : {args.freq:.3f} MHz")
    print(f"  LNB LO     : {args.lo:.3f} MHz")
    print(f"  RF (approx): {args.freq + args.lo:.3f} MHz")
    print(f"  FFT size   : {args.fft} bins  ({SAMPLE_RATE_HZ / args.fft:.0f} Hz/bin)")
    print(f"  Dwell/step : {args.dwell:.1f} s  ({int(SAMPLE_RATE_HZ * args.dwell):,} samples, "
          f"{int(SAMPLE_RATE_HZ * args.dwell) // args.fft} frames)")
    print(f"  Settle time: {args.settle} s")
    print(f"  Exclude    : ±{args.exclude:.0f} kHz around center")
    print(f"  Margin     : {args.margin:.1f} dB above noise floor → threshold")
    print(f"  Gain steps : {len(gains)}  ({gains[0]:.1f}–{gains[-1]:.1f} dB)")
    print(f"  Estimated  : ~{est_s:.0f} s ({est_s/60:.1f} min)")
    print(f"  Output     : {output_path}")
    print()
    print("Point dish at cold sky, then press Enter to begin (Ctrl+C to abort)...")
    try:
        input()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    print("Opening SDR... ", end="", flush=True)
    try:
        sdr = open_sdr(args.freq, args.ppm, args.device)
        sdr.gain = gains[0]
        time.sleep(args.settle)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
    print("OK\n")

    print(f"  {'Gain':>7}  {'Noise floor':>11}  {'Δ noise':>8}  {'Δ gain':>7}  {'Ratio':>6}  Note")
    print(f"  {'dB':>7}  {'dBFS':>11}  {'dB':>8}  {'dB':>7}  {'dN/dG':>6}")
    print(f"  {'-'*7}  {'-'*11}  {'-'*8}  {'-'*7}  {'-'*6}  {'-'*20}")

    results    = []
    knee_gain  = None
    knee_noise = None
    prev_gain  = None
    prev_noise = None

    try:
        for gain in gains:
            sdr.gain = gain
            time.sleep(args.settle)

            noise = measure_noise_floor(sdr, args.fft, args.dwell, exclude_hz)

            delta_noise = delta_gain = ratio = None
            note = ""

            if prev_noise is not None:
                delta_noise = noise - prev_noise
                delta_gain  = gain  - prev_gain
                if delta_gain > 0:
                    ratio = delta_noise / delta_gain
                    if ratio >= KNEE_RATIO and knee_gain is None:
                        knee_gain  = gain
                        knee_noise = noise
                        note = "*** KNEE ***"

            results.append({
                "gain_db":           gain,
                "noise_floor_dbfs":  noise,
                "delta_noise_db":    delta_noise,
                "delta_gain_db":     delta_gain,
                "ratio_dn_dg":       ratio,
            })

            dn_str    = f"{delta_noise:+.2f}" if delta_noise is not None else "---"
            dg_str    = f"{delta_gain:+.1f}"  if delta_gain  is not None else "---"
            ratio_str = f"{ratio:.2f}"         if ratio       is not None else "---"
            print(f"  {gain:>7.1f}  {noise:>+11.2f}  {dn_str:>8}  {dg_str:>7}  {ratio_str:>6}  {note}")

            prev_gain  = gain
            prev_noise = noise

    except KeyboardInterrupt:
        print("\nScan interrupted by user.")
    finally:
        sdr.close()

    # Save CSV
    fields = ["gain_db", "noise_floor_dbfs", "delta_noise_db", "delta_gain_db", "ratio_dn_dg"]
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    # Recommendation
    print()
    print("=" * 60)
    print(f"  Results saved to: {output_path}")
    print()

    if knee_gain is not None:
        threshold = knee_noise + args.margin
        print(f"  Optimal gain       : {knee_gain:.1f} dB")
        print(f"  Noise floor @ knee : {knee_noise:+.1f} dBFS")
        print(f"  Suggested threshold: {threshold:+.1f} dBFS  (noise + {args.margin:.0f} dB margin)")
        print()
        print("  Add to your beacon_monitor.py command line:")
        print(f"    --gain {knee_gain} --threshold {threshold:.1f}")
    elif results:
        # Knee not found — pick the gain closest to ratio=1.0
        valid = [r for r in results if r["ratio_dn_dg"] is not None]
        if valid:
            best = min(valid, key=lambda r: abs(r["ratio_dn_dg"] - 1.0))
            print("  NOTE: Clear knee not identified in this gain range.")
            print(f"  Closest to 1:1 at gain {best['gain_db']:.1f} dB "
                  f"(noise {best['noise_floor_dbfs']:+.1f} dBFS, ratio {best['ratio_dn_dg']:.2f})")
            print()
            print("  Tips:")
            print("  - Ensure dish is pointed at cold sky (not ground, buildings, or sun)")
            print("  - If ratio never reaches 0.85, LNB gain may be unusually low — try higher gain steps")
            print("  - If ratio exceeds 1.0 at low gain, the LNB is very hot — results still valid")
        else:
            print("  No valid ratio data — only one gain step was measured.")

    print("=" * 60)
    print()


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
    config = load_config(config_path)

    def c(key, env_var, default):
        return _cfg(config, key, env_var, default)

    p = argparse.ArgumentParser(
        description="NTMS 10 GHz Beacon Station Calibrator — gain sweep and noise floor analysis"
    )
    p.add_argument("--config", default=None,
                   help="Optional path to beacon_config.py (default: auto-detect in cwd)")
    p.add_argument("--freq",    type=float,
                   default=float(c("SDR_FREQ_MHZ", "BEACON_FREQ_MHZ", DEFAULT_CENTER_MHZ)),
                   help=f"IF center frequency in MHz (default: {DEFAULT_CENTER_MHZ})")
    p.add_argument("--lo",      type=float,
                   default=float(c("SDR_LO_MHZ", "BEACON_LO_MHZ", DEFAULT_LO_MHZ)),
                   help=f"LNB LO frequency in MHz (default: {DEFAULT_LO_MHZ})")
    p.add_argument("--ppm",     type=int,
                   default=int(c("SDR_PPM", "BEACON_PPM", DEFAULT_PPM)),
                   help=f"PPM frequency correction (default: {DEFAULT_PPM})")
    p.add_argument("--fft",     type=int,
                   default=int(c("SDR_FFT_SIZE", "BEACON_FFT_SIZE", DEFAULT_FFT_SIZE)),
                   help=f"FFT size, power of 2 (default: {DEFAULT_FFT_SIZE})")
    p.add_argument("--dwell",   type=float,
                   default=float(c("CAL_DWELL_S", "", DEFAULT_DWELL_S)),
                   help=f"Seconds of IQ data to collect per gain step (default: {DEFAULT_DWELL_S})")
    p.add_argument("--settle",  type=float,
                   default=float(c("CAL_SETTLE_S", "", DEFAULT_SETTLE_S)),
                   help=f"Settle time after each gain change in seconds (default: {DEFAULT_SETTLE_S})")
    p.add_argument("--exclude", type=float,
                   default=float(c("CAL_EXCLUDE_KHZ", "", DEFAULT_EXCLUDE_KHZ)),
                   help=f"Exclusion zone ±kHz around center (default: {DEFAULT_EXCLUDE_KHZ})")
    p.add_argument("--margin",  type=float,
                   default=float(c("CAL_MARGIN_DB", "", DEFAULT_MARGIN_DB)),
                   help=f"Threshold margin above noise floor in dB (default: {DEFAULT_MARGIN_DB})")
    p.add_argument("--gains",   type=str,
                   default=str(c("CAL_GAINS", "", "all")),
                   help="Comma-separated gain values to test in dB, or 'all' (default: all R820T2 steps)")
    p.add_argument("--device",
                   default=c("SDR_DEVICE", "BEACON_DEVICE", DEFAULT_DEVICE),
                   help="Device index (int) or serial number string (default: 0)")
    p.add_argument("--output",  type=str,   default=None,
                   help="CSV output file (default: beacon_cal_<UTC timestamp>.csv)")
    return p.parse_args()


if __name__ == "__main__":
    run_calibration(parse_args())
