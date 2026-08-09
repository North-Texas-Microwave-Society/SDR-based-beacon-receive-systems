# NTMS Beacon Monitor - manual start script (Windows)
# Edit the options below, then run from PowerShell:
#   .\run_monitor.ps1
#
# If PowerShell blocks the script, either unblock it once:
#   Unblock-File .\run_monitor.ps1
# or run it for this session only:
#   powershell -ExecutionPolicy Bypass -File .\run_monitor.ps1
#
# Requires uv (https://docs.astral.sh/uv/) - it installs the dependencies
# declared inside beacon_monitor.py automatically on first run.
#   Install uv:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#
# Also requires the librtlsdr DLLs on PATH (or next to this script):
#   https://github.com/librtlsdr/librtlsdr/releases

$ErrorActionPreference = 'Stop'

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script  = Join-Path $RepoDir 'beacon_monitor.py'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error @"
uv is not on PATH.
  Install it with:  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  Then open a new terminal and re-run this script.
"@
}

# --- Site ---
$Location = 'KM5PO-10G-BURLESON'

# --- RF chain ---
$Freq = 618.245        # IF center frequency (MHz) after Bullseye LNB
$Lo   = 9750.0         # LNB LO frequency (MHz)
$Ppm  = 1              # PPM correction (0 for TCXO; 1-2 for standard crystal)

# --- SDR gain and detection ---
# Run beacon_calibrate.py to find optimal values for your setup.
$Gain      = 36.4      # R820T2 gain in dB (or: auto)
$Threshold = -35.0     # Detection threshold in dBFS

# --- Sweep ---
$Interval   = 10       # Sweep interval in seconds
$MaxSignals = 1        # Max signals to detect per sweep (1-5)
$Span       = 2000     # Analysis span in kHz (2000 = full 2 MHz capture)

# --- Output ---
$Output = Join-Path $RepoDir 'beacon_log.csv'

# --- Run ---
uv run --script $Script `
    --location    $Location `
    --freq        $Freq `
    --lo          $Lo `
    --ppm         $Ppm `
    --gain        $Gain `
    --threshold   $Threshold `
    --interval    $Interval `
    --max-signals $MaxSignals `
    --span        $Span `
    --output      $Output
