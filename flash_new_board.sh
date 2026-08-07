#!/usr/bin/env bash
# Bring up an ESP32-S3 spa controller.
#
#   ./flash_new_board.sh <PORT> --firmware   # step 1: erase + flash MicroPython
#   (power-cycle the board — unplug/replug — wait ~15 s)
#   ./flash_new_board.sh <PORT>              # step 2: deploy the application files
#
# Two steps because on the DevKitC-1 **native USB**, esptool's RTS reset is a
# no-op: after flashing MicroPython the chip stays in the bootloader until you
# physically power-cycle it. Do that, then run step 2.
#
# File copy uses a single `mpremote resume` session (no per-file reconnects, no
# soft-reset) — a soft-reset detaches native USB and wedges the link.
#
# Loads spa_control.py as compiled spa_main.mpy via main.py (=main_loader.py) —
# the board can't compile the big source at runtime (heap; see boot.py).
set -euo pipefail

PORT="${1:-}"
DO_FW=0; [ "${2:-}" = "--firmware" ] && DO_FW=1
FW="ESP32_GENERIC_S3-20260406-v1.28.0.bin"
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"

[ -n "$PORT" ] || { echo "usage: $0 <PORT> [--firmware]"; exit 1; }
[ -e "$PORT" ] || { echo "error: port $PORT not found"; exit 1; }

if [ "$DO_FW" = 1 ]; then
  [ -f "$FW" ] || { echo "error: firmware $FW missing"; exit 1; }
  echo "==> read-only chip id (gate before erase)"
  python3 -m esptool --chip esp32s3 --port "$PORT" flash_id | grep -iE "Chip is|MAC:" \
    || { echo "not a confirmable ESP32-S3 in download mode — ABORT"; exit 1; }
  echo "==> ERASING + flashing MicroPython on $PORT"
  python3 -m esptool --chip esp32s3 --port "$PORT" erase_flash
  python3 -m esptool --chip esp32s3 --port "$PORT" --baud 460800 write_flash -z 0 "$FW"
  echo
  echo "==> MicroPython flashed. Now POWER-CYCLE the board (unplug/replug USB),"
  echo "    wait ~15 s, then run:  $0 $PORT"
  exit 0
fi

echo "==> compiling spa_main.mpy"
python3 -m mpy_cross -o spa_main.mpy spa_control.py

echo "==> preparing board config (WiFi stripped — provisioned via the app's Wi-Fi wizard)"
# Never bake WiFi creds into a board image: the board must start unprovisioned so
# the app's BLE Wi-Fi wizard can join it to ANY network. Broker/MQTT creds are
# kept so the board can reach the broker once WiFi is set. Deploys config.board.json.
python3 - <<'PY'
import json, os, sys
src = "config.json"
if not os.path.exists(src):
    if os.path.exists("config.example.json"):
        print("  config.json missing — using config.example.json (no real broker creds!)")
        src = "config.example.json"
    else:
        sys.exit("  error: no config.json or config.example.json to deploy")
with open(src) as f:
    cfg = json.load(f)
cfg.pop("_comment", None)
cfg["wifi_ssid"] = ""        # provisioned per-install by the app's Wi-Fi wizard
cfg["wifi_password"] = ""
with open("config.board.json", "w") as f:
    json.dump(cfg, f)
kept = sorted(k for k in cfg if k not in ("wifi_ssid", "wifi_password"))
print("  WiFi blanked; kept keys:", kept)
PY

echo "==> REPL alive check (resume)"
python3 -m mpremote connect "$PORT" resume exec "print('ALIVE')" | grep -q ALIVE \
  || { echo "no REPL — power-cycle the board, wait 15 s, retry"; exit 1; }

echo "==> deploying application files (single resume session)"
python3 -m mpremote connect "$PORT" resume \
  fs cp boot.py        :boot.py     + \
  fs cp main_loader.py :main.py     + \
  fs cp spa_main.mpy   :spa_main.mpy + \
  fs cp mqtt_spa.py    :mqtt_spa.py + \
  fs cp st7796.py      :st7796.py   + \
  fs cp xpt2046.py     :xpt2046.py  + \
  fs cp _tls_buf.py    :_tls_buf.py + \
  fs cp config.board.json :config.json
rm -f config.board.json

echo "==> filesystem on board:"
python3 -m mpremote connect "$PORT" resume fs ls
echo "==> device_id:"
python3 -m mpremote connect "$PORT" resume exec \
  "import machine,ubinascii; print(ubinascii.hexlify(machine.unique_id()).decode())"
echo "==> reboot into the app (USB will detach — that's normal)"
python3 -m mpremote connect "$PORT" resume exec "import machine; machine.reset()" || true
echo "==> done."
