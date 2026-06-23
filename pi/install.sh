#!/bin/bash
# NTMS Beacon Station Installer
# Installs beacon_monitor_nesdr.py + beacon_reporter.py as systemd services
# on Raspberry Pi OS (Bookworm / Bullseye, 32-bit or 64-bit).
#
# Run from the repository root:
#   sudo bash pi/install.sh
#
# Safe to re-run for updates — it will not overwrite station.conf if one
# already exists (prompts to reconfigure instead).

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INSTALL_DIR=/opt/ntms-beacon
DATA_DIR=/var/lib/ntms-beacon
CONF_FILE="${INSTALL_DIR}/station.conf"
VENV="${INSTALL_DIR}/venv"
SERVICE_USER=ntms-beacon
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "  [+] $*"; }
warn()  { echo "  [!] $*"; }
die()   { echo "  [ERROR] $*" >&2; exit 1; }
ask()   { local prompt="$1" default="$2" reply
          read -r -p "      ${prompt} [${default}]: " reply
          echo "${reply:-$default}"; }
askpass() { local prompt="$1" reply
            read -r -s -p "      ${prompt}: " reply; echo; echo "$reply"; }

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash pi/install.sh"

if [[ ! -f "${REPO_ROOT}/beacon_monitor_nesdr.py" ]]; then
    die "beacon_monitor_nesdr.py not found in ${REPO_ROOT}. Run from the repo root."
fi

echo
echo "=============================================="
echo "  NTMS 10 GHz Beacon Station Installer"
echo "=============================================="
echo

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
echo "--- Installing system packages ---"
apt-get update -qq
apt-get install -y --no-install-recommends \
    librtlsdr-dev \
    rtl-sdr \
    python3-venv \
    python3-full \
    python3-pip
info "System packages installed."

# ---------------------------------------------------------------------------
# Blacklist DVB kernel module
# ---------------------------------------------------------------------------
BLACKLIST=/etc/modprobe.d/rtlsdr-blacklist.conf
if [[ ! -f "$BLACKLIST" ]]; then
    echo "blacklist dvb_usb_rtl28xxu" > "$BLACKLIST"
    info "DVB module blacklisted (${BLACKLIST})."
    info "The NESDR Smart will now be available to librtlsdr after reboot."
else
    info "DVB blacklist already in place — skipping."
fi

# ---------------------------------------------------------------------------
# System user
# ---------------------------------------------------------------------------
echo
echo "--- Creating service user ---"
if id "${SERVICE_USER}" &>/dev/null; then
    info "User '${SERVICE_USER}' already exists — skipping."
else
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --comment "NTMS Beacon Monitor" "${SERVICE_USER}"
    info "Created system user '${SERVICE_USER}'."
fi

# Add to plugdev so it can access the USB device via the rtl-sdr udev rule
if getent group plugdev &>/dev/null; then
    if ! id -nG "${SERVICE_USER}" | grep -qw plugdev; then
        usermod -aG plugdev "${SERVICE_USER}"
        info "Added '${SERVICE_USER}' to plugdev group."
    fi
fi

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
echo
echo "--- Creating directories ---"
install -d -m 755 -o root -g root          "${INSTALL_DIR}"
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}"
info "Created ${INSTALL_DIR} and ${DATA_DIR}."

# ---------------------------------------------------------------------------
# Copy scripts
# ---------------------------------------------------------------------------
echo
echo "--- Copying scripts ---"
cp "${REPO_ROOT}/beacon_monitor_nesdr.py" "${INSTALL_DIR}/"
cp "${REPO_ROOT}/beacon_reporter.py"      "${INSTALL_DIR}/"
chown root:root "${INSTALL_DIR}"/beacon_*.py
chmod 644       "${INSTALL_DIR}"/beacon_*.py
info "Scripts copied to ${INSTALL_DIR}."

# ---------------------------------------------------------------------------
# Python virtual environment
# ---------------------------------------------------------------------------
echo
echo "--- Setting up Python virtual environment ---"
if [[ ! -d "${VENV}" ]]; then
    python3 -m venv "${VENV}"
    info "Created virtualenv at ${VENV}."
else
    info "Virtualenv already exists — updating packages."
fi
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet --upgrade pyrtlsdr numpy
chown -R root:root "${VENV}"
info "pyrtlsdr and numpy installed."

# ---------------------------------------------------------------------------
# Station configuration
# ---------------------------------------------------------------------------
echo
echo "--- Station configuration ---"

RECONFIGURE=false
if [[ -f "${CONF_FILE}" ]]; then
    warn "station.conf already exists at ${CONF_FILE}."
    read -r -p "      Reconfigure this station? (y/N): " YESNO
    [[ "${YESNO,,}" == "y" ]] && RECONFIGURE=true
