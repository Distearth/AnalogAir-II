#!/usr/bin/env python3
"""
AnalogAir Wi-Fi & Hotspot Manager
Handles Wi-Fi status detection, SSID scanning, network connection,
hotspot failover, and QR code generation for the ST7735 display and Web UI.
"""
import os
import sys
import subprocess
import shutil
import re
import json
import time
from typing import Dict, List, Any, Optional

try:
    import qrcode
    from PIL import Image
except ImportError:
    qrcode = None
    Image = None

DEFAULT_HOTSPOT_SSID = "AnalogAir-Setup"
DEFAULT_HOTSPOT_PSK = "analogair"
SETUP_PORTAL_IP = "192.168.4.1"

def run_cmd(cmd: List[str], timeout: int = 15) -> str:
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return res.stdout.strip()
    except Exception as e:
        return ""

def get_wifi_interface() -> str:
    """Finds active wireless interface (typically wlan0)."""
    out = run_cmd(["iw", "dev"])
    m = re.search(r"Interface\s+([a-zA-Z0-9_-]+)", out)
    if m:
        return m.group(1)
    # Fallback to ip link
    out = run_cmd(["ip", "-o", "link"])
    for line in out.splitlines():
        if "wlan" in line or "wl" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                return parts[1].strip()
    return "wlan0"

def get_current_ip() -> str:
    """Gets primary local IPv4 address."""
    out = run_cmd(["hostname", "-I"])
    if out:
        ips = out.split()
        for ip in ips:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    return "127.0.0.1"

def is_online() -> bool:
    """Quick check if internet gateway or DNS is reachable."""
    # Test DNS or default gateway ping with 1s timeout
    res = subprocess.run(["ping", "-c", "1", "-W", "1", "1.1.1.1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0

def get_wifi_status() -> Dict[str, Any]:
    """Returns connected SSID, IP address, signal strength, and hotspot state."""
    iface = get_wifi_interface()
    ip = get_current_ip()
    status = {
        "connected": False,
        "interface": iface,
        "ip": ip,
        "ssid": "",
        "signal": 0,
        "isHotspot": False,
        "online": False
    }

    # 1. Check NetworkManager nmcli if available
    if shutil.which("nmcli"):
        out = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"])
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[1] == "wifi":
                state = parts[2]
                conn = parts[3]
                if state == "connected":
                    status["connected"] = True
                    status["ssid"] = conn
                    if "hotspot" in conn.lower() or conn == DEFAULT_HOTSPOT_SSID:
                        status["isHotspot"] = True

        if status["connected"]:
            # Get signal strength
            sig_out = run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi"])
            for line in sig_out.splitlines():
                if line.startswith("*"):
                    sub = line.split(":")
                    if len(sub) >= 3:
                        try:
                            status["signal"] = int(sub[2])
                        except ValueError:
                            pass

    # 2. Fallback to iwconfig / wpa_cli if not detected via nmcli
    if not status["connected"] and shutil.which("iwgetid"):
        ssid = run_cmd(["iwgetid", "-r"])
        if ssid:
            status["connected"] = True
            status["ssid"] = ssid

    # Check internet
    if status["connected"] and ip != "127.0.0.1":
        status["online"] = is_online()

    return status

def scan_wifi_networks() -> List[Dict[str, Any]]:
    """Scans and returns available Wi-Fi networks sorted by signal strength."""
    networks = []
    seen = set()

    # 1. Try nmcli dev wifi list --rescan yes
    if shutil.which("nmcli"):
        out = run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "auto"])
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 4:
                in_use = parts[0] == "*"
                ssid = parts[1].strip()
                if not ssid or ssid.startswith("\\"):
                    continue
                try:
                    signal = int(parts[2])
                except ValueError:
                    signal = 50
                security = parts[3].strip() or "Open"
                if ssid not in seen:
                    seen.add(ssid)
                    networks.append({
                        "ssid": ssid,
                        "signal": signal,
                        "security": security,
                        "inUse": in_use
                    })

    # 2. Fallback to iwlist wlan0 scan
    if not networks and shutil.which("iwlist"):
        iface = get_wifi_interface()
        out = run_cmd(["sudo", "iwlist", iface, "scan"])
        current_ssid = None
        current_sig = 50
        for line in out.splitlines():
            line = line.strip()
            if "ESSID:" in line:
                m = re.search(r'ESSID:"([^"]+)"', line)
                if m:
                    current_ssid = m.group(1)
                    if current_ssid and current_ssid not in seen:
                        seen.add(current_ssid)
                        networks.append({
                            "ssid": current_ssid,
                            "signal": current_sig,
                            "security": "WPA2",
                            "inUse": False
                        })
            elif "Quality=" in line:
                m = re.search(r'Quality=(\d+)/(\d+)', line)
                if m:
                    current_sig = int((int(m.group(1)) / int(m.group(2))) * 100)

    # Sort strongest first
    networks.sort(key=lambda n: n.get("signal", 0), reverse=True)
    return networks

