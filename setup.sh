#!/bin/bash
# NTMS Beacon Station — dependency bootstrap for Linux, Raspberry Pi, and macOS
#
#   bash setup.sh              # interactive
#   bash setup.sh --yes        # assume yes to every prompt (unattended)
#
# Installs everything the beacon scripts need:
#   1. uv                     — runs the scripts and installs their Python deps
#   2. librtlsdr + rtl-sdr    — the native USB driver behind pyrtlsdr
#   3. udev rules / plugdev   — non-root access to the dongle (Linux)
#   4. DVB blacklist          — stops the TV tuner driver claiming the dongle (Linux)
#
# Windows users: use setup.ps1 instead.
#
# For an unattended Raspberry Pi station that also installs systemd services,
# use pi/install.sh — it covers everything here plus service setup.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSUME_YES=false
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then ASSUME_YES=true; fi

info() { echo "  [+] $*"; }
warn() { echo "  [!] $*"; }
die()  { echo "  [ERROR] $*" >&2; exit 1; }

confirm() {
    $ASSUME_YES && return 0
    local reply
    read -r -p "      $1 [Y/n]: " reply
    [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# sudo only where we actually need root, and only if we are not already root.
SUDO=""
if [[ $EUID -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

echo
echo "=============================================="
echo "  NTMS Beacon Station — dependency setup"
echo "=============================================="
echo

# ---------------------------------------------------------------------------
# Detect platform
# ---------------------------------------------------------------------------
OS="$(uname -s)"
PKG=""
case "$OS" in
    Linux)
        if   command -v apt-get >/dev/null 2>&1; then PKG=apt
        elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
        elif command -v pacman  >/dev/null 2>&1; then PKG=pacman
        elif command -v zypper  >/dev/null 2>&1; then PKG=zypper
        fi
        ;;
    Darwin)
        if command -v brew >/dev/null 2>&1; then PKG=brew; fi
        ;;
    *)
        die "Unsupported platform '$OS'. Windows users: run setup.ps1 in PowerShell."
        ;;
esac
info "Platform: $OS${PKG:+ (package manager: $PKG)}"

if [[ "$OS" == "Darwin" && -z "$PKG" ]]; then
    die "Homebrew not found. Install it from https://brew.sh, then re-run this script."
fi
if [[ -z "$PKG" ]]; then
    warn "No supported package manager found — install librtlsdr manually."
fi

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------
echo
echo "--- Step 1: uv ---"
if command -v uv >/dev/null 2>&1; then
    info "uv already installed ($(uv --version))."
else
    if confirm "uv is not installed. Install it now?"; then
        if [[ "$PKG" == "brew" ]]; then
            brew install uv
        else
            command -v curl >/dev/null 2>&1 || die "curl is required to install uv."
            curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
        # The installer drops uv in ~/.local/bin, which may not be on PATH yet.
        if [[ -f "${HOME}/.local/bin/env" ]]; then source "${HOME}/.local/bin/env"; fi
        export PATH="${HOME}/.local/bin:${PATH}"
        command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH — open a new terminal and re-run."
        info "uv installed ($(uv --version))."
    else
        die "uv is required to run the beacon scripts."
    fi
fi

# ---------------------------------------------------------------------------
# 2. Native librtlsdr driver
# ---------------------------------------------------------------------------
echo
echo "--- Step 2: librtlsdr ---"

librtlsdr_ok() {
    uv run --no-project --quiet --with pyrtlsdr python -c "import rtlsdr" 2>/dev/null
}

if librtlsdr_ok; then
    info "librtlsdr already present and loadable."
else
    if confirm "librtlsdr is missing. Install it with $PKG?"; then
        case "$PKG" in
            apt)
                $SUDO apt-get update -qq
                $SUDO apt-get install -y --no-install-recommends librtlsdr-dev rtl-sdr
                ;;
            dnf)    $SUDO dnf install -y rtl-sdr rtl-sdr-devel ;;
            pacman) $SUDO pacman -S --needed --noconfirm rtl-sdr ;;
            zypper) $SUDO zypper install -y rtl-sdr librtlsdr-devel ;;
            brew)   brew install librtlsdr ;;
            *)      die "Install librtlsdr manually, then re-run this script." ;;
        esac
        if librtlsdr_ok; then
            info "librtlsdr installed and loadable."
        else
            warn "librtlsdr installed but pyrtlsdr still cannot load it."
            warn "On macOS this usually means the Homebrew lib dir is not searched;"
            warn "try:  export DYLD_LIBRARY_PATH=\"\$(brew --prefix)/lib:\${DYLD_LIBRARY_PATH:-}\""
        fi
    else
        warn "Skipping librtlsdr — the monitor will not be able to open the dongle."
    fi
fi

# ---------------------------------------------------------------------------
# 3 & 4. Linux-only: USB permissions and DVB blacklist
# ---------------------------------------------------------------------------
if [[ "$OS" == "Linux" ]]; then
    echo
    echo "--- Step 3: USB device access ---"
    if getent group plugdev >/dev/null 2>&1; then
        if id -nG "$USER" | grep -qw plugdev; then
            info "$USER is already in the plugdev group."
        elif confirm "Add $USER to the plugdev group for non-root dongle access?"; then
            $SUDO usermod -aG plugdev "$USER"
            info "Added $USER to plugdev — log out and back in for this to take effect."
        fi
    else
        info "No plugdev group on this system; the rtl-sdr udev rules handle access."
    fi

    echo
    echo "--- Step 4: DVB kernel module blacklist ---"
    BLACKLIST=/etc/modprobe.d/rtlsdr-blacklist.conf
    if [[ -f "$BLACKLIST" ]]; then
        info "DVB module already blacklisted ($BLACKLIST)."
    elif confirm "Blacklist dvb_usb_rtl28xxu so librtlsdr can claim the dongle?"; then
        echo "blacklist dvb_usb_rtl28xxu" | $SUDO tee "$BLACKLIST" >/dev/null
        $SUDO modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
        info "Blacklisted. A reboot guarantees it takes effect."
    fi
fi

# ---------------------------------------------------------------------------
# 5. Warm the Python environment and verify
# ---------------------------------------------------------------------------
echo
echo "--- Step 5: Python dependencies ---"
uv run --script "${REPO_DIR}/beacon_monitor.py" --list-devices || true

echo
echo "=============================================="
echo "  Setup complete"
echo "=============================================="
echo
echo "  Run the monitor    :  bash run_monitor.sh"
echo "  Calibrate a station:  bash run_calibrate.sh"
echo "  Ad-hoc             :  uv run beacon_monitor.py --help"
echo
if [[ "$OS" == "Linux" ]]; then
    echo "  If no devices were listed above, reboot and try again — the DVB"
    echo "  blacklist and plugdev membership both need a fresh session."
    echo
fi
