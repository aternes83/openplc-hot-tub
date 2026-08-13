# OTA Firmware Updates — How-To

The spa controller updates its own software over WiFi (via the MQTT broker), with
an **automatic rollback** if a new build won't run. This guide is for the
maintainer publishing a release.

- **App-confirmed:** the board never installs on its own. It advertises that an
  update is available; the user taps **Install** in the app.
- **Verified:** every file is SHA-256-checked before it's applied.
- **Safe:** the previous version is backed up; if the new build fails to run
  healthy, the board restores it automatically (see [Rollback](#rollback)).

---

## Architecture (30-second version)

```
publish_ota.py ── retained ──►  spa/ota/manifest          (version + file list + sha256)
                                spa/ota/file/<name>/<i>    (each file, in 1 KB chunks)
                                        │  MQTT/TLS
                                   ┌────▼─────┐
   app: {"ota_apply":"X"} ───────►│  ESP32   │ fetch → verify → back up → apply → reboot
                                   └────┬─────┘        └─ boot.py rolls back if it won't run
                                        │
                          status: ota_avail / ota_state
```

Files are sent as **1 KB chunks** streamed to flash (a whole-file MQTT payload
won't fit the heap). Only application files may be shipped — `boot.py`, `main.py`,
and `config.json` are **never** delivered over OTA (they're the recovery path and
the device identity).

---

## Prepare a release

You need `config.json` present (the publisher reads the broker creds from it) and
`paho-mqtt` + `mpy-cross` installed.

1. **Bump the version.** Edit `FIRMWARE_VERSION` in `mqtt_spa.py`:

   ```python
   FIRMWARE_VERSION = "1.3.0"
   ```

   > Always include `mqtt_spa.py` in the release (below) so the board reports the
   > new version after applying and stops advertising the update. If you ship only
   > `spa_main.mpy`, the reported version won't change and the board will keep
   > offering the "update".

2. **Compile** the main module (the board can't compile `.py` at runtime):

   ```bash
   python3 -m mpy_cross -o spa_main.mpy spa_control.py
   ```

3. **Publish** the release — list every file that changed (plus `mqtt_spa.py`):

   ```bash
   python3 publish_ota.py --version 1.3.0 --files spa_main.mpy mqtt_spa.py
   ```

   The tool clears any stale chunks, publishes each file as retained chunks, then
   publishes the manifest last. Typical output:

   ```
     cleared 43 stale retained chunk topic(s)
     published spa_main.mpy: 29 chunk(s), 29323 bytes, sha256 2920f98bb780…
     published mqtt_spa.py: 14 chunk(s), 13640 bytes, sha256 c1c0136b9c8d…
   published manifest v1.3.0 with 2 file(s)
   ```

That's it — the release is live on the broker. Every board on that broker running
an older version will now advertise it.

---

## Install (from the app)

1. The controller sees the manifest and reports `ota_avail: "1.3.0"` in its status.
2. In the app: **Settings → Controller firmware** shows an **Update available**
   badge → tap **Install**.
3. The board downloads + verifies the chunks (`ota_state: fetching`), backs up the
   current files, applies the new ones, and **reboots** (~1 minute; the app shows
   "Spa restarting…"). It comes back on the new version.

The whole thing is unattended after the tap — keep the spa powered.

---

## Rollback

This is the safety net. After applying, the board writes an `ota_pending` marker
and reboots. The new build must **confirm itself healthy** — control loop running
and MQTT connected for ≥ 90 s — which clears the marker and commits the update.

If it can't (crash on boot, hang, or MQTT never connects):

- `boot.py` (which is **never** shipped over OTA, so it can't be broken by a bad
  update) counts boot attempts. After **3** failed attempts it restores the
  backed-up known-good files from `ota_good/` and reboots into them.
- `main_loader.py` reboots immediately if the app raises during a trial boot, so a
  crash counts as an attempt.
- A build that runs but can't confirm within 3 minutes forces a reboot too.

No action needed — a bad update self-heals back to the last working version. It's
logged: `grep "ROLLED BACK" ota.log` on the board shows
`boot: ROLLED BACK failed update v1.3.0 (after 3 attempts)`.

**Always test a new build on a bench board before publishing to the fleet.**

---

## Retract a release

To stop boards advertising an update (e.g. you spotted a problem):

```bash
python3 publish_ota.py --clear
```

This deletes the retained manifest and all file chunks. Boards already updated are
unaffected; boards mid-fetch simply time out and stay on their current version.

---

## Rules & safety

- **Never ship `boot.py`, `main.py`, or `config.json`** — the publisher refuses
  them. `boot.py` is the recovery path; `config.json` holds WiFi/broker creds and
  the sensor calibration.
- **Version is a plain dotted number** (`1.3.0`); the board installs a manifest
  only when its version is strictly newer than what's running.
- **Integrity** is SHA-256 per file; a corrupt/partial download is rejected and
  nothing is applied (the board stays on its current version).
- The transport is the same TLS MQTT connection the app already uses.

---

## Verify / troubleshoot (on the board over USB)

```bash
PORT=$(ls /dev/cu.usbmodem* | head -1)
python3 -m mpremote connect "$PORT" resume exec "print(open('ota.log').read())"
```

`ota.log` shows the whole story: `update available` → `fetching` → `staged … ok`
→ `applied … rebooting` → `update confirmed healthy — committed`, or a
`ROLLED BACK` line if a build failed.

Common cases:

| Symptom | Meaning |
|---|---|
| `ota_state` stuck `fetching`, then `error` | a chunk failed sha256 or the fetch timed out — board stayed on current version (safe) |
| board keeps advertising the same version after install | you didn't include `mqtt_spa.py` with a bumped `FIRMWARE_VERSION` |
| `ROLLED BACK` in `ota.log` | the new build didn't run; board recovered the previous version |
