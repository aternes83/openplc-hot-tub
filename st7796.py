"""Minimal ST7796 SPI display driver for MicroPython.

Compatible with spa_control.py:

    lcd = ST7796(spi, 320, 480, reset=..., cs=..., dc=..., rotation=1)

This driver avoids a full-screen framebuffer so it can run on ESP32-S3 boards
where only internal SRAM is available to the MicroPython heap.
"""

from micropython import const
import framebuf
import gc
import utime

_SWRESET = const(0x01)
_SLPOUT = const(0x11)
_INVOFF = const(0x20)
_INVON = const(0x21)
_DISPON = const(0x29)
_CASET = const(0x2A)
_RASET = const(0x2B)
_RAMWR = const(0x2C)
_MADCTL = const(0x36)
_COLMOD = const(0x3A)

_MADCTL_BGR = const(0x08)
_MADCTL_MV = const(0x20)
_MADCTL_MX = const(0x40)
_MADCTL_MY = const(0x80)

_GAMMA_PLUS = b"\xF0\x09\x0B\x06\x04\x15\x2F\x54\x42\x3C\x17\x14\x18\x1B"
_GAMMA_MINUS = b"\xE0\x09\x0B\x06\x04\x03\x2B\x43\x42\x3B\x16\x14\x17\x1B"


