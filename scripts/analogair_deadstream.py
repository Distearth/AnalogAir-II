#!/usr/bin/env python3
"""
AnalogAir Deadstream Hardware Controller
Integrates the Grateful Dead Time Machine PCB (ST7735 128x160 TFT,
3 rotary encoders with push switches, and tactile buttons) with AnalogAir.

Architecture:
- Left Knob  (Knob 1): Master Volume & Mute / Buffer Purge
- Center Knob(Knob 2): Tone DSP (Bass / Mid / Treble / Input Gain)
- Right Knob (Knob 3): AirPlay Speaker Selection & Wi-Fi Navigation
- Buttons: Page Navigation, Shazam Rescan, Source Switch
- Auto-Fallback Wi-Fi QR Code & Network Setup
"""
import os
import sys
import time
import json
import threading
import math
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests
from PIL import Image, ImageDraw, ImageFont

# Try importing hardware libraries
try:
    from gpiozero import RotaryEncoder, Button
    HAS_GPIOZERO = True
except ImportError:
    HAS_GPIOZERO = False

HAS_ST7735 = False
ST7735Class = None
try:
    import st7735
    ST7735Class = getattr(st7735, 'ST7735', st7735)
    HAS_ST7735 = True
except (ImportError, AttributeError):
    try:
        import ST7735
        ST7735Class = getattr(ST7735, 'ST7735', ST7735)
        HAS_ST7735 = True
    except ImportError:
        HAS_ST7735 = False

# Local Wi-Fi helper
try:
    import analogair_wifi
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    try:
        import analogair_wifi
    except ImportError:
        analogair_wifi = None

CONFIG_PATHS = [
    Path("/etc/analogair/deadstream.json"),
    Path.home() / ".config" / "analogair" / "deadstream.json",
    Path(__file__).resolve().parent.parent / "deadstream.json"
]

# Official Grateful Dead Time Machine PCB Pinout:
# - Display: SPI0 (MOSI 10, SCLK 11, CE0 8), DC: GPIO 24, Reset: GPIO 25, BL: 3.3V
# - Knob 1 (Left / Year):  CLK: GPIO 16, DT: GPIO 22, SW: GPIO 23
# - Knob 2 (Center / Month): CLK: GPIO 12, DT: GPIO 5,  SW: GPIO 6
# - Knob 3 (Right / Day):   CLK: GPIO 13, DT: GPIO 17, SW: GPIO 27
# - Front Buttons: Page: GPIO 2 (Stop), Action: GPIO 4 (Select), Source: GPIO 3 (Rewind)
DEFAULT_CONFIG = {
    "enabled": True,
    "api_base": "http://127.0.0.1:3000",
    "display": {
        "width": 160,
        "height": 128,
        "rotation": 90,       # 160x128 landscape
        "spi_port": 0,
        "spi_cs": 0,
        "dc_pin": 24,
        "rst_pin": 25,
        "bl_pin": None,       # 3.3V on Deadstream PCB (no GPIO pin needed)
        "invert": False,
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
        "page": 2,            # Tactile Button 1 (SDA / Stop)
        "action": 4,          # Tactile Button 2 (Select)
        "source": 3           # Tactile Button 3 (SCL / Rewind)
    }
}

class DeadstreamController:
    def __init__(self):
        self.config = self.load_config()
        self.api_base = self.config.get("api_base", "http://127.0.0.1:3000")

        self.width = self.config["display"].get("width", 160)
        self.height = self.config["display"].get("height", 128)
        self.rotation = self.config["display"].get("rotation", 90)

        # State
        self.running = True
        self.current_page = 0  # 0: Now Playing, 1: Speakers, 2: Tone DSP, 3: Wi-Fi Setup
        self.pages = ["now_playing", "speakers", "tone", "wifi"]

        # Cached API State
        self.now_playing: Dict[str, Any] = {
            "status": "idle",
            "artist": "Audio-Technica",
            "album": "Turntable Standby",
            "title": "AnalogAir Vinyl",
            "sourceType": "vinyl"
        }
        self.tone: Dict[str, Any] = {
            "inputGainDb": 0.0,
            "bassGainDb": 1.5,
            "midGainDb": 0.0,
            "trebleGainDb": 0.5
        }
        self.outputs: List[Dict[str, Any]] = []
        self.wifi_status: Dict[str, Any] = {
            "connected": False,
            "ssid": "",
            "ip": "127.0.0.1",
            "isHotspot": False
        }
        self.wifi_scan_results: List[Dict[str, Any]] = []

        # UI interaction state
        self.master_volume = 75
        self.is_muted = False
        self.active_tone_param = 0  # 0: Bass, 1: Mid, 2: Treble, 3: Input Gain
        self.tone_params = ["bassGainDb", "midGainDb", "trebleGainDb", "inputGainDb"]
        self.tone_labels = ["Bass", "Mid", "Treble", "Phono Gain"]

        self.selected_speaker_idx = 0
        self.selected_wifi_idx = 0

        # HUD Overlay State (for temporary volume / tone popup)
        self.hud_title = ""
        self.hud_value = ""
        self.hud_bar_pct = 0
        self.hud_timeout = 0

        # Wi-Fi manual password dialer state
        self.wifi_entering_pass = False
        self.wifi_entered_pass = ""
        self.wifi_charset = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_+-= "
        self.wifi_char_idx = 0

        # Display device
        self.disp = None
        self.canvas = Image.new("RGB", (self.width, self.height), color=(10, 10, 14))
        self.draw = ImageDraw.Draw(self.canvas)

        # Test mode flag
        self.is_test_mode = "--test" in sys.argv

        # Fonts
        self.font_title = ImageFont.load_default()
        self.font_body = ImageFont.load_default()
        self.font_small = ImageFont.load_default()

        self.init_display()
        self.init_gpio()

