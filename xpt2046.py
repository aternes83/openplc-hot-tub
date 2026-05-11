"""XPT2046 Touch module."""
from time import sleep
from micropython import const  # type: ignore


class Touch(object):
    """Serial interface for XPT2046 Touch Screen Controller."""

    GET_X = const(0b11010000)
    GET_Y = const(0b10010000)
    GET_Z1 = const(0b10110000)
    GET_Z2 = const(0b11000000)
    GET_TEMP0 = const(0b10000000)
    GET_TEMP1 = const(0b11110000)
    GET_BATTERY = const(0b10100000)
    GET_AUX = const(0b11100000)

    def __init__(
        self,
        spi,
        cs,
        int_pin=None,
        int_handler=None,
        width=240,
        height=320,
        x_min=150,
        x_max=3900,
        y_min=150,
        y_max=3900,
        z_threshold=80,
        swap_xy=False,
        invert_x=False,
        invert_y=False,
        touch_baudrate=2_500_000,
        display_baudrate=10_000_000,
    ):
        self.spi = spi
        self.cs = cs
        self.cs.init(self.cs.OUT, value=1)
        self.rx_buf = bytearray(3)
        self.tx_buf = bytearray(3)
        self.width = width
        self.height = height
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max
        self.z_threshold = int(z_threshold)
        self.swap_xy = bool(swap_xy)
        self.invert_x = bool(invert_x)
        self.invert_y = bool(invert_y)
        self.touch_baudrate = int(touch_baudrate)
        self.display_baudrate = int(display_baudrate)
        self.x_multiplier = width / (x_max - x_min)
        self.x_add = x_min * -self.x_multiplier
        self.y_multiplier = height / (y_max - y_min)
        self.y_add = y_min * -self.y_multiplier

        self.int_pin = None
        self.int_handler = int_handler
        self.int_locked = False
        if int_pin is not None:
            self.int_pin = int_pin
            self.int_pin.init(int_pin.IN, int_pin.PULL_UP)
            if int_handler is not None:
                int_pin.irq(
                    trigger=int_pin.IRQ_FALLING | int_pin.IRQ_RISING,
                    handler=self.int_press,
                )

    def get_touch(self):
        """Take multiple samples to get accurate touch reading."""
        timeout = 2
        confidence = 5
        buff = [[0, 0] for _ in range(confidence)]
        buf_length = confidence
        buffptr = 0
        nsamples = 0
        while timeout > 0:
            if nsamples == buf_length:
                meanx = sum(c[0] for c in buff) // buf_length
                meany = sum(c[1] for c in buff) // buf_length
                dev = sum((c[0] - meanx) ** 2 + (c[1] - meany) ** 2 for c in buff) / buf_length
                if dev <= 50:
                    return self.normalize(meanx, meany)
            sample = self.raw_touch()
            if sample is None:
                nsamples = 0
            else:
                buff[buffptr] = sample
                buffptr = (buffptr + 1) % buf_length
                nsamples = min(nsamples + 1, buf_length)

            sleep(0.05)
            timeout -= 0.05
        return None

    def int_press(self, pin):
        if not pin.value() and not self.int_locked:
            self.int_locked = True
            buff = self.raw_touch()
            if buff is not None and self.int_handler is not None:
                x, y = self.normalize(*buff)
                self.int_handler(x, y)
            sleep(0.1)
        elif pin.value() and self.int_locked:
            sleep(0.1)
            self.int_locked = False

    def normalize(self, x, y):
        if self.swap_xy:
            x, y = y, x
        x = int(self.x_multiplier * x + self.x_add)
        y = int(self.y_multiplier * y + self.y_add)
        if self.invert_x:
            x = self.width - x
        if self.invert_y:
            y = self.height - y
        if x < 0:
            x = 0
        elif x >= self.width:
            x = self.width - 1
        if y < 0:
            y = 0
        elif y >= self.height:
            y = self.height - 1
        return x, y

    def _set_touch_spi(self):
        try:
            self.spi.init(baudrate=self.touch_baudrate)
        except Exception:
            pass

    def _set_display_spi(self):
        try:
            self.spi.init(baudrate=self.display_baudrate)
        except Exception:
            pass

    def is_touched(self):
        if self.int_pin is not None and self.int_pin.value() == 0:
            return True
        return self.send_command(self.GET_Z1) > self.z_threshold

    def _sample_once(self):
        self.send_command(self.GET_X)
        readings = []
        for _ in range(3):
            x = self.send_command(self.GET_X)
            y = self.send_command(self.GET_Y)
            if 0 < x <= 4095 and 0 < y <= 4095:
                readings.append((x, y))
        if not readings:
            return None
        raw_x = sum(value[0] for value in readings) // len(readings)
        raw_y = sum(value[1] for value in readings) // len(readings)
        return (raw_x, raw_y)

    def raw_touch(self):
        if not self.is_touched():
            return None
        return self._sample_once()

    def poll(self, samples=3):
        self._set_touch_spi()
        try:
            if not self.is_touched():
                return None

            readings = []
            for _ in range(samples):
                sample = self._sample_once()
                if sample is not None:
                    readings.append(sample)
            if not readings:
                return None

            raw_x = sum(sample[0] for sample in readings) // len(readings)
            raw_y = sum(sample[1] for sample in readings) // len(readings)
            return self.normalize(raw_x, raw_y)
        finally:
            self._set_display_spi()

    def send_command(self, command):
        self.tx_buf[0] = command
        self.cs(0)
        self.spi.write_readinto(self.tx_buf, self.rx_buf)
        self.cs(1)
        return (self.rx_buf[1] << 4) | (self.rx_buf[2] >> 4)