class ST7796:
    _ROTATION_TABLE = (
        _MADCTL_MX | _MADCTL_BGR,
        _MADCTL_MV | _MADCTL_MY | _MADCTL_MX | _MADCTL_BGR,
        _MADCTL_MY | _MADCTL_BGR,
        _MADCTL_MV | _MADCTL_BGR,
    )

    def __init__(
        self,
        spi,
        width,
        height,
        reset=None,
        cs=None,
        dc=None,
        rotation=0,
        invert=False,
        x_gap=0,
        y_gap=0,
    ):
        self.spi = spi
        self.reset = reset
        self.cs = cs
        self.dc = dc
        self.x_gap = int(x_gap)
        self.y_gap = int(y_gap)

        self._panel_width = int(width)
        self._panel_height = int(height)
        self.rotation = int(rotation) % 4

        if self.rotation & 1:
            self.width = self._panel_height
            self.height = self._panel_width
        else:
            self.width = self._panel_width
            self.height = self._panel_height

        gc.collect()
        self._chunk = bytearray(2048)

        self._init_display(invert=invert)

    def _write(self, data):
        chunk = 4096
        if isinstance(data, (bytes, bytearray)):
            mv = memoryview(data)
        else:
            mv = data
        for offset in range(0, len(mv), chunk):
            self.spi.write(mv[offset : offset + chunk])

    def _begin_data(self):
        if self.cs is not None:
            self.cs.value(0)
        if self.dc is not None:
            self.dc.value(1)

    def _end_data(self):
        if self.cs is not None:
            self.cs.value(1)

    def _cmd(self, command, data=None):
        if self.cs is not None:
            self.cs.value(0)
        if self.dc is not None:
            self.dc.value(0)
        self.spi.write(bytearray([command]))
        if data:
            if self.dc is not None:
                self.dc.value(1)
            self._write(data)
        if self.cs is not None:
            self.cs.value(1)

    def _set_window(self, x0, y0, x1, y1):
        x0 += self.x_gap
        x1 += self.x_gap
        y0 += self.y_gap
        y1 += self.y_gap
        self._cmd(
            _CASET,
            bytearray(
                [
                    (x0 >> 8) & 0xFF,
                    x0 & 0xFF,
                    (x1 >> 8) & 0xFF,
                    x1 & 0xFF,
                ]
            ),
        )
        self._cmd(
            _RASET,
            bytearray(
                [
                    (y0 >> 8) & 0xFF,
                    y0 & 0xFF,
                    (y1 >> 8) & 0xFF,
                    y1 & 0xFF,
                ]
            ),
        )
        self._cmd(_RAMWR)

    def _fill_pixels(self, count, color):
        if count <= 0:
            return

        hi = (color >> 8) & 0xFF
        lo = color & 0xFF
        chunk = self._chunk
        for i in range(0, len(chunk), 2):
            chunk[i] = hi
            chunk[i + 1] = lo

        chunk_pixels = len(chunk) // 2
        self._begin_data()
        remaining = count
        while remaining > 0:
            batch = remaining if remaining < chunk_pixels else chunk_pixels
            self.spi.write(chunk[: batch * 2])
            remaining -= batch
        self._end_data()

    def _draw_buffer(self, x, y, w, h, buffer):
        if w <= 0 or h <= 0:
            return
        self._set_window(x, y, x + w - 1, y + h - 1)
        self._begin_data()
        self._write(buffer)
        self._end_data()

    def _hardware_reset(self):
        if self.reset is None:
            return
        self.reset.value(1)
        utime.sleep_ms(5)
        self.reset.value(0)
        utime.sleep_ms(20)
        self.reset.value(1)
        utime.sleep_ms(150)

    def _init_display(self, invert=False):
        self._hardware_reset()

        self._cmd(_SWRESET)
        utime.sleep_ms(120)
        self._cmd(_SLPOUT)
        utime.sleep_ms(120)

        self._cmd(0xF0, b"\xC3")
        self._cmd(0xF0, b"\x96")
        self._cmd(_MADCTL, bytearray([self._ROTATION_TABLE[self.rotation]]))
        self._cmd(_COLMOD, b"\x55")
        self._cmd(0xB4, b"\x01")
        self._cmd(0xB6, b"\x80\x02\x3B")
        self._cmd(0xE8, b"\x40\x8A\x00\x00\x29\x19\xA5\x33")
        self._cmd(0xC1, b"\x06")
        self._cmd(0xC2, b"\xA7")
        self._cmd(0xC5, b"\x18")
        utime.sleep_ms(120)
        self._cmd(0xE0, _GAMMA_PLUS)
        self._cmd(0xE1, _GAMMA_MINUS)
        utime.sleep_ms(120)
        self._cmd(0xF0, b"\x3C")
        self._cmd(0xF0, b"\x69")
        utime.sleep_ms(120)

        self._cmd(_INVON if invert else _INVOFF)
        self._cmd(_DISPON)
        utime.sleep_ms(120)

        self.fill(0)

    def pixel(self, x, y, color):
        self.fill_rect(x, y, 1, 1, color)

    def hline(self, x, y, w, color):
        self.fill_rect(x, y, w, 1, color)

    def vline(self, x, y, h, color):
        self.fill_rect(x, y, 1, h, color)

    def line(self, x1, y1, x2, y2, color):
        if x1 == x2:
            self.vline(x1, min(y1, y2), abs(y2 - y1) + 1, color)
        elif y1 == y2:
            self.hline(min(x1, x2), y1, abs(x2 - x1) + 1, color)
        else:
            self._fb_line(x1, y1, x2, y2, color)

    @staticmethod
    def _swap_bytes(color):
        """Swap high and low bytes of a 16-bit colour.

        framebuf.RGB565 stores each pixel as a native uint16_t, which on the
        little-endian ESP32 means the low byte is written to memory first.
        _draw_buffer then streams those bytes directly over SPI.  The ST7796
        expects big-endian (high byte first), so every colour passed into a
        FrameBuffer must be byte-swapped so the bytes land in the right order
        on the wire.
        """
        return ((color & 0xFF) << 8) | (color >> 8)

    def _fb_line(self, x1, y1, x2, y2, color):
        w = abs(x2 - x1) + 1
        h = abs(y2 - y1) + 1
        buf = bytearray(w * h * 2)
        fb = framebuf.FrameBuffer(buf, w, h, framebuf.RGB565)
        fb.line(x1 - min(x1, x2), y1 - min(y1, y2), x2 - min(x1, x2), y2 - min(y1, y2), self._swap_bytes(color))
        self._draw_buffer(min(x1, x2), min(y1, y2), w, h, buf)

    def rect(self, x, y, w, h, color):
        if w <= 0 or h <= 0:
            return
        self.hline(x, y, w, color)
        self.hline(x, y + h - 1, w, color)
        self.vline(x, y, h, color)
        self.vline(x + w - 1, y, h, color)

    def fill_rect(self, x, y, w, h, color):
        if w <= 0 or h <= 0:
            return
        self._set_window(x, y, x + w - 1, y + h - 1)
        self._fill_pixels(w * h, color)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def text(self, s, x, y, color=0xFFFF):
        w = len(s) * 8
        h = 8
        buf = bytearray(w * h * 2)
        fb = framebuf.FrameBuffer(buf, w, h, framebuf.RGB565)
        fb.fill(0)
        fb.text(s, 0, 0, self._swap_bytes(color))
        self._draw_buffer(x, y, w, h, buf)

    def show(self):
        return
