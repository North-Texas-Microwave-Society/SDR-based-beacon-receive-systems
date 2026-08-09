# NTMS Beacon Station - dependency bootstrap for Windows
#
#   .\setup.ps1              # interactive
#   .\setup.ps1 -Yes         # assume yes to every prompt (unattended)
#
# If PowerShell blocks the script:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Installs everything the beacon scripts need:
#   1. uv             - runs the scripts and installs their Python deps
#   2. librtlsdr.dll  - the native USB driver library behind pyrtlsdr, placed in .\lib
#   3. Zadig          - WinUSB driver for the dongle (downloaded; you click through it)
#
# Linux / Raspberry Pi / macOS users: use setup.sh instead.

[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # much faster Invoke-WebRequest

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LibDir  = Join-Path $RepoDir 'lib'

function Write-Info { param($m) Write-Host "  [+] $m" }
function Write-Warn { param($m) Write-Host "  [!] $m" -ForegroundColor Yellow }

function Confirm-Step {
    param($Message)
    if ($Yes) { return $true }
    $reply = Read-Host "      $Message [Y/n]"
    return ($reply -eq '' -or $reply -match '^[Yy]')
}

function Test-Command {
    param($Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

Write-Host ''
Write-Host '=============================================='
Write-Host '  NTMS Beacon Station - dependency setup'
Write-Host '=============================================='
Write-Host ''

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------
Write-Host '--- Step 1: uv ---'
if (Test-Command 'uv') {
    Write-Info "uv already installed ($(uv --version))."
}
elseif (Confirm-Step 'uv is not installed. Install it now?') {
    if (Test-Command 'winget') {
        winget install --id astral-sh.uv --exact --silent `
               --accept-source-agreements --accept-package-agreements
    }
    else {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }
    # The installer puts uv in %USERPROFILE%\.local\bin, not yet on this session's PATH.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Test-Command 'uv')) {
        throw 'uv was installed but is not on PATH. Open a new terminal and re-run this script.'
    }
    Write-Info "uv installed ($(uv --version))."
}
else {
    throw 'uv is required to run the beacon scripts.'
}

# ---------------------------------------------------------------------------
# 2. librtlsdr.dll
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '--- Step 2: librtlsdr ---'

$DllPath = Join-Path $LibDir 'librtlsdr.dll'

if (Test-Path $DllPath) {
    Write-Info "librtlsdr.dll already present in $LibDir."
}
elseif (Confirm-Step 'Download librtlsdr binaries from the official GitHub release?') {
    # 64-bit static build needs no extra runtime DLLs; fall back to 32-bit on x86.
    $arch  = if ([Environment]::Is64BitOperatingSystem) { 'w64' } else { 'w32' }
    $asset = "rtlsdr-bin-${arch}_static.zip"
    $url   = "https://github.com/librtlsdr/librtlsdr/releases/latest/download/$asset"
    $zip   = Join-Path $env:TEMP $asset

    Write-Info "Downloading $asset ..."
    Invoke-WebRequest -Uri $url -OutFile $zip

    New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $LibDir -Force
    Remove-Item $zip -Force

    if (Test-Path $DllPath) {
        Write-Info "librtlsdr.dll and the rtl_* tools extracted to $LibDir."
    }
    else {
        Write-Warn "Archive extracted but librtlsdr.dll was not found in $LibDir."
        Write-Warn 'Check the contents manually, or download from:'
        Write-Warn '  https://github.com/librtlsdr/librtlsdr/releases'
    }
}
else {
    Write-Warn 'Skipping librtlsdr - the monitor will not be able to open the dongle.'
}

# The beacon scripts add .\lib to the DLL search path themselves, so no PATH
# change is required. Offer it anyway for the rtl_test.exe / rtl_sdr.exe tools.
if ((Test-Path $DllPath) -and (Confirm-Step "Add $LibDir to your user PATH (for rtl_test.exe etc.)?")) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$LibDir*") {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$LibDir", 'User')
        Write-Info 'User PATH updated - takes effect in new terminals.'
    }
    else {
        Write-Info 'Already on your user PATH.'
    }
    $env:Path = "$env:Path;$LibDir"
}

# ---------------------------------------------------------------------------
# 3. Zadig / WinUSB driver
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '--- Step 3: WinUSB driver (Zadig) ---'
Write-Host '      Windows ships a TV-tuner (DVB) driver that claims RTL2832U dongles.'
Write-Host '      Zadig replaces it with WinUSB so librtlsdr can talk to the device.'
Write-Host '      This step needs a few clicks - it cannot be safely automated.'
Write-Host ''

if (Confirm-Step 'Download Zadig and launch it?') {
    $zadig = Join-Path $LibDir 'zadig.exe'
    New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    if (-not (Test-Path $zadig)) {
        Write-Info 'Downloading Zadig ...'
        Invoke-WebRequest -Uri 'https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe' `
                          -OutFile $zadig
    }
    Write-Host ''
    Write-Host '      In Zadig:'
    Write-Host '        1. Options -> List All Devices'
    Write-Host '        2. Select "Bulk-In, Interface (Interface 0)" - your RTL2832U dongle'
    Write-Host '        3. Choose WinUSB as the replacement driver'
    Write-Host '        4. Click "Replace Driver", then close Zadig'
    Write-Host ''
    Start-Process -FilePath $zadig -Verb RunAs -Wait
    Write-Info 'Zadig closed.'
}
else {
    Write-Warn 'Skipped. Run lib\zadig.exe later if the dongle is not detected.'
}

# ---------------------------------------------------------------------------
# 4. Warm the Python environment and verify
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '--- Step 4: Python dependencies ---'
& uv run --script (Join-Path $RepoDir 'beacon_monitor.py') --list-devices

Write-Host ''
Write-Host '=============================================='
Write-Host '  Setup complete'
Write-Host '=============================================='
Write-Host ''
Write-Host '  Run the monitor    :  .\run_monitor.ps1'
Write-Host '  Calibrate a station:  .\run_calibrate.ps1'
Write-Host '  Ad-hoc             :  uv run beacon_monitor.py --help'
Write-Host ''
Write-Host '  If no devices were listed above, plug in the dongle and re-run'
Write-Host '  the Zadig step to replace its driver with WinUSB.'
Write-Host ''
