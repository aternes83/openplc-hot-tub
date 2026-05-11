"""
MicroPython spa control logic converted from SpaControl.st.

Plant assumptions:
- 240 Vac supply
- 5 kW heater output (single contactor)
- Pump 1 is two-speed (LOW/HIGH interlocked)
- Pump 2 and Pump 3 are single-speed
- Temperature inputs/setpoints in Fahrenheit

Board profile in this file:
- ESP32-S3-DevKitC-1-N8R8 pin map with practical GPIO assignments
- Inputs use pull-ups and are treated as active-high by default

Review relay board polarity and sensor scaling before energizing loads.
"""

try:
    import utime as time_mod
except ImportError:  # fallback for local simulation
    import time as time_mod

try:
    from machine import Pin, SPI
except ImportError:  # local testing stub
    class Pin:  # type: ignore
        IN = 0
        OUT = 1
        PULL_UP = 2

        def __init__(self, _pin, mode=IN, pull=None, value=0):
            self._v = 1 if value else 0
            self.mode = mode
            self.pull = pull

        def value(self, v=None):
            if v is None:
                return self._v
            self._v = 1 if v else 0
            return self._v

    class SPI:  # type: ignore
        def __init__(self, *_args, **_kwargs):
            pass


def ticks_ms():
    if hasattr(time_mod, "ticks_ms"):
        return time_mod.ticks_ms()
    return int(time_mod.monotonic() * 1000)


def ticks_diff(now, then):
    if hasattr(time_mod, "ticks_diff"):
        return time_mod.ticks_diff(now, then)
    return now - then


class OnDelay:
    def __init__(self, delay_ms):
        self.delay_ms = int(delay_ms)
        self._start_ms = None
        self.q = False

    def update(self, signal):
        now = ticks_ms()
        if signal:
            if self._start_ms is None:
                self._start_ms = now
            self.q = ticks_diff(now, self._start_ms) >= self.delay_ms
        else:
            self._start_ms = None
            self.q = False
        return self.q


class ThermostatHeat:
    def __init__(self):
        self.heat_on = False

    def update(
        self,
        enable,
        temp_f,
        setpoint_f,
        hysteresis_f,
        flow_ok,
        high_limit_ok,
        flow_proven,
        min_on_time_elapsed,
    ):
        sp_low = setpoint_f - (hysteresis_f * 0.5)
        sp_high = setpoint_f + (hysteresis_f * 0.5)

        if (
            (not enable)
            or (not flow_ok)
            or (not high_limit_ok)
            or (not flow_proven)
            or (not min_on_time_elapsed)
        ):
            self.heat_on = False
        elif temp_f < sp_low:
            self.heat_on = True
        elif temp_f > sp_high:
            self.heat_on = False

        return self.heat_on


class SpaController:
    """
    One call to step() = one control cycle (similar to PLC scan).
    """

    def __init__(self):
        # Tunables (Fahrenheit)
        self.temp_setpoint_f = 100.0
        self.temp_hysteresis_f = 2.0
        self.max_safe_temp_f = 105.0
        self.flow_prove_ms = 10_000
        self.pump_preheat_ms = 30_000
        self.default_run_ms = 20 * 60 * 1000  # 20 minutes default spa runtime
        self.light_run_ms = 60 * 60 * 1000  # 60 minutes default light runtime

        # Status/fault outputs
        self.x_fault = False
        self.i_fault_code = 0  # 0=none, 1=no flow, 2=high limit, 3=overtemp, 4=estop

        # Internal FB equivalents
        self._flow_prove = OnDelay(self.flow_prove_ms)
        self._pump_min_run = OnDelay(self.pump_preheat_ms)
        self._thermostat = ThermostatHeat()
        self._run_timer_start_ms = None
        self._light_timer_start_ms = None

    def step(self, inputs):
        # Required boolean inputs
        x_spa_enable_cmd = bool(inputs.get("xSpaEnable", False))
        x_pump_request = bool(inputs.get("xPumpRequest", False))
        x_heat_request = bool(inputs.get("xHeatRequest", False))
        x_pump1_high_request = bool(inputs.get("xPump1HighRequest", False))
        x_pump2_request = bool(inputs.get("xPump2Request", False))
        x_pump3_request = bool(inputs.get("xPump3Request", False))
        x_jets_request = bool(inputs.get("xJetsRequest", False))
        x_blower_request = bool(inputs.get("xBlowerRequest", False))
        x_light_request = bool(inputs.get("xLightRequest", False))
        x_flow_switch = bool(inputs.get("xFlowSwitch", False))
        x_high_limit_ok = bool(inputs.get("xHighLimitOK", True))
        x_remote_estop_ok = bool(inputs.get("xRemoteEStopOK", True))
        r_water_temp_f = float(inputs.get("rWaterTemp_F", 70.0))

        now = ticks_ms()
        if x_spa_enable_cmd:
            if self._run_timer_start_ms is None:
                self._run_timer_start_ms = now
            run_expired = ticks_diff(now, self._run_timer_start_ms) >= self.default_run_ms
        else:
            self._run_timer_start_ms = None
            run_expired = False

        x_spa_enable = x_spa_enable_cmd and (not run_expired)

        if x_light_request:
            if self._light_timer_start_ms is None:
                self._light_timer_start_ms = now
            light_expired = ticks_diff(now, self._light_timer_start_ms) >= self.light_run_ms
        else:
            self._light_timer_start_ms = None
            light_expired = False

        x_permissive = (
            x_spa_enable
            and x_high_limit_ok
            and x_remote_estop_ok
            and (r_water_temp_f <= self.max_safe_temp_f)
        )

        # Pump 1 two-speed interlock: high overrides low.
        x_pump1_high = x_permissive and x_pump1_high_request
        x_pump1_low = x_permissive and (x_pump_request or x_heat_request) and (not x_pump1_high)

        # Single-speed pumps
        x_pump2 = x_permissive and x_pump2_request
        x_pump3 = x_permissive and x_pump3_request
        x_any_pump = x_pump1_low or x_pump1_high or x_pump2 or x_pump3

        flow_proven = self._flow_prove.update(x_any_pump and x_flow_switch)
        min_run_elapsed = self._pump_min_run.update(x_any_pump)

        x_heater = self._thermostat.update(
            enable=(x_permissive and x_heat_request),
            temp_f=r_water_temp_f,
            setpoint_f=self.temp_setpoint_f,
            hysteresis_f=self.temp_hysteresis_f,
            flow_ok=x_flow_switch,
            high_limit_ok=x_high_limit_ok,
            flow_proven=flow_proven,
            min_on_time_elapsed=min_run_elapsed,
        )

        x_jets = x_permissive and x_jets_request and x_any_pump
        x_blower = x_permissive and x_blower_request
        x_light = x_light_request and (not light_expired)

        # Fault priority
        self.x_fault = False
        self.i_fault_code = 0

        if x_spa_enable and (not x_remote_estop_ok):
            self.x_fault = True
            self.i_fault_code = 4
        elif x_spa_enable and (not x_high_limit_ok):
            self.x_fault = True
            self.i_fault_code = 2
        elif x_spa_enable and (r_water_temp_f > self.max_safe_temp_f):
            self.x_fault = True
            self.i_fault_code = 3
        elif x_any_pump and flow_proven and (not x_flow_switch):
            self.x_fault = True
            self.i_fault_code = 1

        if self.x_fault:
            x_pump1_low = False
            x_pump1_high = False
            x_pump2 = False
            x_pump3 = False
            x_heater = False
            x_jets = False
            x_blower = False

        return {
            "xPump1_Low": x_pump1_low,
            "xPump1_High": x_pump1_high,
            "xPump2": x_pump2,
            "xPump3": x_pump3,
            "xHeater": x_heater,
            "xJets": x_jets,
            "xBlower": x_blower,
            "xLight": x_light,
            "xFault": self.x_fault,
            "iFaultCode": self.i_fault_code,
        }


