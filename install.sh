#!/usr/bin/env bash
# install.sh — GRBL Proxy installer
# Run from the repository root after cloning:
#   bash install.sh
set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput &>/dev/null && tput colors &>/dev/null; then
    _GREEN="\033[0;32m"
    _YELLOW="\033[0;33m"
    _RED="\033[0;31m"
    _BOLD="\033[1m"
    _RESET="\033[0m"
else
    _GREEN="" _YELLOW="" _RED="" _BOLD="" _RESET=""
fi

ok()   { echo -e "${_GREEN}[OK]${_RESET}    $*"; }
skip() { echo -e "${_YELLOW}[SKIP]${_RESET}  $*"; }
info() { echo -e "        $*"; }
err()  { echo -e "${_RED}[ERROR]${_RESET} $*" >&2; }
step() { echo -e "\n${_BOLD}==> $*${_RESET}"; }

die() { err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Move to repo root (wherever install.sh lives)
# ---------------------------------------------------------------------------
cd "$(dirname "$(readlink -f "$0")")"
REPO_DIR="$(pwd)"

# ---------------------------------------------------------------------------
# Step 0 — Preflight
# ---------------------------------------------------------------------------
step "Preflight checks"

[[ "$(uname -s)" == "Linux" ]] || die "This installer only supports Linux."

command -v sudo &>/dev/null || die "sudo is required but not found."

# Cache sudo credentials once so later calls don't prompt mid-script
sudo -v || die "Unable to obtain sudo privileges."
# Keep sudo alive in the background for the duration of the script
( while true; do sudo -n true; sleep 50; done ) &
_SUDO_KEEPALIVE_PID=$!
trap 'kill "$_SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT

ok "Running as $USER in $REPO_DIR"

# Python 3.11+ check — try to install if missing
_check_python() {
    local py
    for py in python3.13 python3.12 python3.11 python3; do
        if command -v "$py" &>/dev/null; then
            local ver
            ver="$("$py" -c 'import sys; print(sys.version_info[:2])')"
            if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
                PYTHON="$py"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=""
if ! _check_python; then
    info "Python 3.11+ not found, attempting to install via apt..."
    sudo apt-get install -y python3 python3-venv python3-dev -q
    _check_python || die "Python 3.11+ is required but could not be installed. Please install it manually."
fi
ok "Python: $PYTHON ($("$PYTHON" --version))"

# ---------------------------------------------------------------------------
# Step 1 — System packages
# ---------------------------------------------------------------------------
step "System packages"

PKGS_NEEDED=()
for pkg in python3-venv python3-dev libcap2-bin; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        PKGS_NEEDED+=("$pkg")
    fi
done

if [ ${#PKGS_NEEDED[@]} -eq 0 ]; then
    skip "All required system packages already installed"
else
    info "Installing: ${PKGS_NEEDED[*]}"
    sudo apt-get update -q
    sudo apt-get install -y "${PKGS_NEEDED[@]}" -q
    ok "System packages installed"
fi

# ---------------------------------------------------------------------------
# Step 2 — dialout group
# ---------------------------------------------------------------------------
step "Serial port permissions (dialout group)"

if groups "$USER" | grep -qw dialout; then
    skip "$USER is already in the dialout group"
    _DIALOUT_ADDED=false
else
    sudo usermod -aG dialout "$USER"
    ok "Added $USER to the dialout group"
    _DIALOUT_ADDED=true
fi

# ---------------------------------------------------------------------------
# Step 3 — Python virtual environment
# ---------------------------------------------------------------------------
step "Python virtual environment"

if [ -f ".venv/bin/activate" ]; then
    skip "Virtual environment already exists at .venv/"
else
    "$PYTHON" -m venv .venv
    ok "Created .venv/"
fi

info "Installing Python dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -e . -q
ok "grbl-proxy installed into .venv/"

# ---------------------------------------------------------------------------
# Step 4 — Config directory and file
# ---------------------------------------------------------------------------
step "Configuration"

mkdir -p "$HOME/.grbl-proxy/jobs"
ok "Config directory: $HOME/.grbl-proxy/"

CONFIG_FILE="$HOME/.grbl-proxy/config.yaml"
if [ -f "$CONFIG_FILE" ]; then
    skip "Config already exists at $CONFIG_FILE (not overwriting)"
else
    cp "$REPO_DIR/config.yaml.example" "$CONFIG_FILE"
    ok "Config created at $CONFIG_FILE"
    info "Review and edit this file before starting the service."
fi

# ---------------------------------------------------------------------------
# Step 5 — setcap for port 23
# ---------------------------------------------------------------------------
step "Port 23 capability (setcap)"

VENV_PYTHON="$(readlink -f .venv/bin/python3)"
sudo setcap 'cap_net_bind_service=+ep' "$VENV_PYTHON"

# Verify
if getcap "$VENV_PYTHON" | grep -q cap_net_bind_service; then
    ok "cap_net_bind_service granted to $VENV_PYTHON"
else
    err "setcap appeared to succeed but getcap did not confirm it."
    err "Port 23 may not work. You can retry manually:"
    err "  sudo setcap 'cap_net_bind_service=+ep' $VENV_PYTHON"
fi

# ---------------------------------------------------------------------------
# Step 6 — systemd service
# ---------------------------------------------------------------------------
step "systemd service"

SERVICE_SRC="$REPO_DIR/systemd/grbl-proxy.service"
SERVICE_DST="/etc/systemd/system/grbl-proxy.service"
PATCHED=$(mktemp /tmp/grbl-proxy-XXXXXX.service)

# Substitute actual user, home, and repo path into the service file
sed \
    -e "s|User=pi|User=$USER|g" \
    -e "s|Group=dialout|Group=dialout|g" \
    -e "s|/home/pi/grbl-proxy|$REPO_DIR|g" \
    -e "s|/home/pi/\.grbl-proxy|$HOME/.grbl-proxy|g" \
    "$SERVICE_SRC" > "$PATCHED"

sudo cp "$PATCHED" "$SERVICE_DST"
rm -f "$PATCHED"

sudo systemctl daemon-reload
sudo systemctl enable grbl-proxy
ok "Service installed and enabled: grbl-proxy.service"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${_RESET}"
echo -e "${_GREEN}${_BOLD}  Installation complete!${_RESET}"
echo -e "${_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${_RESET}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Review your config:"
echo "       nano $HOME/.grbl-proxy/config.yaml"
echo ""

if [ "$_DIALOUT_ADDED" = true ]; then
echo -e "  2. ${_YELLOW}Log out and back in${_RESET} so your dialout group membership takes effect."
echo "     (The service itself is already configured correctly.)"
echo ""
echo "  3. Start the service:"
else
echo "  2. Start the service:"
fi

echo "       sudo systemctl start grbl-proxy"
echo ""
echo "  Check status:"
echo "       sudo systemctl status grbl-proxy"
echo ""
echo "  Live logs:"
echo "       journalctl -u grbl-proxy -f"
echo ""
