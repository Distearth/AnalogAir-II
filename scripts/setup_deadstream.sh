#!/bin/bash
# ==============================================================================
# AnalogAir Deadstream Hardware Board Installer & Diagnostic Tool
# Configures the Grateful Dead Time Machine PCB (ST7735 TFT + 3 Knobs + Buttons)
# ==============================================================================
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

CURRENT_USER="${USER:-$(whoami)}"
USER_HOME="${HOME:-/home/$CURRENT_USER}"
CONFIG_DIR="$USER_HOME/.config/analogair"
VENV_PATH="$CONFIG_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN} AnalogAir Deadstream Hardware Controller Setup${NC}"
echo -e "${GREEN} Repurposing Grateful Dead Time Machine PCB for Vinyl Streaming${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo ""

# Copy latest scripts to config dir first
mkdir -p "$CONFIG_DIR"
cp -f "$SCRIPT_DIR/analogair_deadstream.py" "$CONFIG_DIR/analogair_deadstream.py"
cp -f "$SCRIPT_DIR/analogair_wifi.py" "$CONFIG_DIR/analogair_wifi.py"
chmod +x "$CONFIG_DIR/analogair_deadstream.py" "$CONFIG_DIR/analogair_wifi.py"

# Write official Deadstream PCB pin configuration
DEADSTREAM_JSON="$CONFIG_DIR/deadstream.json"
cat << 'JSONEOF' > "$DEADSTREAM_JSON"
{
  "enabled": true,
  "api_base": "http://127.0.0.1:3000",
  "display": {
    "width": 160,
    "height": 128,
    "rotation": 90,
    "spi_port": 0,
    "spi_cs": 0,
    "dc_pin": 24,
    "rst_pin": 25,
    "bl_pin": null,
    "invert": false,
    "brightness": 100
  },
  "knobs": {
    "volume": {
      "name": "Volume (Left / Year)",
      "clk": 16,
      "dt": 22,
      "sw": 23
    },
    "tone": {
      "name": "Tone DSP (Center / Month)",
      "clk": 12,
      "dt": 5,
      "sw": 6
    },
    "speakers": {
      "name": "Speakers / Wi-Fi (Right / Day)",
      "clk": 13,
      "dt": 17,
      "sw": 27
    }
  },
  "buttons": {
    "page": 2,
    "action": 4,
    "source": 3
  }
}
JSONEOF

if [ "$1" == "--test" ]; then
    echo -e "${CYAN}Running Deadstream Hardware Diagnostics...${NC}"
    if [ ! -d "$VENV_PATH" ]; then
        echo -e "${RED}Error: VirtualEnv not found at $VENV_PATH. Please run setup first.${NC}"
        exit 1
    fi

    # 1. Stop background service if running so GPIO pins are not locked
    WAS_RUNNING=0
    if systemctl is-active --quiet analogair-deadstream.service 2>/dev/null; then
        echo -e "${YELLOW}Stopping background analogair-deadstream service to free GPIO pins...${NC}"
        sudo systemctl stop analogair-deadstream.service
        WAS_RUNNING=1
    fi

    # 2. Release GPIO 3 if listen-for-shutdown is holding it
    if systemctl is-active --quiet listen-for-shutdown.service 2>/dev/null; then
        echo -e "${YELLOW}Stopping listen-for-shutdown.service (releases GPIO 3 for Deadstream)...${NC}"
        sudo systemctl stop listen-for-shutdown.service 2>/dev/null || true
    fi

    # Trap exit to restart background service cleanly
    cleanup() {
        echo ""
        if [ "$WAS_RUNNING" -eq 1 ]; then
            echo -e "${CYAN}Restoring background analogair-deadstream service...${NC}"
            sudo systemctl start analogair-deadstream.service 2>/dev/null || true
        fi
        echo -e "${GREEN}Diagnostic complete.${NC}"
    }
    trap cleanup EXIT INT TERM

    echo ""
    echo "=================================================================="
    echo " Starting Deadstream controller in interactive test mode"
    echo " - Turn each of the 3 knobs to verify encoder direction & counts"
    echo " - Click each knob push-button to verify switches"
    echo " - Press front tactile buttons to verify page/action/source"
    echo " - Press Ctrl+C when finished to return to normal operation"
    echo "=================================================================="
    echo ""

    "$VENV_PATH/bin/python3" "$CONFIG_DIR/analogair_deadstream.py" --test
    exit 0