# ------------------- ESP32-S3-DevKitC-1-N8R8 pin map -------------------
# Notes:
# - Avoid GPIO0 for normal controls (boot strap).
# - Avoid GPIO19/GPIO20 (USB D-/D+) unless USB is not used.
# - Avoid GPIO45/GPIO46 for outputs (input-only/strap related on ESP32-S3).
# - GPIO26..GPIO37 are commonly tied to module flash/PSRAM and not for user I/O.
#
# These assignments are chosen from generally usable DevKitC-1 exposed GPIOs.
INPUT_PINS = {
    "xSpaEnable": 4,
    "xPumpRequest": 5,
    "xHeatRequest": 6,
    "xPump1HighRequest": 7,
    "xPump2Request": 15,
    "xPump3Request": 16,
    "xJetsRequest": 17,
    "xBlowerRequest": 18,
    "xLightRequest": 8,
    "xFlowSwitch": 9,
    "xHighLimitOK": 10,
    "xRemoteEStopOK": 11,
}

OUTPUT_PINS = {
    "xPump1_Low": 12,
    "xPump1_High": 13,
    "xPump2": 14,
    "xPump3": 21,
    "xHeater": 38,
    "xJets": 39,
    "xBlower": 40,
    "xLight": 41,
}

IN = {name: Pin(gpio, Pin.IN, Pin.PULL_UP) for name, gpio in INPUT_PINS.items()}
OUT = {name: Pin(gpio, Pin.OUT, value=0) for name, gpio in OUTPUT_PINS.items()}

# ------------------- Hosyond 4.0" ST7796S + XPT2046 -------------------
# Display SPI bus (shared with touch):
#   SCK=GPIO42, MOSI=GPIO47, MISO=GPIO48
# LCD control:
#   CS=GPIO2, DC=GPIO1, RST=GPIO3, BL=GPIO44
# Touch control:
#   CS=GPIO43, IRQ=GPIO45 (GPIO45 is input-only on ESP32-S3, good for IRQ)
DISPLAY_PINS = {
    "SCK": 42,
    "MOSI": 47,
    "MISO": 48,
    "LCD_CS": 2,
    "LCD_DC": 1,
    "LCD_RST": 3,
    "LCD_BL": 44,
    "TOUCH_CS": 43,
    "TOUCH_IRQ": 45,
}

