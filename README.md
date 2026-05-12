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
| `xHeatRequest` | 6 |
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
| `xHeatRequest` | 6 | Heat request |
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

## Commissioning Checklist

1. Verify every GPIO against your exact DevKitC-1 board revision.
2. Confirm relay input polarity (active-high vs active-low).
3. Validate flow/high-limit/e-stop logic sense before live operation.
4. Confirm temperature scaling reports deg F correctly.
5. Test outputs unloaded first (LEDs/meter), then with contactors.
6. Confirm timer behavior: spa 20 min, light 60 min.
7. Verify all fault interlocks force safe output states.
