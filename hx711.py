import time
import statistics
import threading
import RPi.GPIO as GPIO


class HX711:
    def __init__(self, dout_pin=5, pd_sck_pin=6, gain=128):
        self.dout_pin = dout_pin
        self.pd_sck_pin = pd_sck_pin
        self.gain = gain
        self.OFFSET = 0
        self.SCALE = 1.0
        self._lock = threading.Lock()

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pd_sck_pin, GPIO.OUT)
        GPIO.setup(self.dout_pin, GPIO.IN)
        self.reset()

    def is_ready(self):
        return GPIO.input(self.dout_pin) == 0

    def read_raw(self):
        # Wait for conversion ready (DOUT low). Do NOT sleep while SCK is high —
        # HX711 powers down if PD_SCK stays high > ~60µs.
        deadline = time.monotonic() + 0.5
        while not self.is_ready():
            if time.monotonic() > deadline:
                raise TimeoutError("HX711 not ready")
            time.sleep(0.001)

        count = 0
        for _ in range(24):
            GPIO.output(self.pd_sck_pin, True)
            count = count << 1
            GPIO.output(self.pd_sck_pin, False)
            if GPIO.input(self.dout_pin):
                count += 1

        for _ in range(1 if self.gain == 128 else 3):
            GPIO.output(self.pd_sck_pin, True)
            GPIO.output(self.pd_sck_pin, False)

        if count & 0x800000:
            count -= 0x1000000

        return count

    def _median_raw(self, times):
        values = []
        for i in range(times):
            values.append(self.read_raw())
            if i + 1 < times:
                time.sleep(0.02)
        return statistics.median(values)

    def get_weight(self, times=3):
        with self._lock:
            raw = self._median_raw(max(1, times))
            return (raw - self.OFFSET) / self.SCALE

    def tare(self, times=15):
        with self._lock:
            self.OFFSET = self._median_raw(max(1, times))

    def zero(self, times=15):
        self.tare(times)

    def set_reference_unit(self, reference_unit):
        self.SCALE = reference_unit if reference_unit != 0 else 1.0

    def set_scale(self, scale):
        self.set_reference_unit(scale)

    def reset(self):
        GPIO.output(self.pd_sck_pin, False)

    def clean_up(self):
        GPIO.cleanup()