# Hosyond 4.0" ST7796S SPI modules: LED/BL is active-high (3.3 V on = backlight on).
DISPLAY_BL_ACTIVE_LOW = False
DISPLAY_SPI_BAUDRATE = 40_000_000
TOUCH_SPI_BAUDRATE = 2_500_000
CONTROL_LOOP_MS = 50
# Hosyond 4.0" ST7796S + XPT2046 touch mapping for rotation=1 (480x320).
# Calibration derived from empirical tap data on the confirmed-correct 480x320 display.
# X: raw_y spans 82-1966 across physical x=0-479 (GET_Y channel measures horizontal).
# Y: raw_x spans 0-1993 across physical y=0-319 (GET_X channel measures vertical).
#    invert_y=False because raw_x increases as screen y increases (top→bottom).
TOUCH_RAW = {
    "x_min": 82,
    "x_max": 1966,
    "y_min": 0,
    "y_max": 1993,
}
TOUCH_SWAP_XY = True
TOUCH_INVERT_X = False
TOUCH_INVERT_Y = False
TOUCH_HIT_MARGIN = 16
TOUCH_SCREEN_OFFSET_X = 0
TOUCH_SCREEN_OFFSET_Y = 0
TOUCH_SCREEN_SCALE_X  = 1.0
TOUCH_SCREEN_SCALE_Y  = 1.0
TOUCH_DEBUG = False
TOUCH_REPEAT_MS = 300
TOUCH_SETPOINT_REPEAT_MS = 150
# ── HMI colour palette (RGB565) ──────────────────────────────────────────────
C_BG       = 0x0820   # deep navy background
C_PANEL    = 0x1082   # dark panel fill
C_HDR_BG   = 0x0438   # header bar (very dark blue)
C_BORDER   = 0x2965   # panel border / divider
C_ACCENT   = 0x07FF   # cyan accent strip
C_TEXT     = 0xFFFF   # primary white text
C_LABEL    = 0xC618   # secondary grey label
C_DIM      = 0x4208   # dimmed / ghost
C_SEG_ON   = 0x07FF   # 7-seg cyan – water temperature reading
C_SP_ON    = 0x07E0   # 7-seg green – setpoint
C_LED_GN   = 0x07E0   # LED green  (heat requested / pump on / light on)
C_LED_AM   = 0xFD20   # LED amber  (heater element energised)
C_LED_YE   = 0xFFE0   # LED yellow (light on)
C_LED_CY   = 0x07FF   # LED cyan   (pump running)
C_LED_OFF  = 0x2104   # LED inactive dark
C_BTN_NORM = 0x2965   # button idle background
C_BTN_P_AC = 0x0099   # pump button active (dark cyan)
C_BTN_H_AC = 0x6200   # heat button active (dark amber)
C_BTN_L_AC = 0x4420   # light button active (dark yellow-green)
C_FAULT    = 0xF800   # fault red

# ── Panel geometry (landscape 480 × 320) ─────────────────────────────────────
_HDR_H = 28
_PNL_Y = 28
_PNL_H = 292   # 320 - 28
_PNL_S_X, _PNL_S_W = 0,   88    # status panel
_PNL_T_X, _PNL_T_W = 88,  194   # temperature panel
_PNL_C_X, _PNL_C_W = 282, 198   # controls panel

# ── 7-segment digit dimensions (24 × 41 px, pitch 28) ────────────────────────
_DIG_W, _DIG_H, _DIG_P = 24, 41, 28
# bit0=a(top) 1=b(TR) 2=c(BR) 3=d(bot) 4=e(BL) 5=f(TL) 6=g(mid)
_SEG_MASKS = (63, 6, 91, 79, 102, 109, 125, 7, 127, 111)

# Big-digit origins (centred inside 194-px temperature panel)
_BIG_TEMP_X = 117
_BIG_TEMP_Y = 64
_BIG_SP_X   = 117
_BIG_SP_Y   = 148

TIMER_LABEL_WIDTH = 186
TIMER_SPA_POS     = (288, 243)
TIMER_LIGHT_POS   = (288, 261)

UI_LIMITS = {
    "SETPOINT_MIN_F": 80.0,
    "SETPOINT_MAX_F": 104.0,
    "SETPOINT_STEP_F": 1.0,
}

# ── Touch button rects (x, y, w, h) ──────────────────────────────────────────
# Right-panel vertical layout (y from top):
#   y=64   PUMP 1  [OFF][LOW][HI]    h=32
#   y=120  PUMP 2/3  [PUMP 2][PUMP 3]   h=32
#   y=173  HEAT/LIGHT  [HEAT][LIGHT]   h=44
#   y=221  divider → RUN TIMERS
UI_BUTTONS = {
    # Temperature panel – setpoint adjust
    "setpoint_minus": (98,  210, 78, 48),
    "setpoint_plus":  (194, 210, 78, 48),
    # Controls panel – PUMP 1 (three-state)
    "pump_off":  (286,  64, 58, 32),
    "pump_low":  (350,  64, 58, 32),
    "pump_high": (414,  64, 56, 32),
    # Controls panel – PUMP 2 & 3 (toggles)
    "pump2": (286, 120, 89, 32),
    "pump3": (383, 120, 89, 32),
    # Controls panel – heat & light
    "heat":  (286, 173, 89, 44),
    "light": (383, 173, 89, 44),
}

TOUCH_BUTTON_ORDER = (
    "heat",
    "pump_off",
    "pump_low",
    "pump_high",
    "pump2",
    "pump3",
    "light",
    "setpoint_minus",
    "setpoint_plus",
)

_hmi_lcd_cs = None


def _log_hmi(message):
    try:
        print(message)
    except Exception:
        pass


def _report_hmi_error(label, err):
    _log_hmi("%s: %s" % (label, err))
    try:
        import sys

        sys.print_exception(err)
    except Exception:
        pass


