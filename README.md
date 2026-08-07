# Spa Controller Wiring Reference

Target MCU: `ESP32-S3-DevKitC-1-N8R8`  
Display: `Hosyond 4.0" 480x320 SPI TFT (ST7796S + XPT2046 touch)`  
Firmware: `spa_control.py`

## Installer Quick Reference

### Controller Inputs

| Signal | GPIO |
|---|---:|
| `xSpaEnable` | 4 |
| `xPumpRequest` | 5 |
| `rWaterTemp` (NTC ADC) | 6 |
| `xPump1HighRequest` | 7 |
| `xPump2Request` | 15 |
| `xPump3Request` | 16 |
| `xJetsRequest` | 17 |
| `xBlowerRequest` | 18 |
| `xLightRequest` | 8 |
| `xFlowSwitch` | 9 |
| `xHighLimitOK` | 10 |
| `xRemoteEStopOK` | 11 |

### Controller Outputs

| Signal | GPIO |
|---|---:|
| `xPump1_Low` | 12 |
| `xPump1_High` | 13 |
| `xPump2` | 14 |
| `xPump3` | 21 |
| `xHeater` | 38 |
| `xJets` | 39 |
| `xBlower` | 40 |
| `xLight` | 41 |

### Display + Touch Pins

| Signal | GPIO |
|---|---:|
| `SCK` | 42 |
| `MOSI` | 47 |
| `MISO` | 48 |
| `LCD_CS` | 2 |
| `LCD_DC` | 1 |
| `LCD_RST` | 3 |
| `LCD_BL` | 22 |
| `TOUCH_CS` | 43 |
| `TOUCH_IRQ` | 45 |

## System Overview

- Control scan loop: `100 ms`
- HMI refresh: `500 ms`
- Spa runtime timer: `20 minutes`
- Light runtime timer: `60 minutes`
- Temperature units: `Fahrenheit`

## Safety Notes

- ESP32 GPIO is **3.3V logic only**.
- Do **not** connect 120/240VAC loads directly to GPIO.
- Use properly rated opto-isolated relay/contactor interfaces.
- High-voltage work should be done by a qualified electrician.

## ESP32-S3 Logic Levels

| Item | Expected |
|---|---|
| GPIO logic domain | 3.3V |
| GPIO LOW | ~0V |
| GPIO HIGH | ~3.3V |
| 5V direct to GPIO | Not allowed |

## Controller GPIO Map

### Digital Inputs (`Pin.IN`, `Pin.PULL_UP`)

| Signal | GPIO | Function |
|---|---:|---|
| `xSpaEnable` | 4 | Master spa enable command |
| `xPumpRequest` | 5 | Pump request (P1 low unless high selected) |
| `rWaterTemp` | 6 | **Water-temp NTC (analog ADC1_CH5)** — see *Water Temperature Sensor* below. (Was the heat-call input; heating is now driven by measured temp vs setpoint, so the external heat call is no longer wired.) |
| `xPump1HighRequest` | 7 | Pump 1 high-speed request |
| `xPump2Request` | 15 | Pump 2 single-speed request |
| `xPump3Request` | 16 | Pump 3 single-speed request |
| `xJetsRequest` | 17 | Jets request |
| `xBlowerRequest` | 18 | Blower request |
| `xLightRequest` | 8 | Light request |
| `xFlowSwitch` | 9 | Flow proof input (`TRUE = flow OK`) |
| `xHighLimitOK` | 10 | High-limit permissive (`TRUE = OK`) |
| `xRemoteEStopOK` | 11 | Remote e-stop permissive (`TRUE = OK`) |

### Digital Outputs (`Pin.OUT`)

| Signal | GPIO | Function |
|---|---:|---|
| `xPump1_Low` | 12 | Pump 1 low-speed command |
| `xPump1_High` | 13 | Pump 1 high-speed command |
| `xPump2` | 14 | Pump 2 command |
| `xPump3` | 21 | Pump 3 command |
| `xHeater` | 38 | Heater contactor command |
| `xJets` | 39 | Jets actuator/relay command |
| `xBlower` | 40 | Blower relay command |
| `xLight` | 41 | Light relay command |

