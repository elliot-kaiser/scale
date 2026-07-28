import time
import RPi.GPIO as GPIO

class HX711:
    def __init__(self, dout_pin=5, pd_sck_pin=6, gain=128):
        self.dout_pin = dout_pin
        self.pd_sck_pin = pd_sck_pin
        self.gain = gain
        self.OFFSET = 0
        self.SCALE = 1.0

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pd_sck_pin, GPIO.OUT)
        GPIO.setup(self.dout_pin, GPIO.IN)
        self.reset()

    def is_ready(self):
        return GPIO.input(self.dout_pin) == 0

    def read_raw(self):
        while not self.is_ready():
            time.sleep(0.001)

        count = 0
        for _ in range(24):
            GPIO.output(self.pd_sck_pin, True)
            count = count << 1
            GPIO.output(self.pd_sck_pin, False)
            if GPIO.input(self.dout_pin):
                count += 1

        # Pulse clock pin for gain setting
        for _ in range(1 if self.gain == 128 else 3):
            GPIO.output(self.pd_sck_pin, True)
            GPIO.output(self.pd_sck_pin, False)

        # 24-bit 2's complement conversion
        if count & 0x800000:
            count -= 0x1000000

        return count

    def get_weight(self, times=3):
        values = []
        for _ in range(times):
            values.append(self.read_raw())
            if times > 1:
                time.sleep(0.02)
        raw_avg = sum(values) / len(values)
        return (raw_avg - self.OFFSET) / self.SCALE

    def tare(self, times=15):
        values = []
        for _ in range(times):
            values.append(self.read_raw())
            if times > 1:
                time.sleep(0.02)
        self.OFFSET = sum(values) / len(values)

    def zero(self, times=15):
        """Alias so both hx.zero() and hx.tare() work seamlessly"""
        self.tare(times)

    def set_reference_unit(self, reference_unit):
        self.SCALE = reference_unit if reference_unit != 0 else 1.0

    def set_scale(self, scale):
        """Alias for set_reference_unit"""
        self.set_reference_unit(scale)

    def reset(self):
        GPIO.output(self.pd_sck_pin, False)

    def clean_up(self):
        GPIO.cleanup()