fi

# 1. Enable SPI interface
echo -e "${YELLOW}[1/4] Enabling Raspberry Pi SPI Interface...${NC}"
if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_spi 0 || true
    echo "SPI interface enabled via raspi-config."
else
    echo "raspi-config not detected, checking /boot/firmware/config.txt..."
    BOOT_CFG="/boot/firmware/config.txt"
    [ ! -f "$BOOT_CFG" ] && BOOT_CFG="/boot/config.txt"
    if [ -f "$BOOT_CFG" ]; then
        if ! grep -q "^dtparam=spi=on" "$BOOT_CFG"; then
            echo "dtparam=spi=on" | sudo tee -a "$BOOT_CFG" >/dev/null
            echo "Added dtparam=spi=on to $BOOT_CFG."
        fi
    fi
fi

# Ensure user is in spi, gpio, audio groups
sudo usermod -a -G spi,gpio,dialout,audio "$CURRENT_USER" 2>/dev/null || true

# 2. Install required Python drivers into AnalogAir venv
echo ""
echo -e "${YELLOW}[2/4] Installing Python hardware dependencies into virtualenv...${NC}"
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating virtual environment at $VENV_PATH..."
    python3 -m venv "$VENV_PATH" --system-site-packages
fi

"$VENV_PATH/bin/pip" install --upgrade pip
"$VENV_PATH/bin/pip" install \
    st7735 \
    gpiozero \
    gpiod \
    spidev \
    qrcode \
    requests \
    pillow

# 3. Disable any conflicting services on pins (e.g. listen-for-shutdown on GPIO 3)
echo ""
echo -e "${YELLOW}[3/4] Releasing GPIO pin conflicts...${NC}"
if systemctl is-enabled --quiet listen-for-shutdown.service 2>/dev/null; then
    echo "Disabling listen-for-shutdown.service (releases GPIO 3 for Deadstream)..."
    sudo systemctl stop listen-for-shutdown.service 2>/dev/null || true
    sudo systemctl disable listen-for-shutdown.service 2>/dev/null || true
fi

# 4. Install & Enable systemd service
echo ""
echo -e "${YELLOW}[4/4] Setting up analogair-deadstream.service...${NC}"
SERVICE_FILE="/etc/systemd/system/analogair-deadstream.service"
cat << SERVEOF | sudo tee "$SERVICE_FILE" >/dev/null
[Unit]
Description=AnalogAir Deadstream Hardware Display & Rotary Controller
Documentation=https://github.com/eichblatt/deadstream
After=network.target sound.target analogair-web.service
Wants=analogair-web.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$CONFIG_DIR
ExecStart=$VENV_PATH/bin/python3 $CONFIG_DIR/analogair_deadstream.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVEOF

sudo systemctl daemon-reload
sudo systemctl enable --now analogair-deadstream.service

echo ""
echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN} Deadstream Hardware Controller Successfully Enabled!${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo ""
echo "Controls Summary (Deadstream PCB):"
echo " - Knob 1 (Left / Year):   Turn for Master Volume | Click to Mute | Long Press to Purge Buffer"
echo " - Knob 2 (Center / Month):Turn for Tone Gain | Click to Cycle Band [Bass/Mid/Treb/Gain] | Long Press for Flat"
echo " - Knob 3 (Right / Day):   Turn to Scroll Speakers/Wi-Fi | Click to Connect/Disconnect | Long Press for AutoConnect"
echo " - Button 1 (Page):        Cycle Screens [Now Playing -> Speakers -> Tone DSP -> Wi-Fi Setup]"
echo " - Button 2 (Action):      Force Shazam Re-scan"
echo " - Button 3 (Source):      Cycle Source [Vinyl -> Tape -> CD -> Aux]"
echo ""
echo "Wi-Fi Failover:"
echo " - If booted in an area without Wi-Fi, the ST7735 screen automatically"
echo "   displays a setup QR code and starts hotspot 'AnalogAir-Setup'."
echo " - You can also pick networks and dial passwords right on screen using the knobs!"
echo ""
echo "To test knobs & screen live: bash $0 --test"
echo "To check background logs:    sudo journalctl -u analogair-deadstream.service -f"
