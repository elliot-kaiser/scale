import time
import threading
from PIL import Image, ImageDraw, ImageFont
import board
import busio
import adafruit_ssd1306
from gpiozero import Button

G_TO_OZ = 0.035274

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
        self.font = ImageFont.load_default()

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
            self.current_ingredient = name[:15]
            self.target_weight_g = target_g
            self.mode = "GUIDED_STEP"  # Set mode last to prevent partial draws

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

    def _format_weight(self, weight_g, unit):
        display_w = weight_g * G_TO_OZ if unit == "oz" else weight_g
        if unit == "oz":
            return f"{display_w:.2f}oz", display_w
        return f"{display_w:.1f}g", display_w

    def render_display(self, weight_g):
        if not self.hardware_ok:
            return

        state = self.get_display_state()
        unit = state["unit"]
        mode = state["mode"]

        # Clear drawing buffer completely
        self.draw.rectangle((0, 0, 128, 64), outline=0, fill=0)

        if mode == "FREE_WEIGH":
            _, display_w = self._format_weight(weight_g, unit)
            w_str = f"{display_w:.2f}" if unit == "oz" else f"{display_w:.1f}"

            self.draw.text((0, 0), "SMART KITCHEN SCALE", font=self.font, fill=255)
            self.draw.line((0, 12, 128, 12), fill=255)
            self.draw.text((10, 26), w_str, font=self.font, fill=255)
            self.draw.text((100, 48), unit.upper(), font=self.font, fill=255)

        elif mode == "GUIDED_STEP":
            diff = abs(weight_g - state["target_weight_g"])
            status = " [OK!]" if diff <= 3.0 else ""
            live_str, _ = self._format_weight(weight_g, unit)
            target_str, _ = self._format_weight(state["target_weight_g"], unit)

            self.draw.text((0, 0), f"[{state['step_info']}] {state['current_ingredient']}", font=self.font, fill=255)
            self.draw.line((0, 12, 128, 12), fill=255)
            self.draw.text((0, 18), f"Tgt: {target_str}", font=self.font, fill=255)
            self.draw.text((0, 32), f"Live: {live_str}{status}", font=self.font, fill=255)
            self.draw.text((0, 48), "Press TARE -> Next", font=self.font, fill=255)

        # Push single unified image to OLED hardware buffer
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

# Alias for backwards compatibility
DisplayManager = ScaleHardwareManager