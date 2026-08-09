# beacon_config.py
# Copy this file to beacon_config.py and edit for your station.
# beacon_config.py is git-ignored — each station maintains its own.
#
# All settings are optional. Omitted settings fall back to defaults.
# CLI args override config file values.

# --- SDR hardware ---
SDR_DEVICE = 0                # device index or serial string
SDR_FREQ_MHZ = 618.245        # IF center frequency
SDR_LO_MHZ = 9750.0           # LNB LO frequency
SDR_PPM = 0                   # frequency correction (0 for TCXO, 1-2 for crystal)
SDR_GAIN = "auto"             # gain in dB or "auto"
SDR_FFT_SIZE = 2048           # FFT bins

# --- Sweep ---
SWEEP_INTERVAL_S = 300        # seconds between sweeps (e.g., 300 = 5 minutes)
SWEEP_DURATION_S = 0          # 0 = run forever
CW_END_S = 10                 # seconds into odd minute where CW ends
MAX_SIGNALS = 1               # peaks per sweep (1-5)
SPAN_KHZ = 2000               # analysis span in kHz
PASSBAND_KHZ = 5              # ± bandwidth for signal vs noise separation

# --- Detection ---
THRESHOLD_DBFS = -50.0        # detection threshold

# --- Output ---
CSV_PATH = "beacon_log.csv"   # CSV output path

# --- Site ---
GRIDSQUARE = ""               # REQUIRED — Maidenhead grid square of your RECEIVER location.
                                # Must be set to where your SDR hardware is physically located.
                                # Example: "EM12il" (up to 20 chars). Reporting is disabled without it.

# --- API reporting (monitor --report) ---
API_URL = "https://prop.w5isp.com/api/v1/beacon-monitor/measurements"
MONITOR_TOKEN = ""            # from prop.w5isp.com setup page
BEACON_ID = ""                # beacon UUID
PHASE_FILTER = "CARRIER"      # only upload this phase (or "" for all)
REPORT = True                 # enable inline API reporting

# --- Calibration ---
CAL_DWELL_S = 2.0             # seconds per gain step
CAL_SETTLE_S = 0.5            # settle time after gain change
CAL_EXCLUDE_KHZ = 500.0       # exclusion zone around center when measuring noise
CAL_MARGIN_DB = 10.0          # threshold margin above noise floor
CAL_GAINS = "all"             # gain values to test, or "all" for full R820T2 sweep

# --- Standalone reporter ---
REPORTER_POLL_S = 5           # seconds between CSV poll checks
REPORTER_STATE_PATH = "beacon_reporter_state.json"
REPORTER_VERSION = "2.0.0"
