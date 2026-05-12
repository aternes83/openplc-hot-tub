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
    from machine import Pin, SPI, PWM
except ImportError:  # local testing stub
    class PWM:  # type: ignore
        def __init__(self, pin, freq=1000, duty_u16=65535):
            pass

        def duty_u16(self, val=None):
            pass
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


class RollingAverage:
    """Fixed-length circular-buffer rolling average.  O(1) update, no heap churn."""

    def __init__(self, n):
        self._buf   = [0.0] * n
        self._n     = n
        self._idx   = 0
        self._count = 0
        self._total = 0.0

    def update(self, value):
        v = float(value)
        if self._count < self._n:
            self._count += 1
        else:
            self._total -= self._buf[self._idx]
        self._buf[self._idx] = v
        self._total += v
        self._idx = (self._idx + 1) % self._n
        return self._total / self._count


# ── Backlight PWM handle (set by init_hmi) ───────────────────────────────────
_hmi_bl_pwm = None


def _set_backlight(duty_pct):
    """Set backlight brightness 0–100 %.  No-op when PWM not available."""
    if _hmi_bl_pwm is not None:
        _hmi_bl_pwm.duty_u16(max(0, min(65535, duty_pct * 655)))


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
        self.temp_hysteresis_f = 0.5
        self.max_safe_temp_f = 105.0
        self.flow_prove_ms = 5_000
        self.pump_preheat_ms = 5_000
        self.default_run_ms = 4 * 60 * 60 * 1000  # 4-hour safety ceiling; resets on any HMI press
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

        # ── Two-tier permissive ───────────────────────────────────────────────
        # Freeze-protection permissive: safety interlocks only, NO run timer.
        # This keeps the thermostat and its circulation pump alive indefinitely
        # so the setpoint is always maintained (critical for winter freeze protection).
        x_freeze_permissive = (
            x_high_limit_ok
            and x_remote_estop_ok
            and (r_water_temp_f <= self.max_safe_temp_f)
        )
        # Full permissive: additionally requires spa-enable + run timer not expired.
        # Gates user-initiated jets, high-speed pump, blower, etc.
        x_permissive = x_spa_enable and x_freeze_permissive

        # Auto-heat: activate whenever water is below setpoint.
        # x_heat_request is kept for compatibility but no longer gates this path.
        x_heat_active = r_water_temp_f < self.temp_setpoint_f

        # Pump 1: heat-driven circulation uses freeze permissive (always on when
        # needed); user-requested low/high speed requires full permissive.
        x_pump1_high = x_permissive and x_pump1_high_request
        x_pump1_low = (
            (x_freeze_permissive and x_heat_active)   # thermostat circulation
            or (x_permissive and x_pump_request)      # manual LOW request
        ) and (not x_pump1_high)

        # Single-speed pumps – require full permissive (user-initiated only).
        x_pump2 = x_permissive and x_pump2_request
        x_pump3 = x_permissive and x_pump3_request
        x_any_pump = x_pump1_low or x_pump1_high or x_pump2 or x_pump3

        flow_proven = self._flow_prove.update(x_any_pump and x_flow_switch)
        min_run_elapsed = self._pump_min_run.update(x_any_pump)

        x_heater = self._thermostat.update(
            enable=(x_freeze_permissive and x_heat_active),
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
TOUCH_DEBOUNCE_MS = 30          # min hold time before a new button fires
TOUCH_REPEAT_MS = 300           # auto-repeat interval for toggle buttons
TOUCH_SETPOINT_REPEAT_MS = 150  # auto-repeat interval for setpoint +/-

# ── Sensor averaging ──────────────────────────────────────────────────────────
TEMP_AVG_N = 8      # rolling-average window (samples × loop period = ~400 ms lag)

# ── Backlight sleep / dim ─────────────────────────────────────────────────────
BL_FREQ_HZ       = 1000          # PWM carrier frequency
BL_FULL_DUTY     = 100           # % brightness when active
BL_DIM_DUTY      = 25            # % brightness when idle
BL_DIM_TIMEOUT_MS   = 120_000   # 2 min idle → dim
BL_SLEEP_TIMEOUT_MS = 600_000   # 10 min idle → screen off
# ── HMI colour palette (RGB565) ──────────────────────────────────────────────
C_BG       = 0x0820   # deep navy background
C_PANEL    = 0x1082   # dark panel fill
C_BORDER   = 0x2965   # panel border / divider
C_TEXT     = 0xFFFF   # primary white text
C_LABEL    = 0xD69A   # secondary label – light grey 84 % (CSS #D3D3D3, below green-gamma threshold)
C_DIM      = 0x4208   # dimmed / ghost
C_SEG_ON   = 0xFFFF   # temperature reading – white
C_SP_ON    = 0xCE59   # setpoint display – light grey
C_LED_GN   = 0x07E0   # LED green
C_LED_AM   = 0xFD20   # LED amber (heater energised)
C_LED_YE   = 0xFFE0   # LED yellow (light on)
C_LED_CY   = 0x4BD0   # LED teal (pump running)
C_LED_OFF  = 0x2104   # LED inactive dark
C_BTN_NORM = 0x2965   # button idle
C_BTN_P_AC = 0x0099   # pump active (dark cyan)
C_BTN_H_AC = 0x6200   # heat active (dark amber)
C_BTN_L_AC = 0x4420   # light active (dark yellow-green)
C_ACCENT   = 0x8410   # button-border accent – medium grey
C_FAULT    = 0xF800   # fault red

# ── Panel geometry (landscape 480 × 320) ─────────────────────────────────────
# Top bar: connectivity icons + clock (right-aligned).
# Status indicators: horizontal bar at the bottom.
# Temperature panel: full left area (0–282 px) for large digits.
_TOP_BAR_H    = 22           # top status bar height (BT / WiFi / time)
_STATUS_BAR_H = 45           # bottom status-bar height
_STATUS_BAR_Y = 275          # y-start of bottom status bar (320 – 45)
_PNL_Y = _TOP_BAR_H          # main panels start below the top bar
_PNL_H = _STATUS_BAR_Y - _TOP_BAR_H   # main panel area height (253 px)
_PNL_T_X, _PNL_T_W = 0,   282   # temperature panel (full left area)
_PNL_C_X, _PNL_C_W = 282, 198   # controls panel (unchanged)

# ── 7-segment digit renderer ─────────────────────────────────────────────────
# Each digit drawn as smooth hexagonal-ended segments (angled tips, like a real
# digital display) rather than scaled bitmap pixels.
#
# Segment encoding: bit0=a(top) bit1=b(top-right) bit2=c(bot-right)
#                   bit3=d(bot) bit4=e(bot-left)  bit5=f(top-left) bit6=g(mid)
_SEG7 = (0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F)

# Temperature panel now 282 px wide – scale up both digit sizes.
_TEMP_SCALE = 11   # size unit – main temp display  (digit bounding box: 55 × 77 px)
_SP_SCALE   = 8    # size unit – setpoint display   (digit bounding box: 40 × 56 px)

# Pre-computed strip origins (centred in the 282-px temperature panel)
_TEMP_PITCH = 5 * _TEMP_SCALE + max(1, _TEMP_SCALE // 2)   # 60
_SP_PITCH   = 5 * _SP_SCALE   + max(1, _SP_SCALE   // 2)   # 44
_BIG_TEMP_X = _PNL_T_X + (_PNL_T_W - 2*_TEMP_PITCH - 5*_TEMP_SCALE) // 2  # 53
_BIG_TEMP_Y = 26 + _TOP_BAR_H   # 48
_BIG_SP_X   = _PNL_T_X + (_PNL_T_W - 2*_SP_PITCH - 5*_SP_SCALE) // 2      # 77
_BIG_SP_Y   = 136 + _TOP_BAR_H  # 158
_TEMP_DEG_X = _BIG_TEMP_X + 2*_TEMP_PITCH + 5*_TEMP_SCALE + 4   # 232
_SP_DEG_X   = _BIG_SP_X   + 2*_SP_PITCH   + 5*_SP_SCALE   + 3   # 208

UI_LIMITS = {
    "SETPOINT_MIN_F": 80.0,
    "SETPOINT_MAX_F": 104.0,
    "SETPOINT_STEP_F": 1.0,
}

# ── Special operating modes ───────────────────────────────────────────────────
ECO_SETPOINT_F      = 80.0           # temp target while ECO mode is active
MAX_JET_DURATION_MS = 20 * 60 * 1000 # 20-minute MAX JET auto-off

# Timer stubs (run-timers section removed from UI; kept so dead code doesn't NameError)
TIMER_LABEL_WIDTH = 0
TIMER_SPA_POS     = (0, 0)
TIMER_LIGHT_POS   = (0, 0)

# ── Touch button rects (x, y, w, h) ──────────────────────────────────────────
# Controls-panel y-values shifted up 28 px (title bar removed).
UI_BUTTONS = {
    # Top bar – brightness slider (sun icon at x≈4; slider track x=22…152)
    "brightness_slider": (22, 0, 130, _TOP_BAR_H),
    # Temperature panel – rounded-rect setpoint buttons (y shifted by _TOP_BAR_H)
    "setpoint_minus": (10,  230, 120, 44),
    "setpoint_plus":  (152, 230, 120, 44),
    # Controls panel – JET 1 (three-state)
    "pump_off":  (286, 58, 58, 32),
    "pump_low":  (350, 58, 58, 32),
    "pump_high": (414, 58, 56, 32),
    # Controls panel – JET 2 & 3 (toggles)
    "pump2": (286, 114, 89, 32),
    "pump3": (383, 114, 89, 32),
    # Controls panel – light (full-width, spans former heat+light row)
    "light": (286, 167, 186, 44),
    # Controls panel – operating modes
    "eco":     (286, 230, 89, 44),
    "max_jet": (383, 230, 89, 44),
}

TOUCH_BUTTON_ORDER = (
    "brightness_slider",   # checked first so it doesn't compete with panel buttons
    "pump_off",
    "pump_low",
    "pump_high",
    "pump2",
    "pump3",
    "light",
    "eco",
    "max_jet",
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
    global _hmi_lcd_cs, _hmi_bl_pwm
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
    # Backlight: use PWM for dimming support; fall back to digital if PWM fails.
    try:
        bl_pin = Pin(DISPLAY_PINS["LCD_BL"], Pin.OUT)
        _hmi_bl_pwm = PWM(bl_pin, freq=BL_FREQ_HZ, duty_u16=0 if DISPLAY_BL_ACTIVE_LOW else 65535)
    except Exception as err:
        _hmi_bl_pwm = None
        _report_hmi_error("HMI: backlight PWM init failed", err)
        # Hard digital fallback
        Pin(DISPLAY_PINS["LCD_BL"], Pin.OUT, value=0 if DISPLAY_BL_ACTIVE_LOW else 1)

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


# ── Modern 5 × 7 pixel-font helpers ──────────────────────────────────────────

def _fill_h_seg(lcd, x, y, w, t, d, color):
    """Horizontal segment with angled ends.  Flat middle batched to one fill_rect."""
    for dy in range(d):                         # top taper
        ind = d - dy
        sw = w - 2 * ind
        if sw > 0:
            lcd.fill_rect(x + ind, y + dy, sw, 1, color)
    mid = t - 2 * d
    if mid > 0:                                 # flat middle (one rect, biggest win)
        lcd.fill_rect(x, y + d, w, mid, color)
    for dy in range(d):                         # bottom taper
        ind = dy + 1
        sw = w - 2 * ind
        if sw > 0:
            lcd.fill_rect(x + ind, y + t - d + dy, sw, 1, color)


def _fill_v_seg(lcd, x, y, h, t, d, color):
    """Vertical segment with angled ends.  Flat middle batched to one fill_rect."""
    for dy in range(d):                         # top taper
        ind = d - dy
        sw = t - 2 * ind
        if sw > 0:
            lcd.fill_rect(x + ind, y + dy, sw, 1, color)
    mid = h - 2 * d
    if mid > 0:                                 # flat middle (one rect)
        lcd.fill_rect(x, y + d, t, mid, color)
    for dy in range(d):                         # bottom taper
        ind = dy + 1
        sw = t - 2 * ind
        if sw > 0:
            lcd.fill_rect(x + ind, y + h - d + dy, sw, 1, color)


def _draw_digit_7seg(lcd, d, x, y, W, H, fg, bg):
    """Draw a single 7-segment digit in a W×H bounding box at (x, y)."""
    lcd.fill_rect(x, y, W, H, bg)
    if not (0 <= d <= 9):
        return
    segs = _SEG7[d]
    T  = max(4, H // 7)      # segment thickness
    D  = T // 2              # diagonal cut at each tip
    G  = max(1, T // 6)      # gap where segments meet
    HH = H // 2              # centreline

    hw = W - D * 2           # horizontal segment drawable width
    vh = HH - D - G          # half-height of each vertical segment

    if segs & 0x01: _fill_h_seg(lcd, x + D,     y,              hw, T, D, fg)  # a top
    if segs & 0x02: _fill_v_seg(lcd, x + W - T, y + D,          vh, T, D, fg)  # b top-right
    if segs & 0x04: _fill_v_seg(lcd, x + W - T, y + HH + G,     vh, T, D, fg)  # c bot-right
    if segs & 0x08: _fill_h_seg(lcd, x + D,     y + H - T,      hw, T, D, fg)  # d bottom
    if segs & 0x10: _fill_v_seg(lcd, x,          y + HH + G,     vh, T, D, fg)  # e bot-left
    if segs & 0x20: _fill_v_seg(lcd, x,          y + D,          vh, T, D, fg)  # f top-left
    if segs & 0x40: _fill_h_seg(lcd, x + D,     y + HH - T // 2, hw, T, D, fg)  # g middle


def _draw_temp_int(lcd, temp_f, x, y, scale, fg, bg):
    """
    Draw an integer temperature (whole °F, no decimal) using 7-segment digits.
    Always occupies 3 digit positions, right-justified.
    temp_f: float or None (shows centre-bar dashes).
    """
    dw    = scale * 5
    dh    = scale * 7
    gap   = max(1, scale // 2)
    pitch = dw + gap
    lcd.fill_rect(x, y, 3 * pitch, dh, bg)

    if temp_f is None:
        mid_y = y + dh // 2 - scale // 2
        for i in range(3):
            lcd.fill_rect(x + i * pitch + scale, mid_y, dw - scale * 2, scale, fg)
        return

    temp_i = max(0, min(int(round(temp_f)), 999))
    d2 = temp_i // 100
    d1 = (temp_i // 10) % 10
    d0 = temp_i % 10

    if temp_i >= 100:
        _draw_digit_7seg(lcd, d2, x,             y, dw, dh, fg, bg)
        _draw_digit_7seg(lcd, d1, x + pitch,     y, dw, dh, fg, bg)
        _draw_digit_7seg(lcd, d0, x + 2 * pitch, y, dw, dh, fg, bg)
    else:
        _draw_digit_7seg(lcd, d1, x + pitch,     y, dw, dh, fg, bg)
        _draw_digit_7seg(lcd, d0, x + 2 * pitch, y, dw, dh, fg, bg)


# ── UI widget helpers ─────────────────────────────────────────────────────────

def _draw_led(lcd, x, y, color):
    """12 × 12 bordered LED indicator square."""
    lcd.fill_rect(x, y, 12, 12, C_BORDER)
    lcd.fill_rect(x + 2, y + 2, 8, 8, color)


def _fill_round_rect(lcd, x, y, w, h, r, color):
    """
    Fill a rounded rectangle with corner radius r.
    Straight sides are perfectly crisp; only the small corner arcs use scanlines.
    """
    r2 = r * r
    for dy in range(r):                                    # top arc (r rows)
        ind = r - int((r2 - (r - 1 - dy) ** 2) ** 0.5)
        lcd.fill_rect(x + ind, y + dy, w - 2 * ind, 1, color)
    if h > 2 * r:
        lcd.fill_rect(x, y + r, w, h - 2 * r, color)      # flat middle
    for dy in range(r):                                    # bottom arc (r rows)
        ind = r - int((r2 - dy ** 2) ** 0.5)
        lcd.fill_rect(x + ind, y + h - r + dy, w - 2 * ind, 1, color)


def _draw_round_btn(lcd, x, y, w, h, r, symbol):
    """Flat rounded-rectangle setpoint button with a thick +/- symbol."""
    _fill_round_rect(lcd, x, y, w, h, r, 0x528A)    # button body (medium steel)
    cx = x + w // 2
    cy = y + h // 2
    arm   = min(w, h) // 4
    thick = max(5, min(w, h) // 6)
    lcd.fill_rect(cx - arm, cy - thick // 2, 2 * arm + 1, thick, 0xFFFF)   # horiz
    if symbol == "+":
        lcd.fill_rect(cx - thick // 2, cy - arm, thick, 2 * arm + 1, 0xFFFF)  # vert


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


def _get_time_str():
    """Return current RTC time as 'HH:MM', or '--:--' if unavailable."""
    try:
        from machine import RTC
        dt = RTC().datetime()   # (year, month, day, weekday, hours, mins, secs, sub)
        return "%02d:%02d" % (dt[4], dt[5])
    except Exception:
        return "--:--"


def _draw_wifi_icon(lcd, x, y, color):
    """
    Draw a 3-bar ascending WiFi / signal-strength icon (11 × 10 px).
    Bars are 3 px wide with 1-px gaps; heights 4, 7, 10 px (tallest on right).
    x, y = top-left corner of the icon bounding box.
    """
    lcd.fill_rect(x,      y + 6, 3, 4,  color)   # short  bar (left)
    lcd.fill_rect(x + 4,  y + 3, 3, 7,  color)   # medium bar (centre)
    lcd.fill_rect(x + 8,  y,     3, 10, color)   # tall   bar (right)


def _draw_bt_icon(lcd, x, y, color):
    """
    Minimal 8 × 10 px Bluetooth glyph using fill_rect.
    Resembles the classic ᛒ shape: vertical spine with two V-chevrons.
    x, y = top-left corner.
    """
    # Vertical spine
    lcd.fill_rect(x + 3, y,     2, 10, color)
    # Upper-right diagonal  (\)
    lcd.fill_rect(x + 5, y + 1, 2, 2,  color)
    lcd.fill_rect(x + 6, y + 3, 2, 2,  color)
    # Upper-left  return   (/)
    lcd.fill_rect(x + 1, y + 3, 2, 2,  color)
    lcd.fill_rect(x,     y + 1, 2, 2,  color)
    # Lower-right diagonal (\)
    lcd.fill_rect(x + 5, y + 6, 2, 2,  color)
    lcd.fill_rect(x + 6, y + 8, 2, 2,  color)
    # Lower-left  return   (/)
    lcd.fill_rect(x + 1, y + 6, 2, 2,  color)
    lcd.fill_rect(x,     y + 8, 2, 2,  color)


def _draw_sun_icon(lcd, cx, cy, color):
    """
    Draw a 13 × 13 px sun icon centred at (cx, cy).
    4×4 px core circle + 8 single-pixel rays (cardinal + diagonal).
    """
    # Core circle
    lcd.fill_rect(cx - 2, cy - 2, 5, 5, color)
    # Cardinal rays (2 px long, 1 px wide)
    lcd.fill_rect(cx,     cy - 6, 1, 2, color)   # top
    lcd.fill_rect(cx,     cy + 5, 1, 2, color)   # bottom
    lcd.fill_rect(cx - 6, cy,     2, 1, color)   # left
    lcd.fill_rect(cx + 5, cy,     2, 1, color)   # right
    # Diagonal rays (2×2 px blobs)
    lcd.fill_rect(cx - 5, cy - 5, 2, 2, color)   # TL
    lcd.fill_rect(cx + 4, cy - 5, 2, 2, color)   # TR
    lcd.fill_rect(cx - 5, cy + 4, 2, 2, color)   # BL
    lcd.fill_rect(cx + 4, cy + 4, 2, 2, color)   # BR


def _draw_lightbulb(lcd, cx, cy, color):
    """
    Draw a light-bulb icon centred at (cx, cy).
    Bounding box: 16 × 22 px.
    Upper half = glass dome; lower half = threaded base (3 bands).
    """
    x = cx - 8   # left edge
    y = cy - 11  # top edge
    # --- glass dome ---
    lcd.fill_rect(x + 4, y,      8, 1, color)   # tip
    lcd.fill_rect(x + 2, y + 1, 12, 1, color)
    lcd.fill_rect(x + 1, y + 2, 14, 4, color)   # wide body
    lcd.fill_rect(x + 2, y + 6, 12, 1, color)
    lcd.fill_rect(x + 4, y + 7,  8, 1, color)   # bottom of dome
    # --- neck ---
    lcd.fill_rect(x + 5, y + 8,  6, 2, color)
    # --- base bands (threaded cap) ---
    lcd.fill_rect(x + 3, y + 11, 10, 2, color)  # band 1
    lcd.fill_rect(x + 3, y + 14, 10, 2, color)  # band 2
    lcd.fill_rect(x + 3, y + 17, 10, 2, color)  # band 3
    lcd.fill_rect(x + 4, y + 20,  8, 2, color)  # base cap


def _draw_static_frame(lcd):
    """
    Paint all fixed chrome once.
    Layout: top bar (full width, 22 px – BT/WiFi/time) |
            temperature panel (left 282 px) | controls panel (right 198 px) |
            horizontal status bar (full width, bottom 45 px).
    """
    T = _TOP_BAR_H   # shorthand

    # ── Top status bar ────────────────────────────────────────────────────────
    lcd.fill_rect(0, 0, 480, T, C_PANEL)
    lcd.fill_rect(0, T - 1, 480, 1, C_BORDER)

    # ── Panel fills (below top bar) ───────────────────────────────────────────
    lcd.fill_rect(_PNL_T_X, T, _PNL_T_W, _PNL_H, C_BG)
    lcd.fill_rect(_PNL_C_X, T, _PNL_C_W, _PNL_H, C_PANEL)
    lcd.fill_rect(0, _STATUS_BAR_Y, 480, _STATUS_BAR_H, C_PANEL)

    # ── Dividers ──────────────────────────────────────────────────────────────
    lcd.fill_rect(_PNL_T_X + _PNL_T_W, T, 2, _PNL_H, C_BORDER)  # temp | controls
    lcd.fill_rect(0, _STATUS_BAR_Y, 480, 1, C_BORDER)             # main | status bar

    # ── Temperature panel ─────────────────────────────────────────────────────
    _tw = _PNL_T_W
    lcd.text("WATER TEMP", (_tw - 10 * 8) // 2, T + 8,  C_LABEL)
    lcd.fill_rect(_PNL_T_X, T + 22,  _tw, 1, C_BORDER)  # below label
    # [big temp digits: _BIG_TEMP_Y … _BIG_TEMP_Y+77]
    lcd.fill_rect(_PNL_T_X, T + 108, _tw, 1, C_BORDER)  # between water and target
    lcd.text("TARGET TEMP", (_tw - 11 * 8) // 2, T + 114, C_LABEL)
    lcd.fill_rect(_PNL_T_X, T + 130, _tw, 1, C_BORDER)  # below label
    # [setpoint digits: _BIG_SP_Y … _BIG_SP_Y+56]
    lcd.fill_rect(_PNL_T_X, T + 200, _tw, 1, C_BORDER)  # above +/- buttons
    # Rounded-rectangle setpoint buttons: W=120 H=44 corner-r=12
    _draw_round_btn(lcd, 10,  T + 208, 120, 44, 12, "-")
    _draw_round_btn(lcd, 152, T + 208, 120, 44, 12, "+")

    # ── Controls panel ────────────────────────────────────────────────────────
    lcd.text("JET 1",     357, T + 8,   C_LABEL)
    lcd.fill_rect(_PNL_C_X, T + 28,  _PNL_C_W, 1, C_BORDER)
    lcd.fill_rect(_PNL_C_X, T + 72,  _PNL_C_W, 1, C_BORDER)
    lcd.text("JET 2 / 3", 341, T + 80,  C_LABEL)
    lcd.fill_rect(_PNL_C_X, T + 128, _PNL_C_W, 1, C_BORDER)
    lcd.text("LIGHT",     361, T + 136, C_LABEL)
    lcd.fill_rect(_PNL_C_X, T + 192, _PNL_C_W, 1, C_BORDER)
    lcd.text("MODES",     361, T + 196, C_LABEL)

    # ── Bottom status bar ─────────────────────────────────────────────────────
    _sb_ty = _STATUS_BAR_Y + (_STATUS_BAR_H - 8) // 2
    lcd.text("HEAT",  52,  _sb_ty, C_LABEL)
    lcd.text("JETS",  172, _sb_ty, C_LABEL)
    lcd.text("LITE",  292, _sb_ty, C_LABEL)
    lcd.text("FAULT", 408, _sb_ty, C_LABEL)


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


def _update_setpoint_display(lcd, ctrl, ui_state=None):
    if lcd is None:
        return
    _draw_temp_int(lcd, ctrl.temp_setpoint_f, _BIG_SP_X, _BIG_SP_Y, _SP_SCALE, C_SP_ON, C_BG)
    lcd.fill_rect(_SP_DEG_X, _BIG_SP_Y, 20, 12, C_BG)
    lcd.text("oF", _SP_DEG_X, _BIG_SP_Y + 2, C_LABEL)
    if ui_state is not None:
        ui_state["_c_sp"] = ctrl.temp_setpoint_f  # keep cache in sync


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
        ui_state["_touch_press_ms"] = None
        return

    # Wake display on any touch; restore to user-saved brightness.
    if ui_state.get("_dim_state", "bright") != "bright":
        saved = ui_state.get("bl_brightness", BL_FULL_DUTY)
        _set_backlight(saved)
        ui_state["_dim_state"] = "bright"
        ui_state["_last_any_touch_ms"] = now_ms   # wake-tap counts as activity
        # Swallow this touch so a sleep-tap never triggers a button.
        ui_state["touch_button"] = None
        ui_state["_touch_press_ms"] = None
        return

    x, y = point

    # ── Brightness slider: immediate update on drag, no debounce delay ───────
    if _touch_button_at(x, y) == "brightness_slider":
        bx, _by, bw, _bh = UI_BUTTONS["brightness_slider"]
        pct = max(5, min(100, int((x - bx) * 100 // bw)))
        if pct != ui_state.get("bl_brightness", BL_FULL_DUTY):
            ui_state["bl_brightness"] = pct
            _set_backlight(pct)
            ui_state.pop("_c_top", None)   # force slider redraw
        ui_state["_last_any_touch_ms"] = now_ms
        ctrl._run_timer_start_ms = None
        ui_state["last_touch_ms"] = now_ms
        return

    button = _touch_button_at(x, y)
    if TOUCH_DEBUG:
        print("HIT x=%d y=%d -> %s" % (x, y, button if button else "MISS"))
    if button is None:
        # Phantom / dead-zone touch — don't reset idle timer.
        ui_state["touch_button"] = None
        ui_state["_touch_press_ms"] = None
        return

    if button != ui_state.get("touch_button"):
        # New button detected — start debounce clock, don't act yet.
        ui_state["touch_button"]   = button
        ui_state["_touch_press_ms"] = now_ms
        return

    # Same button held — enforce initial debounce before the first action.
    press_ms = ui_state.get("_touch_press_ms")
    if press_ms is not None and ticks_diff(now_ms, press_ms) < TOUCH_DEBOUNCE_MS:
        return

    repeat_ms = (
        TOUCH_SETPOINT_REPEAT_MS
        if button in ("setpoint_minus", "setpoint_plus")
        else TOUCH_REPEAT_MS
    )
    if ticks_diff(now_ms, ui_state.get("last_touch_ms", 0)) < repeat_ms:
        return

    handled = False
    if button == "pump_off":
        # Block OFF when the heater requires water flow for safe operation.
        wt    = ui_state.get("_water_temp_f", 999.0)
        sp_lo = ctrl.temp_setpoint_f - ctrl.temp_hysteresis_f * 0.5
        heat_needs_pump = wt < sp_lo   # matches thermostat turn-on threshold
        if not heat_needs_pump and ui_state["pump1_mode"] != 0:
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
    elif button == "eco":
        if ui_state.get("eco_mode"):
            # Leaving ECO – restore previous setpoint
            ui_state["eco_mode"] = False
            prev = ui_state.pop("_eco_prev_sp", None)
            if prev is not None:
                ctrl.temp_setpoint_f = prev
                ui_state.pop("_c_sp", None)   # force setpoint redraw
        else:
            # Entering ECO – cancel MAX JET, save setpoint, lock to 80 °F
            if ui_state.get("max_jet_on"):
                ui_state["max_jet_on"] = False
                ui_state["max_jet_start_ms"] = None
            ui_state["_eco_prev_sp"] = ctrl.temp_setpoint_f
            ctrl.temp_setpoint_f = ECO_SETPOINT_F
            ui_state["eco_mode"] = True
            ui_state.pop("_c_sp", None)
        handled = True
    elif button == "max_jet":
        if ui_state.get("max_jet_on"):
            # Cancel MAX JET early
            ui_state["max_jet_on"] = False
            ui_state["max_jet_start_ms"] = None
        else:
            # Start MAX JET – cancel ECO first
            if ui_state.get("eco_mode"):
                ui_state["eco_mode"] = False
                prev = ui_state.pop("_eco_prev_sp", None)
                if prev is not None:
                    ctrl.temp_setpoint_f = prev
                    ui_state.pop("_c_sp", None)
            ui_state["max_jet_on"] = True
            ui_state["max_jet_start_ms"] = now_ms
        handled = True
    elif button == "setpoint_minus":
        new_setpoint_f = max(
            UI_LIMITS["SETPOINT_MIN_F"],
            ctrl.temp_setpoint_f - UI_LIMITS["SETPOINT_STEP_F"],
        )
        if new_setpoint_f != ctrl.temp_setpoint_f:
            ctrl.temp_setpoint_f = new_setpoint_f
            _update_setpoint_display(lcd, ctrl, ui_state)
            handled = True
    elif button == "setpoint_plus":
        new_setpoint_f = min(
            UI_LIMITS["SETPOINT_MAX_F"],
            ctrl.temp_setpoint_f + UI_LIMITS["SETPOINT_STEP_F"],
        )
        if new_setpoint_f != ctrl.temp_setpoint_f:
            ctrl.temp_setpoint_f = new_setpoint_f
            _update_setpoint_display(lcd, ctrl, ui_state)
            handled = True

    # Confirmed button press → reset idle timer and spa run timer.
    ui_state["_last_any_touch_ms"] = now_ms
    ctrl._run_timer_start_ms = None   # restart 4-hour safety window on each HMI interaction
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

    # ECO mode: Jet 1 LOW, Jets 2 & 3 off, setpoint locked to ECO_SETPOINT_F.
    if ui_state.get("eco_mode"):
        values["xPumpRequest"]      = True
        values["xPump1HighRequest"] = False
        values["xPump2Request"]     = False
        values["xPump3Request"]     = False

    # MAX JET: all jets on HIGH (overrides ECO if both somehow active).
    if ui_state.get("max_jet_on"):
        values["xPumpRequest"]      = True
        values["xPump1HighRequest"] = True
        values["xPump2Request"]     = True
        values["xPump3Request"]     = True

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

    # ── Top bar: brightness slider (left) + BT/WiFi/clock (right) ─────────────
    bt_con   = bool(ui_state.get("bt_connected",   False))
    wifi_con = bool(ui_state.get("wifi_connected", False))
    time_str = _get_time_str()
    bl_pct   = int(ui_state.get("bl_brightness", BL_FULL_DUTY))
    top_key  = (bt_con, wifi_con, time_str, bl_pct)
    if top_key != ui_state.get("_c_top"):
        ui_state["_c_top"] = top_key
        ty = (_TOP_BAR_H - 10) // 2   # icon top  (10-px tall icons)
        tt = (_TOP_BAR_H - 8)  // 2   # text top  (8-px tall font)

        # ── Left: sun icon + brightness slider ──────────────────────────────
        # Sun icon centred vertically at x=11
        lcd.fill_rect(0, 0, 160, _TOP_BAR_H - 1, C_PANEL)   # clear region
        _draw_sun_icon(lcd, 11, _TOP_BAR_H // 2, C_LABEL)
        # Slider track (x=22 … 152, height=6, vertically centred)
        _sx, _sy, _sw, _sh = 22, (_TOP_BAR_H - 6) // 2, 130, 6
        lcd.fill_rect(_sx, _sy, _sw, _sh, C_BORDER)          # empty track
        _fill_w = max(4, _sw * bl_pct // 100)
        lcd.fill_rect(_sx, _sy, _fill_w, _sh, C_LABEL)       # filled portion
        # Thumb tick (3 px wide, full bar height)
        _thumb_x = _sx + _fill_w - 2
        lcd.fill_rect(_thumb_x, 2, 3, _TOP_BAR_H - 4, C_TEXT)

        # ── Right: BT icon, WiFi bars, time ─────────────────────────────────
        lcd.fill_rect(390, 0, 90, _TOP_BAR_H - 1, C_PANEL)
        _draw_bt_icon(lcd,  396, ty, C_LABEL)
        _draw_wifi_icon(lcd, 410, ty, C_LABEL)
        lcd.text(time_str,   436, tt, C_LABEL)

    heat_req  = ui_state["xHeatRequest"]
    light_req = ui_state["xLightRequest"]
    pump_mode = ui_state["pump1_mode"]
    pump2_on  = bool(ui_state.get("pump2_on", False))
    pump3_on  = bool(ui_state.get("pump3_on", False))
    heater_on = bool(outputs.get("xHeater"))
    pump_on   = pump_mode > 0 or pump2_on or pump3_on
    fault     = bool(outputs.get("xFault"))
    fc        = int(outputs.get("iFaultCode", 0))

    # ── Water temperature (only redraws when the integer value changes) ───────
    wt_raw = inputs.get("rWaterTemp_F")
    wt_i   = int(round(wt_raw)) if wt_raw is not None else None
    if wt_i != ui_state.get("_c_wt"):
        ui_state["_c_wt"] = wt_i
        _draw_temp_int(lcd, wt_raw, _BIG_TEMP_X, _BIG_TEMP_Y,
                       _TEMP_SCALE, C_SEG_ON, C_BG)
        lcd.fill_rect(_TEMP_DEG_X, _BIG_TEMP_Y, 20, 12, C_BG)
        lcd.text("oF", _TEMP_DEG_X, _BIG_TEMP_Y + 4, C_LABEL)

    # ── Setpoint (only redraws when value changes) ────────────────────────────
    sp = ctrl.temp_setpoint_f
    if sp != ui_state.get("_c_sp"):
        ui_state["_c_sp"] = sp
        _draw_temp_int(lcd, sp, _BIG_SP_X, _BIG_SP_Y,
                       _SP_SCALE, C_SP_ON, C_BG)
        lcd.fill_rect(_SP_DEG_X, _BIG_SP_Y, 20, 12, C_BG)
        lcd.text("oF", _SP_DEG_X, _BIG_SP_Y + 2, C_LABEL)

    # ── Status bar LEDs (only redraws when any status changes) ───────────────
    _sb_led_y = _STATUS_BAR_Y + (_STATUS_BAR_H - 12) // 2
    heat_led  = C_LED_AM if heater_on else C_LED_OFF
    led_key   = (heat_led, pump_on, light_req, fault, fc)
    if led_key != ui_state.get("_c_led"):
        ui_state["_c_led"] = led_key
        _draw_led(lcd,  36, _sb_led_y, heat_led)
        _draw_led(lcd, 156, _sb_led_y, C_LED_CY if pump_on   else C_LED_OFF)
        _draw_led(lcd, 276, _sb_led_y, C_LED_YE if light_req else C_LED_OFF)
        _draw_led(lcd, 392, _sb_led_y, C_FAULT  if fault     else C_LED_OFF)
        _sb_ty = _STATUS_BAR_Y + (_STATUS_BAR_H - 8) // 2
        if fault:
            lcd.fill_rect(408, _sb_ty - 1, 56, 10, C_PANEL)
            lcd.text("FC:%d" % fc, 408, _sb_ty, C_FAULT)
        else:
            lcd.fill_rect(408, _sb_ty - 1, 56, 10, C_PANEL)
            lcd.text("FAULT", 408, _sb_ty, C_LABEL)

    # ── Controls panel buttons (only redraws when state changes) ─────────────
    eco_mode   = bool(ui_state.get("eco_mode",   False))
    max_jet_on = bool(ui_state.get("max_jet_on", False))

    # MAX JET countdown label — changes every minute while active.
    if max_jet_on:
        elapsed_ms  = ticks_diff(ticks_ms(), ui_state.get("max_jet_start_ms", 0))
        remain_ms   = max(0, MAX_JET_DURATION_MS - elapsed_ms)
        remain_m    = (remain_ms + 59999) // 60000   # round up to nearest minute
        mj_label    = ("%d min" % remain_m) if remain_m > 0 else "< 1m"
    else:
        remain_m = 0
        mj_label = "MAX JET"

    # Reflect actual pump outputs so the display stays honest when the
    # heater forces Jet 1 LOW regardless of the user's OFF/LOW/HI selection.
    p1_low_act  = bool(outputs.get("xPump1_Low",  False))
    p1_high_act = bool(outputs.get("xPump1_High", False))
    p1_off_act  = not (p1_low_act or p1_high_act)

    p2_act = bool(outputs.get("xPump2", False))
    p3_act = bool(outputs.get("xPump3", False))

    btn_key = (p1_off_act, p1_low_act, p1_high_act,
               p2_act, p3_act, light_req,
               eco_mode, max_jet_on, remain_m)
    if btn_key != ui_state.get("_c_btn"):
        ui_state["_c_btn"] = btn_key
        _draw_button_v2(lcd, UI_BUTTONS["pump_off"],  "OFF",
                        active=p1_off_act,  act_color=C_BTN_P_AC)
        _draw_button_v2(lcd, UI_BUTTONS["pump_low"],  "LOW",
                        active=p1_low_act,  act_color=C_BTN_P_AC)
        _draw_button_v2(lcd, UI_BUTTONS["pump_high"], "HI",
                        active=p1_high_act, act_color=C_BTN_P_AC)
        _draw_button_v2(lcd, UI_BUTTONS["pump2"], "JET 2",
                        active=p2_act, act_color=C_BTN_P_AC)
        _draw_button_v2(lcd, UI_BUTTONS["pump3"], "JET 3",
                        active=p3_act, act_color=C_BTN_P_AC)
        # Light button – draw bevel background then centred bulb icon
        _bx, _by, _bw, _bh = UI_BUTTONS["light"]
        _bbg = C_BTN_L_AC if light_req else C_BTN_NORM
        lcd.fill_rect(_bx, _by, _bw, _bh, _bbg)
        _bhi = C_ACCENT if light_req else C_BORDER
        lcd.fill_rect(_bx,          _by,          _bw, 1,   _bhi)   # top
        lcd.fill_rect(_bx,          _by,          1,   _bh, _bhi)   # left
        lcd.fill_rect(_bx,          _by + _bh - 1, _bw, 1,  C_DIM)  # bottom
        lcd.fill_rect(_bx + _bw - 1, _by,          1,  _bh, C_DIM)  # right
        _draw_lightbulb(lcd, _bx + _bw // 2, _by + _bh // 2, C_TEXT)
        _draw_button_v2(lcd, UI_BUTTONS["eco"],     "ECO",
                        active=eco_mode,   act_color=0x0640)   # muted green
        _draw_button_v2(lcd, UI_BUTTONS["max_jet"], mj_label,
                        active=max_jet_on, act_color=0x7800)   # deep orange


def render_hmi(lcd, inputs, outputs, ctrl, ui_state, full=False):
    """Enhanced industrial HMI render. Safe no-op when driver is unavailable."""
    if lcd is None:
        return

    try:
        if full or not ui_state.get("_hmi_initialized", False):
            lcd.fill(C_BG)
            _draw_static_frame(lcd)
            ui_state["_hmi_initialized"] = True
            # Force all dynamic regions to repaint after a full wipe
            for k in ("_c_wt", "_c_sp", "_c_led", "_c_btn", "_c_top"):
                ui_state.pop(k, None)
        _render_dynamic_fields(lcd, inputs, outputs, ctrl, ui_state)
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
    temp_avg = RollingAverage(TEMP_AVG_N)
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
        "xHeatRequest": True,   # permanently armed; no UI toggle
        "xLightRequest": False,
        "pump1_mode": 1,        # 0=off, 1=low, 2=high
        "pump2_on": False,
        "pump3_on": False,
        "eco_mode": False,
        "max_jet_on": False,
        "max_jet_start_ms": None,
        "_eco_prev_sp": None,
        "touch_button": None,
        "last_touch_ms": 0,
        "_last_any_touch_ms": 0,
        "_touch_press_ms": None,
        "_dim_state": "bright",  # "bright" | "dim" | "sleep"
        "_render_error_logged": False,
        "_hmi_initialized": False,
        "_dynamic_key": None,
        "_timer_key": None,
        "bl_brightness":  BL_FULL_DUTY,   # 0-100 %; persists across dim/sleep cycles
        # Top-bar connectivity state – TEST: forced on to verify icon layout
        "bt_connected":   True,
        "wifi_connected": True,
    }
    raw_inputs = read_inputs()
    raw_inputs["rWaterTemp_F"] = temp_avg.update(raw_inputs.get("rWaterTemp_F", 0.0))
    inputs = apply_ui_overrides(raw_inputs, ui_state)
    outputs = ctrl.step(inputs)
    render_hmi(lcd, inputs, outputs, ctrl, ui_state, full=True)
    ui_state["_dynamic_key"] = _dynamic_snapshot(inputs, outputs, ctrl, ui_state)

    _last_touch_display = None

    while True:
        raw_inputs = read_inputs()
        raw_inputs["rWaterTemp_F"] = temp_avg.update(raw_inputs.get("rWaterTemp_F", 0.0))
        ui_state["_water_temp_f"] = raw_inputs["rWaterTemp_F"]
        now = ticks_ms()
        update_touch_ui(touch, ui_state, ctrl, now, lcd)
        inputs = apply_ui_overrides(raw_inputs, ui_state)
        outputs = ctrl.step(inputs)
        write_outputs(outputs)

        # ── MAX JET auto-off after 20 min ────────────────────────────────────
        if ui_state["max_jet_on"]:
            if ticks_diff(now, ui_state["max_jet_start_ms"]) >= MAX_JET_DURATION_MS:
                ui_state["max_jet_on"] = False
                ui_state["max_jet_start_ms"] = None

        # ── ECO mode: keep setpoint locked to ECO_SETPOINT_F ─────────────────
        if ui_state["eco_mode"] and ctrl.temp_setpoint_f != ECO_SETPOINT_F:
            ctrl.temp_setpoint_f = ECO_SETPOINT_F
            ui_state.pop("_c_sp", None)

        # ── Backlight dim / sleep ─────────────────────────────────────────────
        idle_ms    = ticks_diff(now, ui_state["_last_any_touch_ms"])
        dim_state  = ui_state.get("_dim_state", "bright")
        if dim_state == "bright" and idle_ms >= BL_DIM_TIMEOUT_MS:
            _set_backlight(BL_DIM_DUTY)
            ui_state["_dim_state"] = "dim"
        elif dim_state == "dim" and idle_ms >= BL_SLEEP_TIMEOUT_MS:
            _set_backlight(0)
            ui_state["_dim_state"] = "sleep"
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
