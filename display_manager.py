import time
import threading
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import board
import busio
import adafruit_ssd1306
from gpiozero import Button

G_TO_OZ = 0.035274

_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_FONT_BOLD = _FONT_DIR / "DejaVuSansMono-Bold.ttf"
_FONT_REG = _FONT_DIR / "DejaVuSansMono.ttf"
_FONT_SANS = _FONT_DIR / "DejaVuSans.ttf"


def _load_font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


class ScaleHardwareManager:
    def __init__(self, tare_callback=None, next_step_callback=None):
        self._lock = threading.Lock()
        self.unit = "g"
        self.tare_callback = tare_callback
        self.next_step_callback = next_step_callback
        self.running = False
        self.thread = None

        # State Engine
        self.mode = "FREE_WEIGH"  # Strictly "FREE_WEIGH" or "GUIDED_STEP"
        self.current_ingredient = ""
        self.target_weight_g = 0.0
        self.step_info = ""

        # OLED Setup
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self.oled = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)
            self.oled.fill(0)
            self.oled.show()
            self.hardware_ok = True
        except Exception as e:
            print(f"[Hardware Warning] OLED setup: {e}")
            self.hardware_ok = False

        self.image = Image.new("1", (128, 64))
        self.draw = ImageDraw.Draw(self.image)

        # Typography: big weight, small chrome
        self.font_weight = _load_font(_FONT_BOLD, 28)
        self.font_weight_sm = _load_font(_FONT_BOLD, 22)
        self.font_label = _load_font(_FONT_SANS, 10)
        self.font_tiny = _load_font(_FONT_SANS, 9)
        self.font_unit = _load_font(_FONT_BOLD, 16)

        # Physical Push Buttons (Exclusive Ownership)
        try:
            self.btn_tare = Button(17, pull_up=True, bounce_time=0.1)
            self.btn_unit = Button(27, pull_up=True, bounce_time=0.1)

            self.btn_tare.when_pressed = self._handle_tare_press
            self.btn_unit.when_pressed = self._handle_unit_press
        except Exception as e:
            print(f"[Hardware Warning] Buttons setup: {e}")

    def _handle_tare_press(self):
        """When in guided mode, Tare button acts as Next Step. In free weigh, it zeroes scale."""
        if self.get_display_state()["mode"] == "GUIDED_STEP":
            if self.next_step_callback:
                self.next_step_callback()
        elif self.tare_callback:
            self.tare_callback()

    def _handle_unit_press(self):
        self.toggle_unit()

    def toggle_unit(self):
        with self._lock:
            self.unit = "oz" if self.unit == "g" else "g"

    def set_guided_step(self, step_info, name, target_g):
        """Safely sets guided mode parameters."""
        with self._lock:
            self.step_info = step_info
            self.current_ingredient = name[:18]
            self.target_weight_g = target_g
            self.mode = "GUIDED_STEP"

    def clear_guided_mode(self):
        """Returns OLED strictly to free-weighing mode."""
        with self._lock:
            self.mode = "FREE_WEIGH"
            self.step_info = ""
            self.current_ingredient = ""
            self.target_weight_g = 0.0

    def get_display_state(self):
        with self._lock:
            return {
                "unit": self.unit,
                "mode": self.mode,
                "step_info": self.step_info,
                "current_ingredient": self.current_ingredient,
                "target_weight_g": self.target_weight_g,
            }

    def _text_size(self, text, font):
        bbox = self.draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _centered_x(self, text, font, width=128):
        tw, _ = self._text_size(text, font)
        return max(0, (width - tw) // 2)

    def _format_weight_parts(self, weight_g, unit):
        display_w = weight_g * G_TO_OZ if unit == "oz" else weight_g
        if unit == "oz":
            return f"{display_w:.2f}", "oz", display_w
        # Drop trailing .0 for large whole numbers readability when >= 100
        if display_w >= 100:
            return f"{display_w:.0f}", "g", display_w
        return f"{display_w:.1f}", "g", display_w

    def _pick_weight_font(self, w_str):
        """Use slightly smaller font if number + unit won't fit."""
        tw, _ = self._text_size(w_str, self.font_weight)
        # Reserve ~28px for unit + gap on the right
        if tw > 92:
            return self.font_weight_sm
        return self.font_weight

    def _draw_progress_bar(self, x, y, width, height, pct):
        pct = max(0.0, min(1.0, pct))
        self.draw.rectangle((x, y, x + width, y + height), outline=255, fill=0)
        fill_w = int((width - 2) * pct)
        if fill_w > 0:
            self.draw.rectangle((x + 1, y + 1, x + 1 + fill_w, y + height - 1), outline=255, fill=255)

    def render_display(self, weight_g):
        if not self.hardware_ok:
            return

        state = self.get_display_state()
        unit = state["unit"]
        mode = state["mode"]

        self.draw.rectangle((0, 0, 128, 64), outline=0, fill=0)

        if mode == "FREE_WEIGH":
            w_str, unit_label, _ = self._format_weight_parts(weight_g, unit)
            weight_font = self._pick_weight_font(w_str)
            unit_text = unit_label.upper()

            # Keep content in the blue zone (y >= 16 on dual-color OLEDs).
            # Number + larger unit side-by-side, centered as a group.
            gap = 4
            nw, nh = self._text_size(w_str, weight_font)
            uw, uh = self._text_size(unit_text, self.font_unit)
            total_w = nw + gap + uw
            x0 = max(0, (128 - total_w) // 2)
            y_num = 18
            # Baseline-align unit toward the lower part of the number
            y_unit = y_num + max(0, nh - uh - 2)

            self.draw.text((x0, y_num), w_str, font=weight_font, fill=255)
            self.draw.text((x0 + nw + gap, y_unit), unit_text, font=self.font_unit, fill=255)

        elif mode == "GUIDED_STEP":
            target = float(state["target_weight_g"] or 0)
            live = float(weight_g or 0)
            diff = abs(live - target)
            on_target = target > 0 and diff <= 3.0

            w_str, unit_label, _ = self._format_weight_parts(live, unit)
            t_str, _, _ = self._format_weight_parts(target, unit)
            weight_font = self._pick_weight_font(w_str)

            # Yellow band (top): step + ingredient only
            header = f"{state['step_info']}  {state['current_ingredient']}".strip()
            if len(header) > 21:
                header = header[:20] + "…"
            self.draw.text((2, 2), header, font=self.font_tiny, fill=255)

            # Blue band: large live weight
            wx = self._centered_x(w_str, weight_font)
            self.draw.text((wx, 16), w_str, font=weight_font, fill=255)

            # Target / status + unit
            if on_target:
                status = f"OK  {unit_label.upper()}"
            elif target > 0:
                status = f"tgt {t_str} {unit_label}"
            else:
                status = unit_label.upper()
            sx = self._centered_x(status, self.font_label)
            self.draw.text((sx, 46), status, font=self.font_label, fill=255)

            if target > 0:
                pct = live / target if target else 0
                self._draw_progress_bar(8, 57, 112, 5, pct)
            else:
                self.draw.text((2, 55), "TARE = next", font=self.font_tiny, fill=255)

        try:
            self.oled.image(self.image)
            self.oled.show()
        except Exception:
            pass

    def start_loop(self, weight_provider_func):
        self.running = True

        def _loop():
            while self.running:
                w = weight_provider_func()
                self.render_display(w)
                time.sleep(0.2)

        self.thread = threading.Thread(target=_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.hardware_ok:
            self.oled.fill(0)
            self.oled.show()


DisplayManager = ScaleHardwareManager