def connect_wifi(ssid: str, password: str = "") -> Dict[str, Any]:
    """Connects to a specified Wi-Fi network."""
    if not ssid:
        return {"success": False, "error": "SSID cannot be empty"}

    # 1. Using NetworkManager nmcli
    if shutil.which("nmcli"):
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd.extend(["password", password])
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
        if res.returncode == 0:
            time.sleep(2)
            return {"success": True, "status": get_wifi_status()}
        else:
            return {"success": False, "error": res.stderr.strip() or res.stdout.strip()}

    # 2. Using wpa_passphrase / wpa_cli fallback
    if shutil.which("wpa_cli"):
        try:
            run_cmd(["sudo", "wpa_cli", "-i", "wlan0", "reconfigure"])
            return {"success": True, "status": get_wifi_status()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": "No Wi-Fi management tool found (nmcli or wpa_cli)"}

def start_setup_hotspot() -> Dict[str, Any]:
    """Starts a fallback access point for captive Wi-Fi configuration."""
    iface = get_wifi_interface()
    if shutil.which("nmcli"):
        # Stop existing hotspot connection if any
        run_cmd(["nmcli", "con", "down", "AnalogAir-Hotspot"])
        run_cmd(["nmcli", "con", "delete", "AnalogAir-Hotspot"])
        # Create new AP hotspot
        cmd = [
            "nmcli", "dev", "wifi", "hotspot",
            "ifname", iface,
            "con-name", "AnalogAir-Hotspot",
            "ssid", DEFAULT_HOTSPOT_SSID,
            "password", DEFAULT_HOTSPOT_PSK
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        if res.returncode == 0:
            return {
                "success": True,
                "isHotspot": True,
                "ssid": DEFAULT_HOTSPOT_SSID,
                "password": DEFAULT_HOTSPOT_PSK,
                "portalUrl": f"http://{SETUP_PORTAL_IP}:3000"
            }
        else:
            return {"success": False, "error": res.stderr.strip() or res.stdout.strip()}

    return {"success": False, "error": "nmcli is required for hotspot failover"}

def generate_wifi_qr_image(ssid: str = DEFAULT_HOTSPOT_SSID,
                           password: str = DEFAULT_HOTSPOT_PSK,
                           security: str = "WPA",
                           size: int = 110) -> Optional[Any]:
    """
    Generates a 1-bit or RGB PIL Image containing the Wi-Fi QR code
    sized appropriately for the 128x160 ST7735 screen.
    Format: WIFI:S:<ssid>;T:<WPA|WEP|nopass>;P:<password>;;
    """
    if not qrcode or not Image:
        return None

    auth_type = "nopass" if not password else security
    qr_payload = f"WIFI:S:{ssid};T:{auth_type};P:{password};;"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=2,
        border=1,
    )
    qr.add_data(qr_payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    # Resize cleanly to target display box
    return img.resize((size, size), Image.Resampling.NEAREST)

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "status":
        print(json.dumps(get_wifi_status(), indent=2))
    elif action == "scan":
        print(json.dumps(scan_wifi_networks(), indent=2))
    elif action == "hotspot":
        print(json.dumps(start_setup_hotspot(), indent=2))
    elif action == "connect" and len(sys.argv) >= 3:
        ssid = sys.argv[2]
        pwd = sys.argv[3] if len(sys.argv) > 3 else ""
        print(json.dumps(connect_wifi(ssid, pwd), indent=2))
    else:
        print("Usage: analogair_wifi.py [status|scan|hotspot|connect <ssid> [password]]")