def init_hmi():
    """
    Initialize Hosyond ST7796S LCD and XPT2046 touch if drivers exist.
    Returns (display, touch). Either value may be None.
    """
    global _hmi_lcd_cs
    spi = SPI(
        1,
        baudrate=DISPLAY_SPI_BAUDRATE,
        polarity=0,
        phase=0,
        sck=Pin(DISPLAY_PINS["SCK"]),
        mosi=Pin(DISPLAY_PINS["MOSI"]),
        miso=Pin(DISPLAY_PINS["MISO"]),
    )

    lcd = None
    touch = None

    lcd_cs = Pin(DISPLAY_PINS["LCD_CS"], Pin.OUT, value=1)
    _hmi_lcd_cs = lcd_cs
    lcd_dc = Pin(DISPLAY_PINS["LCD_DC"], Pin.OUT, value=1)
    lcd_rst = Pin(DISPLAY_PINS["LCD_RST"], Pin.OUT, value=1)
    lcd_bl = Pin(DISPLAY_PINS["LCD_BL"], Pin.OUT, value=0 if DISPLAY_BL_ACTIVE_LOW else 1)
    lcd_bl.value(0 if DISPLAY_BL_ACTIVE_LOW else 1)

    try:
        import st7796  # type: ignore

        st7796_cls = getattr(st7796, "ST7796", None)
        if st7796_cls is None:
            _log_hmi("HMI: st7796.ST7796 not found")
        else:
            try:
                lcd = st7796_cls(
                    spi,
                    320,
                    480,
                    reset=lcd_rst,
                    cs=lcd_cs,
                    dc=lcd_dc,
                    rotation=1,
                )
                _log_hmi("HMI: ST7796 init ok (%sx%s)" % (lcd.width, lcd.height))
            except Exception as err:
                _report_hmi_error("HMI: ST7796 init failed", err)
                lcd = None
    except Exception as err:
        _report_hmi_error("HMI: st7796 import failed", err)
        lcd = None

    try:
        import xpt2046  # type: ignore

        tcs = Pin(DISPLAY_PINS["TOUCH_CS"], Pin.OUT, value=1)
        tirq = Pin(DISPLAY_PINS["TOUCH_IRQ"], Pin.IN, Pin.PULL_UP)
        touch_cls = getattr(xpt2046, "Touch", None)
        if touch_cls is not None:
            touch = touch_cls(
                spi,
                tcs,
                int_pin=tirq,
                width=480,
                height=320,
                x_min=TOUCH_RAW["x_min"],
                x_max=TOUCH_RAW["x_max"],
                y_min=TOUCH_RAW["y_min"],
                y_max=TOUCH_RAW["y_max"],
                swap_xy=TOUCH_SWAP_XY,
                invert_x=TOUCH_INVERT_X,
                invert_y=TOUCH_INVERT_Y,
                z_threshold=40,
                touch_baudrate=TOUCH_SPI_BAUDRATE,
                display_baudrate=DISPLAY_SPI_BAUDRATE,
            )
            _log_hmi("HMI: touch init ok")
        else:
            touch_cls = getattr(xpt2046, "XPT2046", None)
            if touch_cls is not None:
                touch = touch_cls(spi=spi, cs=tcs, int_pin=tirq)
                _log_hmi("HMI: touch init ok")
            else:
                _log_hmi("HMI: touch driver class not found")
    except Exception as err:
        _report_hmi_error("HMI: touch init failed", err)
        touch = None

    return lcd, touch


# ── 7-segment & big-digit helpers ────────────────────────────────────────────

def _seg_digit(lcd, d, x, y, fg, bg):
    """Draw one 24 × 41 px 7-segment digit.  d=0-9; anything else = blank."""
    lcd.fill_rect(x, y, _DIG_W, _DIG_H, bg)
    if not (0 <= d <= 9):
        return
    m = _SEG_MASKS[d]
    if m & 1:   lcd.fill_rect(x + 4,  y,       16, 4,  fg)  # a  top
    if m & 32:  lcd.fill_rect(x,       y + 5,   4,  14, fg)  # f  top-left
    if m & 2:   lcd.fill_rect(x + 20,  y + 5,   4,  14, fg)  # b  top-right
    if m & 64:  lcd.fill_rect(x + 4,   y + 19,  16, 4,  fg)  # g  middle
    if m & 16:  lcd.fill_rect(x,        y + 23,  4,  14, fg)  # e  bot-left
    if m & 4:   lcd.fill_rect(x + 20,   y + 23,  4,  14, fg)  # c  bot-right
    if m & 8:   lcd.fill_rect(x + 4,    y + 37,  16, 4,  fg)  # d  bottom


