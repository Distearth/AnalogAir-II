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

if [ "$1" == "--test" ]; then
    echo -e "${CYAN}Running Deadstream Hardware Diagnostics...${NC}"
    if [ ! -d "$VENV_PATH" ]; then
        echo -e "${RED}Error: VirtualEnv not found at $VENV_PATH. Please run setup first.${NC}"
        exit 1
    fi
    echo "Starting Deadstream controller in test mode (Press Ctrl+C to exit)..."
    "$VENV_PATH/bin/python3" "$CONFIG_DIR/analogair_deadstream.py"
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

# 3. Copy Deadstream scripts & default configuration
echo ""
echo -e "${YELLOW}[3/4] Installing scripts to $CONFIG_DIR...${NC}"
mkdir -p "$CONFIG_DIR"

cp -f "$SCRIPT_DIR/analogair_deadstream.py" "$CONFIG_DIR/analogair_deadstream.py"
cp -f "$SCRIPT_DIR/analogair_wifi.py" "$CONFIG_DIR/analogair_wifi.py"
chmod +x "$CONFIG_DIR/analogair_deadstream.py" "$CONFIG_DIR/analogair_wifi.py"

# Write default pinout config if it doesn't already exist
DEADSTREAM_JSON="$CONFIG_DIR/deadstream.json"
if [ ! -f "$DEADSTREAM_JSON" ]; then
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
    "dc_pin": 25,
    "rst_pin": 27,
    "bl_pin": 18,
    "brightness": 100
  },
  "knobs": {
    "volume": {
      "name": "Volume (Left)",
      "clk": 17,
      "dt": 27,
      "sw": 22
    },
    "tone": {
      "name": "Tone DSP (Center)",
      "clk": 5,
      "dt": 6,
      "sw": 13
    },
    "speakers": {
      "name": "Speakers / Wi-Fi (Right)",
      "clk": 19,
      "dt": 26,
      "sw": 4
    }
  },
  "buttons": {
    "page": 2,
    "action": 3,
    "source": 14
  }
}
JSONEOF
    echo "Created default pinout config at $DEADSTREAM_JSON."
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
echo "Controls Summary:"
echo " - Knob 1 (Left):   Turn for Master Volume | Click to Mute | Long Press to Purge Buffer"
echo " - Knob 2 (Center): Turn for Tone Gain | Click to Cycle Band [Bass/Mid/Treble/Gain] | Long Press for Flat"
echo " - Knob 3 (Right):  Turn to Scroll Speakers/Wi-Fi | Click to Connect/Disconnect | Long Press for AutoConnect"
echo " - Button 1 (Page): Cycle Screens [Now Playing -> Speakers -> Tone DSP -> Wi-Fi Setup]"
echo " - Button 2 (Act):  Force Shazam Re-scan"
echo " - Button 3 (Src):  Cycle Source [Vinyl -> Tape -> CD -> Aux]"
echo ""
echo "Wi-Fi Failover:"
echo " - If booted in an area without Wi-Fi, the ST7735 screen automatically"
echo "   displays a setup QR code and starts hotspot 'AnalogAir-Setup'."
echo " - You can also pick networks and dial passwords right on screen using the knobs!"
echo ""
echo "To test in terminal: $0 --test"
echo "To check logs:       sudo journalctl -u analogair-deadstream.service -f"