class DirectST7735:
    """
    Pure Python ST7735 SPI display driver using standard spidev and gpiozero.
    Zero external C library dependencies; works reliably across all Pi models.
    """
    def __init__(self, port=0, cs=0, dc=24, rst=25, width=160, height=128, rotation=90):
        self.width = width
        self.height = height
        self.rotation = rotation
        import spidev
        from gpiozero import OutputDevice
        self.dc = OutputDevice(dc)
        self.rst = OutputDevice(rst) if rst is not None else None
        self.spi = spidev.SpiDev()
        self.spi.open(port, cs)
        self.spi.max_speed_hz = 16000000
        self.spi.mode = 0

    def command(self, cmd):
        self.dc.off()
        self.spi.writebytes([cmd])

    def data(self, val):
        self.dc.on()
        if isinstance(val, (list, tuple)):
            self.spi.writebytes(list(val))
        elif isinstance(val, (bytes, bytearray)):
            self.spi.writebytes2(val)
        else:
            self.spi.writebytes([val])

    def begin(self):
        if self.rst:
            self.rst.on()
            time.sleep(0.01)
            self.rst.off()
            time.sleep(0.01)
            self.rst.on()
            time.sleep(0.12)

        self.command(0x01)  # SWRESET
        time.sleep(0.12)
        self.command(0x11)  # SLPOUT
        time.sleep(0.12)

        # FRMCTR1: Frame rate control
        self.command(0xB1)
        self.data([0x01, 0x2C, 0x2D])

        # INVCTR: Display inversion control
        self.command(0xB4)
        self.data([0x07])

        # PWCTR1: Power control
        self.command(0xC0)
        self.data([0xA2, 0x02, 0x84])
        self.command(0xC1)
        self.data([0xC5])
        self.command(0xC2)
        self.data([0x0A, 0x00])

        # VMCTR1: VCOM control
        self.command(0xC5)
        self.data([0x8A, 0x27])

        # MADCTL: Memory Access Control (Orientation)
        self.command(0x36)
        if self.rotation == 90:
            self.data([0xA8])  # Landscape 160x128 BGR
        elif self.rotation == 270:
            self.data([0x68])
        elif self.rotation == 180:
            self.data([0xC8])
        else:
            self.data([0x08])

        # COLMOD: 16-bit RGB565
        self.command(0x3A)
        self.data([0x05])

        # DISPON: Display on
        self.command(0x29)
        time.sleep(0.05)

    def display(self, image):
        img = image.convert("RGB")
        w, h = img.size

        # CASET: Column Address Set
        self.command(0x2A)
        self.data([0x00, 0x00, 0x00, (w - 1) & 0xFF])

        # RASET: Row Address Set
        self.command(0x2B)
        self.data([0x00, 0x00, 0x00, (h - 1) & 0xFF])

        # RAMWR: Memory Write
        self.command(0x2C)
        self.dc.on()

        # Convert RGB888 to RGB565 byte buffer
        raw = img.tobytes()
        buf = bytearray(w * h * 2)
        j = 0
        for i in range(0, len(raw), 3):
            r = raw[i]
            g = raw[i+1]
            b = raw[i+2]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[j] = (rgb565 >> 8) & 0xFF
            buf[j+1] = rgb565 & 0xFF
            j += 2

        # Send in 4096-byte SPI chunks
        chunk_size = 4096
        for k in range(0, len(buf), chunk_size):
            self.spi.writebytes2(buf[k:k+chunk_size])

    def load_config(self) -> Dict[str, Any]:
        for p in CONFIG_PATHS:
            if p.exists():
                try:
                    with open(p, "r") as f:
                        cfg = json.load(f)
                        merged = DEFAULT_CONFIG.copy()
                        merged.update(cfg)
                        return merged
                except Exception as e:
                    print(f"[Deadstream] Error loading {p}: {e}")
        return DEFAULT_CONFIG

    def init_display(self):
        """Initializes ST7735 TFT via SPI with multi-tier driver fallback."""
        d_cfg = self.config.get("display", {})
        dc_pin = d_cfg.get("dc_pin", 24)
        rst_pin = d_cfg.get("rst_pin", 25)
        bl_pin = d_cfg.get("bl_pin", None)
        port = d_cfg.get("spi_port", 0)
        cs = d_cfg.get("spi_cs", 0)

        # Tier 1: Try luma.lcd (Industry standard on Raspberry Pi)
        try:
            from luma.core.interface.serial import spi as luma_spi
            from luma.lcd.device import st7735 as luma_st7735
            rotate_val = 1 if self.rotation == 90 else (2 if self.rotation == 180 else (3 if self.rotation == 270 else 0))
            serial = luma_spi(port=port, device=cs, gpio_DC=dc_pin, gpio_RST=rst_pin)
            self.disp = luma_st7735(serial, width=self.width, height=self.height, rotate=rotate_val)
            print(f"[Deadstream] ST7735 display initialized via luma.lcd (DC={dc_pin}, RST={rst_pin}).")
            return
        except Exception as e_luma:
            print(f"[Deadstream] Note (luma.lcd): {e_luma}")

        # Tier 2: Try Pimoroni st7735 library
        if HAS_ST7735 and ST7735Class is not None:
            try:
                kwargs = {
                    "port": port,
                    "cs": cs,
                    "dc": dc_pin,
                    "rotation": self.rotation,
                    "width": self.width,
                    "height": self.height,
                    "invert": d_cfg.get("invert", False)
                }
                if rst_pin is not None:
                    kwargs["rst"] = rst_pin
                if bl_pin is not None:
                    kwargs["backlight"] = bl_pin

                self.disp = ST7735Class(**kwargs)
                self.disp.begin()
                print(f"[Deadstream] ST7735 display initialized via st7735 driver (DC={dc_pin}, RST={rst_pin}).")
                return
            except Exception as e_st:
                print(f"[Deadstream] Note (st7735): {e_st}")

        # Tier 3: Direct Native ST7735 driver using spidev and gpiozero
        try:
            self.disp = DirectST7735(
                port=port,
                cs=cs,
                dc=dc_pin,
                rst=rst_pin,
                width=self.width,
                height=self.height,
                rotation=self.rotation
            )
            self.disp.begin()
            print(f"[Deadstream] ST7735 display initialized via Direct SPI driver (DC={dc_pin}, RST={rst_pin}).")
            return
        except Exception as e_direct:
            print(f"[Deadstream] Direct SPI display driver note: {e_direct}")

        print("[Deadstream] Display driver unavailable. Running in framebuffer/headless mode.")
        self.disp = None

    def init_gpio(self):
        """Initializes 3 rotary encoders and 3 tactile buttons."""
        if not HAS_GPIOZERO:
            print("[Deadstream] gpiozero not available. Hardware buttons/knobs disabled.")
            return

        k_cfg = self.config.get("knobs", {})
        b_cfg = self.config.get("buttons", {})

        # 1. Left Knob: Volume
        try:
            v_cfg = k_cfg.get("volume", {})
            self.enc_volume = RotaryEncoder(v_cfg["clk"], v_cfg["dt"], bounce_time=0.01)
            self.enc_volume.when_rotated_clockwise = self.on_volume_up
            self.enc_volume.when_rotated_counter_clockwise = self.on_volume_down

            self.btn_volume = Button(v_cfg["sw"], pull_up=True, bounce_time=0.05, hold_time=1.5)
            self.btn_volume.when_pressed = self.on_volume_click
            self.btn_volume.when_held = self.on_volume_long_press
            print(f"[Deadstream] Knob 1 (Volume) active: CLK={v_cfg['clk']}, DT={v_cfg['dt']}, SW={v_cfg['sw']}")
        except Exception as e:
            print(f"[Deadstream] Error initializing Volume knob: {e}")

        # 2. Center Knob: Tone DSP
        try:
            t_cfg = k_cfg.get("tone", {})
            self.enc_tone = RotaryEncoder(t_cfg["clk"], t_cfg["dt"], bounce_time=0.01)
            self.enc_tone.when_rotated_clockwise = self.on_tone_up
            self.enc_tone.when_rotated_counter_clockwise = self.on_tone_down

            self.btn_tone = Button(t_cfg["sw"], pull_up=True, bounce_time=0.05, hold_time=1.5)
            self.btn_tone.when_pressed = self.on_tone_click
            self.btn_tone.when_held = self.on_tone_long_press
            print(f"[Deadstream] Knob 2 (Tone DSP) active: CLK={t_cfg['clk']}, DT={t_cfg['dt']}, SW={t_cfg['sw']}")
        except Exception as e:
            print(f"[Deadstream] Error initializing Tone knob: {e}")

        # 3. Right Knob: Speakers & Navigation
        try:
            s_cfg = k_cfg.get("speakers", {})
            self.enc_speakers = RotaryEncoder(s_cfg["clk"], s_cfg["dt"], bounce_time=0.01)
            self.enc_speakers.when_rotated_clockwise = self.on_speakers_up
            self.enc_speakers.when_rotated_counter_clockwise = self.on_speakers_down

            self.btn_speakers = Button(s_cfg["sw"], pull_up=True, bounce_time=0.05, hold_time=1.5)
            self.btn_speakers.when_pressed = self.on_speakers_click
            self.btn_speakers.when_held = self.on_speakers_long_press
            print(f"[Deadstream] Knob 3 (Speakers/Wi-Fi) active: CLK={s_cfg['clk']}, DT={s_cfg['dt']}, SW={s_cfg['sw']}")
        except Exception as e:
            print(f"[Deadstream] Error initializing Speakers knob: {e}")

        # 4. Front Tactile Buttons
        try:
            p_pin = b_cfg.get("page", 2)
            self.btn_page = Button(p_pin, pull_up=True, bounce_time=0.08)
            self.btn_page.when_pressed = self.cycle_page

            a_pin = b_cfg.get("action", 4)
            self.btn_action = Button(a_pin, pull_up=True, bounce_time=0.08)
            self.btn_action.when_pressed = self.on_action_button

            s_pin = b_cfg.get("source", 3)
            self.btn_source = Button(s_pin, pull_up=True, bounce_time=0.08)
            self.btn_source.when_pressed = self.on_source_button
            print(f"[Deadstream] Tactile buttons active: Page={p_pin}, Action={a_pin}, Source={s_pin}")
        except Exception as e:
            print(f"[Deadstream] Error initializing Tactile buttons: {e}")

    # =========================================================================
    # Knob 1 (Left): Master Volume & Mute / Buffer Resync
    # =========================================================================
    def on_volume_up(self):
        if self.is_test_mode:
            print(f"[Test] Knob 1 (Volume) turned UP -> {min(100, self.master_volume + 2)}%")
        if self.wifi_entering_pass:
            # Knob 1 dials characters forward
            self.wifi_char_idx = (self.wifi_char_idx + 1) % len(self.wifi_charset)
            return

        self.master_volume = min(100, self.master_volume + 2)
        self.apply_master_volume()
        self.trigger_hud("VOLUME", f"{self.master_volume}%", self.master_volume)

    def on_volume_down(self):
        if self.is_test_mode:
            print(f"[Test] Knob 1 (Volume) turned DOWN -> {max(0, self.master_volume - 2)}%")
        if self.wifi_entering_pass:
            # Knob 1 dials characters backward
            self.wifi_char_idx = (self.wifi_char_idx - 1) % len(self.wifi_charset)
            return

        self.master_volume = max(0, self.master_volume - 2)
        self.apply_master_volume()
        self.trigger_hud("VOLUME", f"{self.master_volume}%", self.master_volume)

    def on_volume_click(self):
        if self.is_test_mode:
            print(f"[Test] Knob 1 (Volume) CLICKED -> Mute toggle (now {not self.is_muted})")
        if self.wifi_entering_pass:
            # Append selected character
            ch = self.wifi_charset[self.wifi_char_idx]
            self.wifi_entered_pass += ch
            return

        self.is_muted = not self.is_muted
        vol = 0 if self.is_muted else self.master_volume
        self.apply_master_volume(override_vol=vol)
        status_text = "MUTED" if self.is_muted else f"{self.master_volume}%"
        self.trigger_hud("VOLUME", status_text, 0 if self.is_muted else self.master_volume)

    def on_volume_long_press(self):
        """Long press Knob 1: Purges 30s pipe buffer and restores live needle sync."""
        self.trigger_hud("RESYNC", "Purging Buffer...", 100)
        try:
            requests.post(f"{self.api_base}/api/purge-buffer", timeout=2)
        except Exception:
            pass

    def apply_master_volume(self, override_vol: Optional[int] = None):
        target_vol = self.master_volume if override_vol is None else override_vol
        # Update active outputs
        for o in self.outputs:
            if o.get("selected"):
                try:
                    requests.put(f"{self.api_base}/api/owntone/outputs/{o['id']}/volume",
                                 json={"volume": target_vol}, timeout=1)
                except Exception:
                    pass

    # =========================================================================
    # Knob 2 (Center): Tone DSP & Phono Gain
    # =========================================================================
    def on_tone_up(self):
        param = self.tone_params[self.active_tone_param]
        curr = float(self.tone.get(param, 0.0))
        new_val = min(12.0, round(curr + 0.5, 1))
        label = self.tone_labels[self.active_tone_param]
        if self.is_test_mode:
            print(f"[Test] Knob 2 (Tone) turned UP: {label} -> {new_val:+.1f} dB")
        if self.wifi_entering_pass:
            # Knob 2 backspace
            if len(self.wifi_entered_pass) > 0:
                self.wifi_entered_pass = self.wifi_entered_pass[:-1]
            return

        self.tone[param] = new_val
        self.save_tone_dsp()
        pct = int(((new_val + 12.0) / 24.0) * 100)
        self.trigger_hud(label.upper(), f"{new_val:+.1f} dB", pct)

    def on_tone_down(self):
        param = self.tone_params[self.active_tone_param]
        curr = float(self.tone.get(param, 0.0))
        new_val = max(-12.0, round(curr - 0.5, 1))
        label = self.tone_labels[self.active_tone_param]
        if self.is_test_mode:
            print(f"[Test] Knob 2 (Tone) turned DOWN: {label} -> {new_val:+.1f} dB")
        if self.wifi_entering_pass:
            return

        self.tone[param] = new_val
        self.save_tone_dsp()
        pct = int(((new_val + 12.0) / 24.0) * 100)
        self.trigger_hud(label.upper(), f"{new_val:+.1f} dB", pct)

    def on_tone_click(self):
        if self.wifi_entering_pass:
            return

        # Cycle active band: Bass -> Mid -> Treble -> Input Gain
        self.active_tone_param = (self.active_tone_param + 1) % len(self.tone_params)
        label = self.tone_labels[self.active_tone_param]
        param = self.tone_params[self.active_tone_param]
        val = float(self.tone.get(param, 0.0))
        if self.is_test_mode:
            print(f"[Test] Knob 2 (Tone) CLICKED -> Selected {label} ({val:+.1f} dB)")
        pct = int(((val + 12.0) / 24.0) * 100)
        self.trigger_hud(label.upper(), f"{val:+.1f} dB", pct)

    def on_tone_long_press(self):
        """Long press Knob 2: Resets Tone DSP to Flat (0.0 dB)."""
        if self.is_test_mode:
            print("[Test] Knob 2 (Tone) LONG-PRESSED -> Resetting Tone to Flat (0 dB)")
        self.tone["bassGainDb"] = 0.0
        self.tone["midGainDb"] = 0.0
        self.tone["trebleGainDb"] = 0.0
        self.save_tone_dsp()
        self.trigger_hud("TONE RESET", "Flat (0 dB)", 50)

    def save_tone_dsp(self):
        try:
            requests.post(f"{self.api_base}/api/tone", json={
                "bassGainDb": self.tone.get("bassGainDb", 0),
                "midGainDb": self.tone.get("midGainDb", 0),
                "trebleGainDb": self.tone.get("trebleGainDb", 0),
                "inputGainDb": self.tone.get("inputGainDb", 0)
            }, timeout=1)
        except Exception:
            pass

    # =========================================================================
    # Knob 3 (Right): Speakers Navigation & Wi-Fi Picker
    # =========================================================================
    def on_speakers_up(self):
        if self.is_test_mode:
            print("[Test] Knob 3 (Speakers) turned UP")
        if self.current_page == 1 and self.outputs:
            self.selected_speaker_idx = (self.selected_speaker_idx - 1) % len(self.outputs)
        elif self.current_page == 3 and not self.wifi_entering_pass and self.wifi_scan_results:
            self.selected_wifi_idx = (self.selected_wifi_idx - 1) % len(self.wifi_scan_results)

    def on_speakers_down(self):
        if self.is_test_mode:
            print("[Test] Knob 3 (Speakers) turned DOWN")
        if self.current_page == 1 and self.outputs:
            self.selected_speaker_idx = (self.selected_speaker_idx + 1) % len(self.outputs)
        elif self.current_page == 3 and not self.wifi_entering_pass and self.wifi_scan_results:
            self.selected_wifi_idx = (self.selected_wifi_idx + 1) % len(self.wifi_scan_results)

    def on_speakers_click(self):
        if self.is_test_mode:
            print("[Test] Knob 3 (Speakers) CLICKED")
        if self.current_page == 1 and self.outputs:
            # Toggle connection for selected speaker
            spk = self.outputs[self.selected_speaker_idx]
            new_state = not spk.get("selected", False)
            spk["selected"] = new_state
            try:
                requests.put(f"{self.api_base}/api/owntone/outputs/{spk['id']}/toggle",
                             json={"selected": new_state}, timeout=1)
            except Exception:
                pass
            status_text = "CONNECTED" if new_state else "DISCONNECTED"
            self.trigger_hud(spk.get("name", "SPEAKER")[:14].upper(), status_text, 100 if new_state else 0)

        elif self.current_page == 3:
            if not self.wifi_entering_pass and self.wifi_scan_results:
                net = self.wifi_scan_results[self.selected_wifi_idx]
                if "wpa" in net.get("security", "").lower():
                    # Enter password mode
                    self.wifi_entering_pass = True
                    self.wifi_entered_pass = ""
                    self.wifi_char_idx = 0
                else:
                    # Connect open network directly
                    self.trigger_hud("CONNECTING", net.get("ssid", "")[:14], 50)
                    threading.Thread(target=self.do_wifi_connect, args=(net.get("ssid"), ""), daemon=True).start()
            elif self.wifi_entering_pass:
                # Confirm password and connect
                net = self.wifi_scan_results[self.selected_wifi_idx]
                self.wifi_entering_pass = False
                self.trigger_hud("CONNECTING", net.get("ssid", "")[:14], 50)
                threading.Thread(target=self.do_wifi_connect,
                                args=(net.get("ssid"), self.wifi_entered_pass), daemon=True).start()

    def on_speakers_long_press(self):
        """Long press Knob 3: Toggle Auto-Connect preference for highlighted speaker."""
        if self.current_page == 1 and self.outputs:
            spk = self.outputs[self.selected_speaker_idx]
            curr_auto = spk.get("autoConnect", False)
            try:
                requests.put(f"{self.api_base}/api/owntone/outputs/{spk['id']}/autoconnect", timeout=1)
                spk["autoConnect"] = not curr_auto
                status_text = "AUTOCONNECT ON" if not curr_auto else "AUTOCONNECT OFF"
                self.trigger_hud(spk.get("name", "SPEAKER")[:14].upper(), status_text, 100)
            except Exception:
                pass

    def do_wifi_connect(self, ssid: str, password: str):
        if analogair_wifi:
            res = analogair_wifi.connect_wifi(ssid, password)
            if res.get("success"):
                self.trigger_hud("WI-FI CONNECTED", ssid[:14], 100)
                self.fetch_wifi_status()
            else:
                self.trigger_hud("WI-FI FAILED", "Check Pass", 0)

    # =========================================================================
    # Front Tactile Buttons
    # =========================================================================
    def cycle_page(self):
        """Button 1 (Page): Cycles between Now Playing, Speakers, Tone DSP, and Wi-Fi."""
        self.wifi_entering_pass = False
        self.current_page = (self.current_page + 1) % len(self.pages)
        if self.is_test_mode:
            print(f"[Test] Button 1 (Page) PRESSED -> Screen: {self.pages[self.current_page]}")
        if self.current_page == 3:
            # Trigger scan on entering Wi-Fi page
            threading.Thread(target=self.refresh_wifi_scan, daemon=True).start()

    def on_action_button(self):
        """Button 2 (Action): Triggers instant Shazam re-identification."""
        if self.is_test_mode:
            print("[Test] Button 2 (Action) PRESSED -> Triggering Shazam scan")
        self.trigger_hud("IDENTIFYING", "Listening to vinyl...", 50)
        try:
            requests.post(f"{self.api_base}/api/recognize", timeout=1)
        except Exception:
            pass

    def on_source_button(self):
        """Button 3 (Source): Cycles input source (Vinyl -> Tape -> CD -> Standby)."""
        sources = ["vinyl", "tape", "cd", "aux"]
        curr = self.now_playing.get("sourceType", "vinyl")
        idx = (sources.index(curr) + 1) % len(sources) if curr in sources else 0
        new_source = sources[idx]
        self.now_playing["sourceType"] = new_source
        if self.is_test_mode:
            print(f"[Test] Button 3 (Source) PRESSED -> Switched to source: {new_source.upper()}")
        try:
            requests.post(f"{self.api_base}/api/settings", json={"sourceType": new_source}, timeout=1)
            self.trigger_hud("SOURCE", new_source.upper(), 100)
        except Exception:
            pass

    # =========================================================================
    # HUD Temporary Overlay Trigger
    # =========================================================================
    def trigger_hud(self, title: str, value: str, bar_pct: int = 50, duration: float = 2.0):
        self.hud_title = title
        self.hud_value = value
        self.hud_bar_pct = max(0, min(100, bar_pct))
        self.hud_timeout = time.time() + duration

    # =========================================================================
    # Data Polling & Synchronization
    # =========================================================================
    def poll_api_loop(self):
        while self.running:
            try:
                # 1. State
                r = requests.get(f"{self.api_base}/api/state", timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    self.now_playing = {
                        "status": data.get("status", "idle"),
                        "artist": data.get("artist", ""),
                        "album": data.get("album", ""),
                        "title": data.get("title", ""),
                        "sourceType": data.get("sourceType", "vinyl")
                    }
                    if "tone" in data:
                        self.tone.update(data["tone"])

                # 2. Speakers
                r2 = requests.get(f"{self.api_base}/api/owntone/outputs", timeout=2)
                if r2.status_code == 200:
                    out_data = r2.json()
                    self.outputs = out_data.get("outputs", [])
                    # Synchronize master volume
                    selected_vols = [o.get("volume", 75) for o in self.outputs if o.get("selected")]
                    if selected_vols:
                        self.master_volume = int(sum(selected_vols) / len(selected_vols))

                # 3. Wi-Fi status periodically
                self.fetch_wifi_status()

            except Exception:
                pass
            time.sleep(3)

    def fetch_wifi_status(self):
        if analogair_wifi:
            try:
                self.wifi_status = analogair_wifi.get_wifi_status()
            except Exception:
                pass

    def refresh_wifi_scan(self):
        if analogair_wifi:
            try:
                self.wifi_scan_results = analogair_wifi.scan_wifi_networks()
            except Exception:
                pass

    # =========================================================================
    # Display Rendering Engine (160x128 ST7735)
    # =========================================================================
    def render_header(self, title: str, badge: str = "ANALOGAIR"):
        # Header bar
        self.draw.rectangle([(0, 0), (self.width, 16)], fill=(22, 22, 28))
        self.draw.text((4, 2), badge, fill=(245, 158, 11))
        # Right aligned page indicator
        page_str = f"P{self.current_page + 1}/4"
        self.draw.text((self.width - 28, 2), page_str, fill=(160, 160, 175))
        self.draw.line([(0, 16), (self.width, 16)], fill=(45, 45, 55), width=1)

    def draw_progress_bar(self, x: int, y: int, w: int, h: int, pct: int, color=(245, 158, 11)):
        self.draw.rectangle([(x, y), (x + w, y + h)], outline=(50, 50, 60), fill=(20, 20, 25))
        fill_w = int((w - 2) * (pct / 100.0))
        if fill_w > 0:
            self.draw.rectangle([(x + 1, y + 1), (x + 1 + fill_w, y + h - 1)], fill=color)

    def render_now_playing_screen(self):
        status = self.now_playing.get("status", "idle")
        is_playing = status == "playing"
        badge = "LIVE VINYL" if is_playing else "STANDBY"
        self.render_header("NOW PLAYING", badge)

        artist = self.now_playing.get("artist", "Audio-Technica")[:22]
        album = self.now_playing.get("album", "Turntable Standby")[:24]
        title = self.now_playing.get("title", "AnalogAir Vinyl")[:24]

        # Typography
        # Artist
        self.draw.text((6, 22), artist, fill=(255, 255, 255))
        # Album
        self.draw.text((6, 38), album, fill=(217, 119, 6))
        # Track Title
        self.draw.text((6, 54), title, fill=(180, 180, 195))

        # Bottom Status Section (No VU Meter as requested!)
        # Show active AirPlay speakers count and master volume
        active_count = sum(1 for o in self.outputs if o.get("selected"))
        speaker_summary = f"{active_count} Speaker{'s' if active_count != 1 else ''}" if active_count > 0 else "No Speakers"

        self.draw.line([(0, 78), (self.width, 78)], fill=(35, 35, 45), width=1)
        self.draw.text((6, 84), "OUTPUT:", fill=(140, 140, 150))
        self.draw.text((54, 84), speaker_summary, fill=(34, 197, 94) if active_count > 0 else (239, 68, 68))

        # Master Volume Bar
        self.draw.text((6, 98), f"VOL: {self.master_volume}%", fill=(245, 158, 11))
        self.draw_progress_bar(64, 100, 88, 8, self.master_volume)

        # Tone badge
        bass = self.tone.get("bassGainDb", 0.0)
        self.draw.text((6, 112), f"BASS: {bass:+.1f}dB", fill=(130, 130, 145))
        ip_short = self.wifi_status.get("ip", "127.0.0.1").split(".")
        ip_tail = f".{ip_short[-1]}" if len(ip_short) == 4 else ""
        self.draw.text((self.width - 55, 112), f"IP: {ip_tail}", fill=(100, 100, 115))

    def render_speakers_screen(self):
        self.render_header("SPEAKERS", "AIRPLAY")

        if not self.outputs:
            self.draw.text((10, 45), "Scanning OwnTone...", fill=(200, 200, 200))
            self.draw.text((10, 65), "Check AirPlay devices", fill=(140, 140, 150))
            return

        # Display up to 3 speakers visible per screen
        start_idx = max(0, min(self.selected_speaker_idx - 1, len(self.outputs) - 3))
        y = 22
        for idx in range(start_idx, min(start_idx + 3, len(self.outputs))):
            spk = self.outputs[idx]
            is_cursor = (idx == self.selected_speaker_idx)
            is_active = spk.get("selected", False)
            vol = spk.get("volume", 75)

            # Highlight cursor row
            if is_cursor:
                self.draw.rectangle([(2, y - 1), (self.width - 2, y + 24)], fill=(32, 32, 42), outline=(245, 158, 11))

            cursor_mark = ">" if is_cursor else " "
            check_mark = "[*]" if is_active else "[ ]"
            name = spk.get("name", "Speaker")[:14]

            color = (255, 255, 255) if is_active else (160, 160, 175)
            self.draw.text((4, y + 2), f"{cursor_mark}{check_mark} {name}", fill=color)

            # Mini volume bar for each speaker
            self.draw_progress_bar(self.width - 42, y + 4, 36, 6, vol,
                                   color=(34, 197, 94) if is_active else (120, 120, 130))
            self.draw.text((self.width - 38, y + 12), f"{vol}%", fill=(130, 130, 145))

            y += 26

        # Footer guide
        self.draw.line([(0, 112), (self.width, 112)], fill=(35, 35, 45), width=1)
        self.draw.text((4, 115), "K3: Select | K1: Vol", fill=(140, 140, 155))

    def render_tone_screen(self):
        self.render_header("TONE DSP", "EQUALIZER")

        y = 22
        for i, (param, label) in enumerate(zip(self.tone_params, self.tone_labels)):
            is_selected = (i == self.active_tone_param)
            val = float(self.tone.get(param, 0.0))
            pct = int(((val + 12.0) / 24.0) * 100)

            # Highlight selected row
            if is_selected:
                self.draw.rectangle([(2, y - 1), (self.width - 2, y + 20)], fill=(32, 32, 42), outline=(245, 158, 11))

            cursor = ">" if is_selected else " "
            color = (255, 255, 255) if is_selected else (170, 170, 185)
            self.draw.text((4, y + 2), f"{cursor}{label}", fill=color)
            self.draw.text((70, y + 2), f"{val:+.1f}dB", fill=(245, 158, 11) if is_selected else (150, 150, 165))

            # Horizontal Tone slider
            self.draw_progress_bar(116, y + 4, 40, 7, pct,
                                   color=(245, 158, 11) if is_selected else (100, 100, 115))
            y += 22

        # Footer guide
        self.draw.line([(0, 112), (self.width, 112)], fill=(35, 35, 45), width=1)
        self.draw.text((4, 115), "K2: Adjust | Click: Next", fill=(140, 140, 155))

    def render_wifi_screen(self):
        self.render_header("WI-FI SETUP", "NETWORK")

        # Case 1: Manual password entry with knob dialer
        if self.wifi_entering_pass and self.wifi_scan_results:
            net = self.wifi_scan_results[self.selected_wifi_idx]
            self.draw.text((4, 20), f"SSID: {net.get('ssid','')[:16]}", fill=(255, 255, 255))
            # Current password entered with masked view
            masked = "".join(["*" for _ in self.wifi_entered_pass[:-1]])
            if len(self.wifi_entered_pass) > 0:
                masked += self.wifi_entered_pass[-1]
            self.draw.text((4, 38), f"Pass: {masked}_", fill=(245, 158, 11))

            # Current dial character
            curr_char = self.wifi_charset[self.wifi_char_idx]
            display_char = "[SPACE]" if curr_char == " " else f"[{curr_char}]"
            self.draw.rectangle([(55, 60), (105, 84)], fill=(35, 35, 45), outline=(245, 158, 11))
            self.draw.text((62, 66), display_char, fill=(255, 255, 255))

            self.draw.text((4, 94), "K1: Dial | Click K1: Add", fill=(150, 150, 165))
            self.draw.text((4, 108), "K2: Back | Click K3: OK", fill=(150, 150, 165))
            return

        # Case 2: Offline or Hotspot -> Render instant QR Code!
        if not self.wifi_status.get("connected") or self.wifi_status.get("isHotspot"):
            self.draw.text((4, 20), "NO WI-FI CONNECTION", fill=(239, 68, 68))
            self.draw.text((4, 34), "Scan to connect phone:", fill=(200, 200, 210))

            # Generate and draw 60x60 QR code in center
            if analogair_wifi:
                qr_img = analogair_wifi.generate_wifi_qr_image(size=56)
                if qr_img:
                    self.canvas.paste(qr_img, (6, 52))

            # Details next to QR Code
            self.draw.text((68, 54), "SSID:", fill=(140, 140, 150))
            self.draw.text((68, 66), "AnalogAir-Setup", fill=(245, 158, 11))
            self.draw.text((68, 80), "Pass: analogair", fill=(180, 180, 195))
            self.draw.text((68, 94), "192.168.4.1:3000", fill=(140, 140, 150))

            self.draw.line([(0, 112), (self.width, 112)], fill=(35, 35, 45), width=1)
            self.draw.text((4, 115), "Press K3 to scan SSIDs", fill=(140, 140, 155))
            return

        # Case 3: Connected & Normal -> Show Status & Scan List
        ssid = self.wifi_status.get("ssid", "Connected")[:16]
        ip = self.wifi_status.get("ip", "127.0.0.1")
        sig = self.wifi_status.get("signal", 80)

        self.draw.text((4, 20), f"SSID: {ssid}", fill=(34, 197, 94))
        self.draw.text((4, 34), f"IP:   {ip}:3000", fill=(255, 255, 255))
        self.draw.text((4, 48), f"SIG:  {sig}%", fill=(170, 170, 185))
        self.draw_progress_bar(64, 50, 88, 6, sig, color=(34, 197, 94))

        # Show scanned networks preview below
        self.draw.line([(0, 64), (self.width, 64)], fill=(35, 35, 45), width=1)
        self.draw.text((4, 68), "AVAILABLE WI-FI:", fill=(140, 140, 150))

        if self.wifi_scan_results:
            y = 82
            start = max(0, min(self.selected_wifi_idx, len(self.wifi_scan_results) - 2))
            for i in range(start, min(start + 2, len(self.wifi_scan_results))):
                net = self.wifi_scan_results[i]
                is_cur = (i == self.selected_wifi_idx)
                cur_mark = ">" if is_cur else " "
                self.draw.text((4, y), f"{cur_mark}{net.get('ssid','')[:16]}",
                               fill=(245, 158, 11) if is_cur else (170, 170, 185))
                y += 14
        else:
            self.draw.text((4, 84), "Turn K3 to view networks", fill=(120, 120, 130))

        self.draw.line([(0, 112), (self.width, 112)], fill=(35, 35, 45), width=1)
        self.draw.text((4, 115), "Click K3 to switch Wi-Fi", fill=(140, 140, 155))

    def render_hud_overlay(self):
        """Temporary floating HUD for volume and tone changes."""
        if time.time() > self.hud_timeout:
            return

        box_w = 140
        box_h = 44
        box_x = (self.width - box_w) // 2
        box_y = (self.height - box_h) // 2

        # Outer backdrop
        self.draw.rectangle([(box_x, box_y), (box_x + box_w, box_y + box_h)],
                            fill=(18, 18, 24), outline=(245, 158, 11), width=2)
        # Title
        self.draw.text((box_x + 8, box_y + 4), self.hud_title[:18], fill=(245, 158, 11))
        # Value
        self.draw.text((box_x + box_w - 48, box_y + 4), self.hud_value[:8], fill=(255, 255, 255))
        # Progress bar
        self.draw_progress_bar(box_x + 8, box_y + 24, box_w - 16, 12, self.hud_bar_pct)

    def draw_frame(self):
        # Clear background
        self.draw.rectangle([(0, 0), (self.width, self.height)], fill=(12, 12, 16))

        # Render active page
        if self.current_page == 0:
            self.render_now_playing_screen()
        elif self.current_page == 1:
            self.render_speakers_screen()
        elif self.current_page == 2:
            self.render_tone_screen()
        elif self.current_page == 3:
            self.render_wifi_screen()

        # Render HUD Overlay if active
        self.render_hud_overlay()

        # Push to ST7735 SPI display
        if self.disp:
            try:
                self.disp.display(self.canvas)
            except Exception as e:
                pass

    def run(self):
        print("[Deadstream] Starting AnalogAir Deadstream display service...")
        # Start API polling thread
        t = threading.Thread(target=self.poll_api_loop, daemon=True)
        t.start()

        # Main rendering loop (~15 FPS)
        while self.running:
            try:
                self.draw_frame()
                time.sleep(0.065)
            except KeyboardInterrupt:
                break
            except Exception as e:
                time.sleep(0.1)

        print("[Deadstream] Service stopped.")

if __name__ == "__main__":
    controller = DeadstreamController()
    controller.run()