## Display + Touch GPIO Mapping

### Shared SPI Bus

| Bus Signal | GPIO |
|---|---:|
| `SCK` | 42 |
| `MOSI` | 47 |
| `MISO` | 48 |

### LCD Control (ST7796S)

| LCD Signal | GPIO | Notes |
|---|---:|---|
| `LCD_CS` | 2 | LCD chip select |
| `LCD_DC` | 1 | Data/command |
| `LCD_RST` | 3 | Hardware reset |
| `LCD_BL` | 22 | Backlight PWM (NPN transistor gate) |

### Touch Control (XPT2046)

| Touch Signal | GPIO | Notes |
|---|---:|---|
| `TOUCH_CS` | 43 | Touch chip select |
| `TOUCH_IRQ` | 45 | Touch IRQ input (input-only pin) |

## Hosyond Module Pinout (Typical)

### ST7796S Display Header

| Module Pin | Connect To |
|---|---|
| `VCC` | Board supply (per module spec, often 5V) |
| `GND` | ESP32 GND |
| `CS` | `LCD_CS` |
| `RESET` | `LCD_RST` |
| `DC/RS` | `LCD_DC` |
| `SDI` | `MOSI` |
| `SCK` | `SCK` |
| `LED` | NPN transistor collector (GPIO22 controls gate) |
| `SDO` | `MISO` |

### XPT2046 Touch Header

| Module Pin | Connect To |
|---|---|
| `T_CLK` | `SCK` |
| `T_CS` | `TOUCH_CS` |
| `T_DIN` | `MOSI` |
| `T_DO` | `MISO` |
| `T_IRQ` | `TOUCH_IRQ` |

## Simple Wiring Diagram (Text)

```text
ESP32-S3-DevKitC-1-N8R8
  SPI: SCK=42, MOSI=47, MISO=48
        |        |         |
        +--------+---------+--------------------------+
                                                     |
                                   Hosyond 4.0" ST7796S + XPT2046
                                   --------------------------------
                                   LCD_SCK  <- GPIO42 (SCK)
                                   LCD_SDI  <- GPIO47 (MOSI)
                                   LCD_SDO  -> GPIO48 (MISO)
                                   LCD_CS   <- GPIO2
                                   LCD_DC   <- GPIO1
                                   LCD_RST  <- GPIO3
                                   LCD_LED  <- GPIO22 (via NPN transistor)

                                   T_CLK    <- GPIO42 (shared)
                                   T_DIN    <- GPIO47 (shared)
                                   T_DO     -> GPIO48 (shared)
                                   T_CS     <- GPIO43
                                   T_IRQ    -> GPIO45

                                   VCC      <- 5V or 3.3V (per module)
                                   GND      <- Common GND
```

## Backlight PWM Circuit

The Hosyond module's `LED` pin is wired directly to 5V on the PCB (always-on).
To enable software brightness control a small NPN transistor is added in-line:

```text
GPIO22 ──[470 Ω]──┐
                  NPN Base   (e.g. 2N2222 / BC547 / S8050)
5V ─── LED(+) ─── LED(−) ── NPN Collector
                             NPN Emitter ── GND
```

- `GPIO22 HIGH` → transistor ON → backlight on
- `GPIO22 LOW`  → transistor OFF → backlight off
- PWM on GPIO22 gives proportional brightness control (1 kHz carrier)
- `DISPLAY_BL_ACTIVE_LOW = False` (active-high, matches this circuit)

## Reserved/Caution Pins (ESP32-S3)

- Avoid `GPIO0` for normal runtime controls (boot strap behavior).
- Avoid `GPIO19`/`GPIO20` when native USB is used.
- Avoid `GPIO45`/`GPIO46` as general outputs.
- Avoid `GPIO26..GPIO37` (commonly tied to module flash/PSRAM).

## Water Temperature Sensor (NTC thermistor)

