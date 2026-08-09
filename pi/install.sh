#!/bin/bash
# NTMS Beacon Station Installer
# Installs beacon_monitor_nesdr.py + beacon_reporter.py as systemd services
# on Raspberry Pi OS (Bookworm / Bullseye, 32-bit or 64-bit).
#
# Run from the repository root:
#   sudo bash pi/install.sh
#
# Safe to re-run for updates — it will not overwrite drop-in config files if
# they already exist (prompts to reconfigure instead).

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INSTALL_DIR=/opt/ntms-beacon
DATA_DIR=/var/lib/ntms-beacon
VENV="${INSTALL_DIR}/venv"
SERVICE_USER=ntms-beacon
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MONITOR_DROPDIR=/etc/systemd/system/beacon-monitor.service.d
REPORTER_DROPDIR=/etc/systemd/system/beacon-reporter.service.d
MONITOR_DROPIN="${MONITOR_DROPDIR}/env.conf"
REPORTER_DROPIN="${REPORTER_DROPDIR}/env.conf"

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

if [[ ! -f "${REPO_ROOT}/beacon_monitor.py" ]]; then
    die "beacon_monitor.py not found in ${REPO_ROOT}. Run from the repo root."
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
mkdir -p "${MONITOR_DROPDIR}" "${REPORTER_DROPDIR}"
info "Created ${INSTALL_DIR}, ${DATA_DIR}, and systemd drop-in directories."

# ---------------------------------------------------------------------------
# Copy scripts
# ---------------------------------------------------------------------------
echo
echo "--- Copying scripts ---"
cp "${REPO_ROOT}/beacon_monitor.py"    "${INSTALL_DIR}/"
cp "${REPO_ROOT}/beacon_calibrate.py" "${INSTALL_DIR}/"
cp "${REPO_ROOT}/beacon_reporter.py"  "${INSTALL_DIR}/"
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
# Station configuration (systemd drop-in files)
# ---------------------------------------------------------------------------
echo
echo "--- Station configuration ---"

ANY_DROPIN_EXISTS=false
[[ -f "${MONITOR_DROPIN}" ]] && ANY_DROPIN_EXISTS=true
[[ -f "${REPORTER_DROPIN}" ]] && ANY_DROPIN_EXISTS=true

RECONFIGURE=false
if [[ "$ANY_DROPIN_EXISTS" == "true" ]]; then
    warn "Drop-in config already exists at ${MONITOR_DROPDIR}/ and/or ${REPORTER_DROPDIR}/."
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

    MONITOR_TOKEN=$(askpass "Monitor token (from prop.w5isp.com)")
    BEACON_ID=$(ask      "Beacon UUID"                                "00000000-0000-0000-0000-000000000000")
    GRIDSQUARE=$(ask     "Gridsquare (e.g. FN31pr)"                   "")
    ANTENNA_HEIGHT=$(ask "Antenna height (ft)"                        "")
    API_URL=$(ask        "NTMS API URL"                               "https://prop.w5isp.com/api/v1/beacon-monitor/measurements")
    PHASE_FILTER=$(ask   "Phase filter (blank=all, e.g. CARRIER)"     "CARRIER")
    FREQ=$(ask           "IF center frequency MHz"                    "618.245")
    LO=$(ask             "LNB LO frequency MHz"                       "9750.0")
    THRESHOLD=$(ask      "Detection threshold dBFS"                   "-50.0")
    GAIN=$(ask           "SDR gain (dB or 'auto')"                    "auto")
    PPM=$(ask            "PPM correction (0=TCXO, 1-2=crystal)"      "0")
    INTERVAL=$(ask       "Sweep interval seconds (300 = 5 minutes)" "300")
    DEVICE=$(ask         "Device index or serial"                     "0")

    echo
    # Write monitor drop-in — restrict permissions so the token is never
    # briefly world-readable during the write.
    install -m 640 -o root -g "${SERVICE_USER}" /dev/null "${MONITOR_DROPIN}"
    cat > "${MONITOR_DROPIN}" <<EOF
[Service]
Environment=BEACON_OUTPUT=${DATA_DIR}/beacon_log.csv
Environment=BEACON_DEVICE=${DEVICE}
Environment=BEACON_FREQ_MHZ=${FREQ}
Environment=BEACON_LO_MHZ=${LO}
Environment=BEACON_THRESHOLD_DBFS=${THRESHOLD}
Environment=BEACON_PASSBAND_KHZ=5
Environment=BEACON_GAIN=${GAIN}
Environment=BEACON_PPM=${PPM}
Environment=BEACON_INTERVAL_S=${INTERVAL}
Environment=BEACON_CW_END_S=10
EOF
    info "Monitor drop-in written to ${MONITOR_DROPIN}."

    # Write reporter drop-in
    install -m 640 -o root -g "${SERVICE_USER}" /dev/null "${REPORTER_DROPIN}"
    cat > "${REPORTER_DROPIN}" <<EOF
[Service]
Environment=NTMS_INPUT=${DATA_DIR}/beacon_log.csv
Environment=NTMS_MONITOR_TOKEN=${MONITOR_TOKEN}
Environment=NTMS_BEACON_ID=${BEACON_ID}
Environment=NTMS_GRIDSQUARE=${GRIDSQUARE}
Environment=NTMS_ANTENNA_HEIGHT_FT=${ANTENNA_HEIGHT}
Environment=NTMS_API_URL=${API_URL}
Environment=NTMS_PHASE_FILTER=${PHASE_FILTER}
EOF
    info "Reporter drop-in written to ${REPORTER_DROPIN}."
else
    info "Keeping existing drop-in config files."
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
echo "  Config     : ${MONITOR_DROPIN}"
echo "               ${REPORTER_DROPIN}"
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
echo "    Edit config     : sudo systemctl edit beacon-monitor"
echo "                      sudo systemctl edit beacon-reporter"
echo "    List SDR devices: ${VENV}/bin/python3 ${INSTALL_DIR}/beacon_monitor.py --list-devices"
echo "    Run calibration : ${VENV}/bin/python3 ${INSTALL_DIR}/beacon_calibrate.py"
echo
if [[ ! -f "$BLACKLIST" ]] || systemctl is-active beacon-monitor.service &>/dev/null; then
    warn "NOTE: The DVB kernel module blacklist takes effect after a reboot."
    warn "If beacon-monitor is not running, reboot and check again:"
    warn "  sudo reboot"
fi
echo
