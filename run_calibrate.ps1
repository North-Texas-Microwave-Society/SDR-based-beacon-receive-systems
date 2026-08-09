# NTMS Beacon Station Calibrator - manual start script (Windows)
# Point dish at cold sky, then run from PowerShell:
#   .\run_calibrate.ps1
#
# If PowerShell blocks the script, either unblock it once:
#   Unblock-File .\run_calibrate.ps1
# or run it for this session only:
#   powershell -ExecutionPolicy Bypass -File .\run_calibrate.ps1
#
# Requires uv (https://docs.astral.sh/uv/) - it installs the dependencies
# declared inside beacon_calibrate.py automatically on first run.
#   Install uv:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#
# Also requires the librtlsdr DLLs on PATH (or next to this script):
#   https://github.com/librtlsdr/librtlsdr/releases

$ErrorActionPreference = 'Stop'

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script  = Join-Path $RepoDir 'beacon_calibrate.py'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error @"
uv is not on PATH.
  Install it with:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  Then open a new terminal and re-run this script.
"@
}

# --- RF chain ---
$Freq = 618.245        # IF center frequency (MHz) after Bullseye LNB
$Lo   = 9750.0         # LNB LO frequency (MHz)
$Ppm  = 1              # PPM correction

# --- Calibration sweep settings ---
$Dwell   = 2.0         # Seconds of IQ data collected per gain step
$Settle  = 0.5         # Seconds to wait after each gain change
$Exclude = 500         # Exclusion zone (kHz) around center when measuring noise
$Margin  = 10.0        # dB above noise floor for suggested threshold
$Gains   = 'all'       # 'all' for full R820T2 sweep, or e.g. '28.0,29.7,32.8,33.8,36.4'

# --- Output ---
# Timestamped CSV saved automatically if --output not specified

uv run --script $Script `
    --freq    $Freq `
    --lo      $Lo `
    --ppm     $Ppm `
    --dwell   $Dwell `
    --settle  $Settle `
    --exclude $Exclude `
    --margin  $Margin `
    --gains   $Gains