The controller regulates from a water-temp probe. The **default and recommended**
sensor is an **NTC thermistor** read on an analog ADC pin — the right choice for the
retrofit market (most spa packs already run a 10 kΩ NTC that can be reused) and,
crucially, it has **no bit-bang timing**, so it can never disable CPU interrupts.
The earlier DS18B20 (1-Wire) driver hard-locked the ESP32-S3's WiFi and is now
**legacy-only** (kept for boards that already use one; see below).

Independent over-temp safety is still the hardware high-limit on `xHighLimitOK` —
the software sensor is the *regulating* sensor only.

### Wiring — NTC on `GPIO6` (ADC1_CH5)

| NTC probe / part | Connect To |
|---|---|
| NTC lead A | `GPIO6` (ADC node) **and** through `R_fixed` to 3.3 V |
| NTC lead B | Common GND |
| `R_fixed` | **10 kΩ 1% between `GPIO6` and 3.3 V** (divider top) |
| filter (recommended) | **100 nF between `GPIO6` and GND** (EMI on long retrofit leads) |

```text
      3.3V ───[ R_fixed 10kΩ ]──┬────────────── (ADC node)
                                 │
                              GPIO6 ── ADC1_CH5
                                 │
                              [ NTC probe ]
                                 │
      GND ──────────────────────┴───[100nF]──── GND
```

Use an **ADC1** pin (`GPIO1–10`) — ADC2 conflicts with WiFi on the ESP32-S3.
`GPIO6` was the old heat-call input; a real temperature sensor makes an external
heat call redundant (heating is driven by measured temp vs setpoint), so that pin
is repurposed. Put the probe in a thermowell in flowing water downstream of the
heater; keep the cable away from relay/AC wiring.

### Calibration (2-point, from the app)

Because retrofit NTCs vary (Beta/tolerance differ by brand), the curve is
calibrated in the SpaControl app's **Settings → Calibrate temperature sensor**
wizard — either pick a preset (Balboa/Gecko/generic 10 k) or run a 2-point field
calibration that fits **any** unknown probe from two thermometer readings. The app
solves the Beta model and pushes it via a `set_temp_cal` command; the firmware
applies it live and **persists it to `config.json`**, so the spa keeps its
calibration across reboots and runs standalone. The board publishes the live probe
resistance as `r_ohms` in `spa/<id>/status` for the wizard to read.

### Behavior & config

- Non-blocking: the ADC is oversampled (16×) every ~1 s using the calibrated
  `read_uv()` curve.
- **Fail-safe:** an open / shorted / out-of-range probe sets `xTempSensorOK`
  false → **fault code 5** and the heater is disabled. Reconnecting the probe
  clears the fault automatically.
- Optional `config.json` block (defaults to NTC on `GPIO6` if omitted; the wizard
  writes the calibrated values here):

```json
"sensor": { "type": "ntc", "pin": 6, "r_fixed": 10000, "r0": 10000, "t0_c": 25, "beta": 3950, "offset_f": 0.0 }
"sensor": { "type": "ds18b20", "pin": 0, "offset_f": 0.0 }   // legacy 1-Wire probe
"sensor": { "type": "none" }                                  // bench stub
```

`offset_f` trims thermowell/placement error. `"none"` restores the bench stub.

> **Legacy DS18B20:** if a board must use a 1-Wire DS18B20, set `"type": "ds18b20"`
> with `DQ` on `GPIO0` and a **4.7 kΩ** pull-up from `DQ` to 3.3 V. Note this driver
> can intermittently lock up the ESP32-S3 when WiFi is active — prefer the NTC.

## Commissioning Checklist

1. Verify every GPIO against your exact DevKitC-1 board revision.
2. Confirm relay input polarity (active-high vs active-low).
3. Validate flow/high-limit/e-stop logic sense before live operation.
4. Confirm temperature scaling reports deg F correctly.
5. Test outputs unloaded first (LEDs/meter), then with contactors.
6. Confirm timer behavior: spa 20 min, light 60 min.
7. Verify all fault interlocks force safe output states.
