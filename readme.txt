SPA CONTROLLER WIRING REFERENCE
Target MCU: ESP32-S3-DevKitC-1-N8R8
Display: Hosyond 4.0in 480x320 SPI TFT, ST7796S + XPT2046 Touch

============================================================
1) SYSTEM OVERVIEW
============================================================
- Controller firmware file: spa_control.py
- Main control loop: 100 ms
- UI refresh loop: 500 ms
- Spa runtime timer: 20 minutes
- Light runtime timer: 60 minutes
- Temperature units: Fahrenheit

IMPORTANT SAFETY NOTE:
- The ESP32 GPIO pins are LOW VOLTAGE LOGIC ONLY.
- Do NOT connect pumps/heater/240 Vac loads directly to GPIO pins.
- Use properly rated opto-isolated relay/contactor driver interfaces.
- Follow local code and have high-voltage wiring done by a qualified electrician.

============================================================
2) ESP32-S3 LOGIC LEVELS AND EXPECTED VOLTAGES
============================================================
ESP32-S3 GPIO electrical expectations:
- GPIO logic voltage: 3.3 V nominal
- Typical input HIGH threshold: around 2.0 V and above
- Input LOW threshold: near 0 V
- Absolute maximum GPIO voltage: 3.3 V domain only (no 5 V direct GPIO)

Recommended field wiring practice:
- Digital inputs to ESP32 should present clean 0 V / 3.3 V logic.
- Use pull-ups/pull-downs and debouncing where needed.
- For external 5 V sensors/switches, use proper level shifting or isolation.
- Keep noisy power wiring physically separated from signal wiring.

============================================================
3) ESP32-S3 GPIO MAP USED BY spa_control.py
============================================================

3.1 DIGITAL INPUTS (configured as Pin.IN, Pin.PULL_UP)
------------------------------------------------------------
Signal              GPIO    Function
xSpaEnable          4       Master spa enable command
xPumpRequest        5       Pump request (P1 low unless high selected)
xHeatRequest        6       Heat request
xPump1HighRequest   7       Pump 1 high-speed request
xPump2Request       15      Pump 2 single-speed request
xPump3Request       16      Pump 3 single-speed request
xJetsRequest        17      Jets request
xBlowerRequest      18      Blower request
xLightRequest       8       Light request
xFlowSwitch         9       Flow proof input (TRUE = flow OK)
xHighLimitOK        10      High-limit permissive (TRUE = OK)
xRemoteEStopOK      11      Remote E-stop permissive (TRUE = OK)

Expected voltage at these GPIO inputs:
- LOW state: approximately 0 V
- HIGH state: approximately 3.3 V

Note:
- Inputs are configured with pull-up in code.
- If your field device is active-low, invert in hardware or software as needed.

3.2 DIGITAL OUTPUTS (configured as Pin.OUT)
------------------------------------------------------------
Signal              GPIO    Function
xPump1_Low          12      Pump 1 low-speed output command
xPump1_High         13      Pump 1 high-speed output command
xPump2              14      Pump 2 output command
xPump3              21      Pump 3 output command
xHeater             38      Heater contactor command (5 kW @ 240 Vac load side)
xJets               39      Jets actuator/relay command
xBlower             40      Blower relay command
xLight              41      Light relay command

Expected voltage at these GPIO outputs:
- OFF: approximately 0 V
- ON: approximately 3.3 V

Use-case expectation:
- These outputs should drive relay/driver inputs only.
- Confirm driver module trigger polarity (active-high vs active-low).

============================================================
4) TOUCH DISPLAY PINOUT AND ESP32 MAPPING
============================================================
Display module: Hosyond ST7796S SPI + XPT2046 touch

4.1 SHARED SPI BUS (LCD + TOUCH)
------------------------------------------------------------
Bus Signal   ESP32 GPIO   Notes
SCK          42           SPI clock
MOSI         47           SPI master out
MISO         48           SPI master in

4.2 LCD CONTROL PINS (ST7796S)
------------------------------------------------------------
LCD Signal   ESP32 GPIO   Notes
LCD_CS       2            LCD chip select
LCD_DC       1            Data/command select
LCD_RST      3            LCD reset
LCD_BL       44           Backlight enable/control

4.3 TOUCH CONTROL PINS (XPT2046)
------------------------------------------------------------
Touch Signal ESP32 GPIO   Notes
TOUCH_CS     43           Touch controller chip select
TOUCH_IRQ    45           Touch interrupt input

Note:
- GPIO45 is input-only on ESP32-S3 and suitable for IRQ input.

============================================================
5) HOSYOND MODULE HEADER PINOUT (TYPICAL)
============================================================
Verify with your exact module silk-screen/datasheet.

Display side (ST7796S):
- VCC
- GND
- CS
- RESET
- DC/RS
- SDI (MOSI)
- SCK
- LED (backlight)
- SDO (MISO)

Touch side (XPT2046):
- T_CLK
- T_CS
- T_DIN (MOSI)
- T_DO (MISO)
- T_IRQ

Common wiring correspondence:
- SDI and T_DIN -> ESP32 MOSI
- SDO and T_DO -> ESP32 MISO
- SCK and T_CLK -> ESP32 SCK
- Separate CS lines for LCD and touch

============================================================
6) POWER GUIDANCE
============================================================
ESP32 board:
- Power by USB-C or regulated 5 V input per dev board specification.

Display module:
- Many ST7796S boards accept 5 V at VCC and use on-board regulation.
- Logic interface to ESP32 is still 3.3 V domain; confirm board level-shifting.
- If uncertain, power and signal at 3.3 V logic-compatible levels.

Grounding:
- ESP32 GND and display GND must be common reference.
- Keep high-current relay/contactor grounds and wiring managed to minimize noise.

============================================================
7) RESERVED / CAUTION PINS (ESP32-S3)
============================================================
General caution from current project notes:
- Avoid GPIO0 for normal controls (boot strap behavior).
- Avoid GPIO19/GPIO20 when USB D-/D+ is in use.
- Avoid GPIO45/GPIO46 for general outputs.
- Avoid GPIO26..GPIO37 (commonly tied to flash/PSRAM on modules).

============================================================
8) COMMISSIONING CHECKLIST
============================================================
1. Verify all GPIO numbers against your exact DevKitC-1 pinout.
2. Verify relay driver polarity (active-high or active-low).
3. Confirm flow switch and high-limit are interpreted with correct logic sense.
4. Confirm temperature sensor scaling returns degrees Fahrenheit.
5. Test each output with load disconnected first (relay LED or meter).
6. Confirm spa 20-minute timer and light 60-minute timer behavior.
7. Confirm all fault interlocks trip outputs as expected.

End of file.