def _draw_temp_big(lcd, temp_f, x, y, fg, bg):
    """
    Render a temperature value in large 7-segment style.
    Layout: [hundreds] [tens] [ones] · [tenths]  °F
    Always clears a fixed-width strip so stale pixels cannot linger.
    temp_f: float, or None to show dashes.
    """
    lcd.fill_rect(x, y, 4 * _DIG_P + 40, _DIG_H, bg)

    if temp_f is None or temp_f < -50:
        for i in range(4):
            lcd.fill_rect(x + i * _DIG_P + 4, y + 19, 16, 4, fg)
        lcd.text("F", x + 4 * _DIG_P + 16, y + 10, C_LABEL)
        return

    val = int(round(abs(temp_f) * 10))
    val = min(val, 1999)
    d3 = (val // 1000) % 10
    d2 = (val // 100) % 10
    d1 = (val // 10) % 10
    d0 = val % 10

    if val >= 1000:
        _seg_digit(lcd, d3, x, y, fg, bg)
    _seg_digit(lcd, d2, x + _DIG_P, y, fg, bg)
    _seg_digit(lcd, d1, x + 2 * _DIG_P, y, fg, bg)

    # Decimal dot (4 × 4 px square at bottom-right of the ones position)
    lcd.fill_rect(x + 3 * _DIG_P - 4, y + 37, 4, 4, fg)

    # Tenths digit (offset 4 px past dot)
    _seg_digit(lcd, d0, x + 3 * _DIG_P + 4, y, fg, bg)

    # Degree-F symbol: small hollow ring then "F"
    rx = x + 4 * _DIG_P + 8
    lcd.fill_rect(rx, y + 6, 5, 5, fg)
    lcd.fill_rect(rx + 1, y + 7, 3, 3, bg)
    lcd.text("F", rx + 6, y + 10, C_LABEL)


# ── UI widget helpers ─────────────────────────────────────────────────────────

def _draw_led(lcd, x, y, color):
    """12 × 12 bordered LED indicator square."""
    lcd.fill_rect(x, y, 12, 12, C_BORDER)
    lcd.fill_rect(x + 2, y + 2, 8, 8, color)


def _draw_button_v2(lcd, rect, label, active=False, act_color=0x0492):
    """Bevelled button with centred label."""
    x, y, w, h = rect
    bg = act_color if active else C_BTN_NORM
    lcd.fill_rect(x, y, w, h, bg)
    hi = C_ACCENT if active else C_BORDER
    lcd.fill_rect(x, y, w, 1, hi)           # top highlight
    lcd.fill_rect(x, y, 1, h, hi)           # left highlight
    lcd.fill_rect(x, y + h - 1, w, 1, C_DIM)   # bottom shadow
    lcd.fill_rect(x + w - 1, y, 1, h, C_DIM)   # right shadow
    lx = x + (w - len(label) * 8) // 2
    ly = y + (h - 8) // 2
    lcd.text(label, lx, ly, C_TEXT)


def _draw_static_frame(lcd):
    """
    Paint all fixed chrome once: header bar, panel fills, borders, section
    labels, and static button outlines.  Dynamic fields are layered on top.
    """
    # Header bar
    lcd.fill_rect(0, 0, 480, _HDR_H, C_HDR_BG)
    lcd.fill_rect(0, _HDR_H - 1, 480, 1, C_ACCENT)
    lcd.text("SPA CONTROL", 8, 10, C_TEXT)

    # Panel fills
    lcd.fill_rect(_PNL_S_X, _PNL_Y, _PNL_S_W, _PNL_H, C_PANEL)
    lcd.fill_rect(_PNL_T_X, _PNL_Y, _PNL_T_W, _PNL_H, C_BG)
    lcd.fill_rect(_PNL_C_X, _PNL_Y, _PNL_C_W, _PNL_H, C_PANEL)

    # Vertical panel dividers
    lcd.fill_rect(_PNL_S_X + _PNL_S_W, _PNL_Y, 2, _PNL_H, C_BORDER)
    lcd.fill_rect(_PNL_T_X + _PNL_T_W, _PNL_Y, 2, _PNL_H, C_BORDER)

    # Status panel
    lcd.text("STATUS", 20, 36, C_LABEL)
    lcd.fill_rect(0, 56, _PNL_S_W, 1, C_BORDER)
    lcd.text("HEAT",  26, 68,  C_LABEL)
    lcd.text("PUMP",  26, 100, C_LABEL)
    lcd.text("LITE",  26, 132, C_LABEL)
    lcd.text("FAULT", 26, 164, C_LABEL)

    # Temperature panel
    lcd.text("WATER TEMP", 145, 36, C_LABEL)
    lcd.fill_rect(_PNL_T_X, 56,  _PNL_T_W, 1, C_BORDER)
    lcd.fill_rect(_PNL_T_X, 125, _PNL_T_W, 1, C_BORDER)
    lcd.text("SETPOINT", 153, 132, C_LABEL)
    lcd.fill_rect(_PNL_T_X, 200, _PNL_T_W, 1, C_BORDER)
    _draw_button_v2(lcd, UI_BUTTONS["setpoint_minus"], "  -  ")
    _draw_button_v2(lcd, UI_BUTTONS["setpoint_plus"],  "  +  ")
    lcd.text("RANGE: 80 - 104 F", 108, 273, C_DIM)

    # Controls panel – PUMP 1 section
    lcd.text("PUMP 1", 357, 36, C_LABEL)
    lcd.fill_rect(_PNL_C_X, 56,  _PNL_C_W, 1, C_BORDER)   # top divider

    # Controls panel – PUMP 2 / 3 section
    lcd.fill_rect(_PNL_C_X, 100, _PNL_C_W, 1, C_BORDER)
    lcd.text("PUMP 2 / 3", 341, 108, C_LABEL)

    # Controls panel – HEAT / LIGHT section
    lcd.fill_rect(_PNL_C_X, 156, _PNL_C_W, 1, C_BORDER)
    lcd.text("HEAT / LIGHT", 333, 164, C_LABEL)

    # Controls panel – RUN TIMERS section
    lcd.fill_rect(_PNL_C_X, 221, _PNL_C_W, 1, C_BORDER)
    lcd.text("RUN TIMERS", 341, 229, C_LABEL)


def _touch_point(touch):
    if touch is None:
        return None

    if _hmi_lcd_cs is not None:
        _hmi_lcd_cs.value(1)

    try:
        if hasattr(touch, "poll"):
            point = touch.poll()
        elif hasattr(touch, "raw_touch"):
            sample = touch.raw_touch()
            if sample is None:
                return None
            if hasattr(touch, "normalize"):
                point = touch.normalize(sample[0], sample[1])
            else:
                point = sample
        elif hasattr(touch, "read"):
            point = touch.read()
        elif hasattr(touch, "get_point"):
            point = touch.get_point()
        else:
            point = None
    except Exception:
        point = None

    if not point:
        return None

    if isinstance(point, (tuple, list)) and len(point) >= 2:
        mapped = _map_touch_point(int(point[0]), int(point[1]))
        if TOUCH_DEBUG:
            print("TOUCH raw=(%d,%d) mapped=(%d,%d)" % (int(point[0]), int(point[1]), mapped[0], mapped[1]))
        return mapped
    return None


def _map_touch_point(x, y):
    x = int((x - TOUCH_SCREEN_OFFSET_X) * TOUCH_SCREEN_SCALE_X)
    y = int((y - TOUCH_SCREEN_OFFSET_Y) * TOUCH_SCREEN_SCALE_Y)
    if x < 0:
        x = 0
    elif x > 479:
        x = 479
    if y < 0:
        y = 0
    elif y > 319:
        y = 319
    return x, y


def _hit_button(x, y, rect, margin=0):
    bx, by, bw, bh = rect
    return (bx - margin) <= x < (bx + bw + margin) and (by - margin) <= y < (by + bh + margin)


def _touch_button_at(x, y):
    for name in TOUCH_BUTTON_ORDER:
        if _hit_button(x, y, UI_BUTTONS[name], TOUCH_HIT_MARGIN):
            return name
    return None


def _update_setpoint_display(lcd, ctrl):
    if lcd is None:
        return
    _draw_temp_big(lcd, ctrl.temp_setpoint_f, _BIG_SP_X, _BIG_SP_Y, C_SP_ON, C_BG)


def _remaining_seconds(active, start_ms, duration_ms, now_ms):
    if (not active) or (start_ms is None):
        return 0
    elapsed = ticks_diff(now_ms, start_ms)
    if elapsed < 0:
        elapsed = 0
    remain = duration_ms - elapsed
    if remain <= 0:
        return 0
    return int(remain / 1000)


def update_touch_ui(touch, ui_state, ctrl, now_ms, lcd=None):
    """
    Handle tap actions:
    - Heat toggle
    - Pump 1 mode (off/low/high)
    - Pump 2 / Pump 3 toggle
    - Light toggle
    - Setpoint +/- (F)
    """
    point = _touch_point(touch)
    if point is None:
        ui_state["touch_button"] = None
        return

    x, y = point
    button = _touch_button_at(x, y)
    if TOUCH_DEBUG:
        print("HIT x=%d y=%d -> %s" % (x, y, button if button else "MISS"))
    if button is None:
        ui_state["touch_button"] = None
        return

    repeat_ms = (
        TOUCH_SETPOINT_REPEAT_MS
        if button in ("setpoint_minus", "setpoint_plus")
        else TOUCH_REPEAT_MS
    )
    if button == ui_state.get("touch_button"):
        if ticks_diff(now_ms, ui_state.get("last_touch_ms", 0)) < repeat_ms:
            return
    else:
        ui_state["touch_button"] = button

    handled = False
    if button == "heat":
        ui_state["xHeatRequest"] = not ui_state["xHeatRequest"]
        handled = True
    elif button == "pump_off":
        if ui_state["pump1_mode"] != 0:
            ui_state["pump1_mode"] = 0
            handled = True
    elif button == "pump_low":
        if ui_state["pump1_mode"] != 1:
            ui_state["pump1_mode"] = 1
            handled = True
    elif button == "pump_high":
        if ui_state["pump1_mode"] != 2:
            ui_state["pump1_mode"] = 2
            handled = True
    elif button == "pump2":
        ui_state["pump2_on"] = not ui_state.get("pump2_on", False)
        handled = True
    elif button == "pump3":
        ui_state["pump3_on"] = not ui_state.get("pump3_on", False)
        handled = True
    elif button == "light":
        ui_state["xLightRequest"] = not ui_state["xLightRequest"]
        handled = True
    elif button == "setpoint_minus":
        new_setpoint_f = max(
            UI_LIMITS["SETPOINT_MIN_F"],
            ctrl.temp_setpoint_f - UI_LIMITS["SETPOINT_STEP_F"],
        )
        if new_setpoint_f != ctrl.temp_setpoint_f:
            ctrl.temp_setpoint_f = new_setpoint_f
            _update_setpoint_display(lcd, ctrl)
            handled = True
    elif button == "setpoint_plus":
        new_setpoint_f = min(
            UI_LIMITS["SETPOINT_MAX_F"],
            ctrl.temp_setpoint_f + UI_LIMITS["SETPOINT_STEP_F"],
        )
        if new_setpoint_f != ctrl.temp_setpoint_f:
            ctrl.temp_setpoint_f = new_setpoint_f
            _update_setpoint_display(lcd, ctrl)
            handled = True

    ui_state["last_touch_ms"] = now_ms
    if handled:
        ui_state["_dynamic_key"] = None


def apply_ui_overrides(inputs, ui_state):
    values = dict(inputs)
    values["xHeatRequest"] = ui_state["xHeatRequest"]
    values["xLightRequest"] = ui_state["xLightRequest"]
    values["xPump1HighRequest"] = ui_state["pump1_mode"] == 2
    values["xPumpRequest"] = ui_state["pump1_mode"] in (1, 2)
    values["xPump2Request"] = bool(ui_state.get("pump2_on", False))
    values["xPump3Request"] = bool(ui_state.get("pump3_on", False))
    return values


def _timer_remain_seconds(inputs, ctrl, ui_state, now_ms):
    spa_remain_s = _remaining_seconds(
        bool(inputs.get("xSpaEnable", False)),
        ctrl._run_timer_start_ms,
        ctrl.default_run_ms,
        now_ms,
    )
    light_remain_s = _remaining_seconds(
        ui_state["xLightRequest"],
        ctrl._light_timer_start_ms,
        ctrl.light_run_ms,
        now_ms,
    )
    return spa_remain_s, light_remain_s


def _dynamic_snapshot(inputs, outputs, ctrl, ui_state):
    return (
        round(inputs.get("rWaterTemp_F", 0.0), 1),
        round(ctrl.temp_setpoint_f, 1),
        bool(outputs.get("xHeater")),
        ui_state["pump1_mode"],
        bool(ui_state["xHeatRequest"]),
        bool(ui_state["xLightRequest"]),
        bool(ui_state.get("pump2_on", False)),
        bool(ui_state.get("pump3_on", False)),
        int(bool(outputs.get("xPump1_Low"))),
        int(bool(outputs.get("xPump1_High"))),
        int(bool(outputs.get("xPump2"))),
        int(bool(outputs.get("xPump3"))),
        int(outputs.get("iFaultCode", 0)),
        bool(outputs.get("xFault")),
    )


def _fmt_timer(s):
    if s <= 0:
        return "--:--"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, sec)
    return "%02d:%02d" % (m, sec)


def _update_timer_display(lcd, inputs, ctrl, ui_state, now_ms):
    if lcd is None:
        return

    spa_remain_s, light_remain_s = _timer_remain_seconds(inputs, ctrl, ui_state, now_ms)
    timer_key = (spa_remain_s, light_remain_s)
    if timer_key == ui_state.get("_timer_key"):
        return

    ui_state["_timer_key"] = timer_key
    spa_x, spa_y = TIMER_SPA_POS
    light_x, light_y = TIMER_LIGHT_POS

    lcd.fill_rect(spa_x, spa_y - 1, TIMER_LABEL_WIDTH, 10, C_PANEL)
    lcd.text("SPA  " + _fmt_timer(spa_remain_s), spa_x, spa_y,
             C_TEXT if spa_remain_s > 0 else C_DIM)

    lcd.fill_rect(light_x, light_y - 1, TIMER_LABEL_WIDTH, 10, C_PANEL)
    lcd.text("LGT  " + _fmt_timer(light_remain_s), light_x, light_y,
             C_LED_YE if light_remain_s > 0 else C_DIM)


def _render_dynamic_fields(lcd, inputs, outputs, ctrl, ui_state):
    if lcd is None:
        return

    heat_req  = ui_state["xHeatRequest"]
    light_req = ui_state["xLightRequest"]
    pump_mode = ui_state["pump1_mode"]
    pump2_on  = bool(ui_state.get("pump2_on", False))
    pump3_on  = bool(ui_state.get("pump3_on", False))
    heater_on = bool(outputs.get("xHeater"))
    pump_on   = pump_mode > 0 or pump2_on or pump3_on
    fault     = bool(outputs.get("xFault"))
    fc        = int(outputs.get("iFaultCode", 0))

    # ── Temperature panel: big PV and SP displays ────────────────────────────
    _draw_temp_big(lcd, inputs.get("rWaterTemp_F"), _BIG_TEMP_X, _BIG_TEMP_Y,
                   C_SEG_ON, C_BG)
    _draw_temp_big(lcd, ctrl.temp_setpoint_f, _BIG_SP_X, _BIG_SP_Y,
                   C_SP_ON, C_BG)

    # ── Status panel: LED indicators ─────────────────────────────────────────
    heat_led = C_LED_AM if heater_on else (C_LED_GN if heat_req else C_LED_OFF)
    _draw_led(lcd, 10, 64, heat_led)
    _draw_led(lcd, 10, 96,  C_LED_CY if pump_on   else C_LED_OFF)
    _draw_led(lcd, 10, 128, C_LED_YE if light_req else C_LED_OFF)
    _draw_led(lcd, 10, 160, C_FAULT  if fault     else C_LED_OFF)

    # Fault / heater status text
    lcd.fill_rect(4, 180, 80, 10, C_PANEL)
    lcd.text("FC:%d" % fc if fc else "OK", 4, 182,
             C_FAULT if fault else C_LED_GN)
    lcd.fill_rect(4, 198, 80, 10, C_PANEL)
    lcd.text("HTR:%s" % ("ON" if heater_on else "OFF"), 4, 200,
             C_LED_AM if heater_on else C_DIM)

    # ── Controls panel: PUMP 1 buttons ───────────────────────────────────────
    _draw_button_v2(lcd, UI_BUTTONS["pump_off"],  "OFF",
                    active=(pump_mode == 0), act_color=C_BTN_P_AC)
    _draw_button_v2(lcd, UI_BUTTONS["pump_low"],  "LOW",
                    active=(pump_mode == 1), act_color=C_BTN_P_AC)
    _draw_button_v2(lcd, UI_BUTTONS["pump_high"], "HI",
                    active=(pump_mode == 2), act_color=C_BTN_P_AC)

    # ── Controls panel: PUMP 2 & 3 toggles ───────────────────────────────────
    _draw_button_v2(lcd, UI_BUTTONS["pump2"], "PUMP 2",
                    active=pump2_on, act_color=C_BTN_P_AC)
    _draw_button_v2(lcd, UI_BUTTONS["pump3"], "PUMP 3",
                    active=pump3_on, act_color=C_BTN_P_AC)

    # ── Controls panel: heat & light buttons ─────────────────────────────────
    _draw_button_v2(lcd, UI_BUTTONS["heat"],  "HEAT",
                    active=heat_req,  act_color=C_BTN_H_AC)
    _draw_button_v2(lcd, UI_BUTTONS["light"], "LIGHT",
                    active=light_req, act_color=C_BTN_L_AC)


def render_hmi(lcd, inputs, outputs, ctrl, ui_state, full=False):
    """Enhanced industrial HMI render. Safe no-op when driver is unavailable."""
    if lcd is None:
        return

    try:
        if full or not ui_state.get("_hmi_initialized", False):
            lcd.fill(C_BG)
            _draw_static_frame(lcd)
            ui_state["_hmi_initialized"] = True
        _render_dynamic_fields(lcd, inputs, outputs, ctrl, ui_state)
        _update_timer_display(lcd, inputs, ctrl, ui_state, ticks_ms())
        if hasattr(lcd, "show"):
            lcd.show()
    except Exception as err:
        if not ui_state.get("_render_error_logged", False):
            ui_state["_render_error_logged"] = True
            _report_hmi_error("HMI: render failed", err)


def read_water_temp_f():
    """
    Replace with your ADC + sensor conversion to Fahrenheit.
    """
    return 95.0


def read_inputs():
    values = {name: bool(pin.value()) for name, pin in IN.items()}
    values["rWaterTemp_F"] = read_water_temp_f()
    return values


def write_outputs(values):
    for name, pin in OUT.items():
        pin.value(1 if values.get(name, False) else 0)


def main(loop_ms=CONTROL_LOOP_MS):
    try:
        import gc

        gc.collect()
        _log_hmi("boot free mem: %s" % gc.mem_free())
    except Exception:
        pass

    ctrl = SpaController()
    lcd, touch = init_hmi()
    if lcd is not None:
        try:
            lcd.fill(0xF800)
            if hasattr(lcd, "show"):
                lcd.show()
            if hasattr(time_mod, "sleep_ms"):
                time_mod.sleep_ms(400)
            else:
                time_mod.sleep(0.4)
        except Exception as err:
            _report_hmi_error("HMI: splash failed", err)
    ui_state = {
        "xHeatRequest": False,
        "xLightRequest": False,
        "pump1_mode": 1,   # 0=off, 1=low, 2=high
        "pump2_on": False,
        "pump3_on": False,
        "touch_button": None,
        "last_touch_ms": 0,
        "_render_error_logged": False,
        "_hmi_initialized": False,
        "_dynamic_key": None,
        "_timer_key": None,
    }
    raw_inputs = read_inputs()
    inputs = apply_ui_overrides(raw_inputs, ui_state)
    outputs = ctrl.step(inputs)
    render_hmi(lcd, inputs, outputs, ctrl, ui_state, full=True)
    ui_state["_dynamic_key"] = _dynamic_snapshot(inputs, outputs, ctrl, ui_state)

    _last_touch_display = None

    while True:
        raw_inputs = read_inputs()
        now = ticks_ms()
        update_touch_ui(touch, ui_state, ctrl, now, lcd)
        inputs = apply_ui_overrides(raw_inputs, ui_state)
        outputs = ctrl.step(inputs)
        write_outputs(outputs)
        _update_timer_display(lcd, inputs, ctrl, ui_state, now)
        dynamic_key = _dynamic_snapshot(inputs, outputs, ctrl, ui_state)
        if dynamic_key != ui_state["_dynamic_key"]:
            ui_state["_dynamic_key"] = dynamic_key
            _render_dynamic_fields(lcd, inputs, outputs, ctrl, ui_state)
        if TOUCH_DEBUG and lcd is not None:
            tp = _touch_point(touch)
            if tp is not None:
                btn = _touch_button_at(tp[0], tp[1])
                label = "x=%-3d y=%-3d %-14s" % (tp[0], tp[1], btn if btn else "MISS")
                if label != _last_touch_display:
                    _last_touch_display = label
                    if hasattr(lcd, "fill_rect"):
                        lcd.fill_rect(0, 152, 480, 12, 0x0010)
                    if hasattr(lcd, "text"):
                        lcd.text(label, 4, 154, 0xFFE0)
        if hasattr(time_mod, "sleep_ms"):
            time_mod.sleep_ms(loop_ms)
        else:
            time_mod.sleep(loop_ms / 1000.0)


if __name__ == "__main__":
    main()