else
    RECONFIGURE=true
fi

if [[ "$RECONFIGURE" == "true" ]]; then
    echo
    echo "  Enter station-specific values."
    echo "  Press Enter to accept the default shown in [brackets]."
    echo

    SITE_ID=$(ask   "Site ID (e.g. KM5PO-10G-BURLESON)"    "CALLSIGN-10G-CITY")
    API_URL=$(ask   "NTMS API URL"                           "https://api.ntms.org/beacon/observation")
    API_KEY=$(askpass "NTMS API key")
    FREQ=$(ask      "IF center frequency MHz"                "618.245")
    LO=$(ask        "LNB LO frequency MHz"                   "9750.0")
    THRESHOLD=$(ask "Detection threshold dBFS"               "-50.0")
    GAIN=$(ask      "SDR gain (dB or 'auto')"                "auto")
    PPM=$(ask       "PPM correction (0=TCXO, 1-2=crystal)"  "0")
    INTERVAL=$(ask  "Sweep interval seconds"                 "10")
    DEVICE=$(ask    "Device index or serial"                 "0")

    echo
    # Write config — restrict permissions first, then write, so the key is
    # never briefly world-readable during the write.
    install -m 640 -o root -g "${SERVICE_USER}" /dev/null "${CONF_FILE}"
    cat > "${CONF_FILE}" <<EOF
# NTMS Beacon Station Configuration
# Generated by install.sh — edit carefully, then: sudo systemctl restart beacon-monitor beacon-reporter

NTMS_SITE_ID=${SITE_ID}
NTMS_API_URL=${API_URL}
NTMS_API_KEY=${API_KEY}

BEACON_OUTPUT=${DATA_DIR}/beacon_log.csv
NTMS_INPUT=${DATA_DIR}/beacon_log.csv

BEACON_FREQ_MHZ=${FREQ}
BEACON_LO_MHZ=${LO}
BEACON_THRESHOLD_DBFS=${THRESHOLD}
BEACON_GAIN=${GAIN}
BEACON_PPM=${PPM}
BEACON_INTERVAL_S=${INTERVAL}
BEACON_DEVICE=${DEVICE}
BEACON_CW_END_S=10
EOF
    info "station.conf written."
else
    info "Keeping existing station.conf."
fi

# ---------------------------------------------------------------------------
# Systemd service files
# ---------------------------------------------------------------------------
echo
echo "--- Installing systemd services ---"
cp "${SCRIPT_DIR}/beacon-monitor.service"  /etc/systemd/system/
cp "${SCRIPT_DIR}/beacon-reporter.service" /etc/systemd/system/
chmod 644 /etc/systemd/system/beacon-monitor.service
chmod 644 /etc/systemd/system/beacon-reporter.service
info "Service files installed."

systemctl daemon-reload
systemctl enable beacon-monitor.service beacon-reporter.service
info "Services enabled (will start on boot)."

# ---------------------------------------------------------------------------
# Start / restart services
# ---------------------------------------------------------------------------
echo
echo "--- Starting services ---"
systemctl restart beacon-monitor.service
systemctl restart beacon-reporter.service
sleep 3

# ---------------------------------------------------------------------------
# Status summary
# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "  Installation complete"
echo "=============================================="
echo
echo "  Scripts    : ${INSTALL_DIR}"
echo "  Data / CSV : ${DATA_DIR}/beacon_log.csv"
echo "  Config     : ${CONF_FILE}"
echo "  Logs       : journalctl -u beacon-monitor -u beacon-reporter -f"
echo
echo "  Service status:"
systemctl is-active beacon-monitor.service  && echo "    beacon-monitor   : running" \
                                            || echo "    beacon-monitor   : NOT running"
systemctl is-active beacon-reporter.service && echo "    beacon-reporter  : running" \
                                            || echo "    beacon-reporter  : NOT running"
echo
echo "  Useful commands:"
echo "    View live logs  : sudo journalctl -u beacon-monitor -u beacon-reporter -f"
echo "    Stop services   : sudo systemctl stop beacon-monitor beacon-reporter"
echo "    Restart services: sudo systemctl restart beacon-monitor beacon-reporter"
echo "    List SDR devices: ${VENV}/bin/python3 ${INSTALL_DIR}/beacon_monitor_nesdr.py --list-devices"
echo
if [[ ! -f "$BLACKLIST" ]] || systemctl is-active beacon-monitor.service &>/dev/null; then
    warn "NOTE: The DVB kernel module blacklist takes effect after a reboot."
    warn "If beacon-monitor is not running, reboot and check again:"
    warn "  sudo reboot"
fi
echo
