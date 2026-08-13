# boot.py — runs before main.py on every reset. Two jobs:
#   1) OTA rollback manager (must run first; imports NO app code — this is why
#      boot.py itself is never delivered over OTA).
#   2) Bring up the radios: BLE (before WiFi, for BT/WiFi coexistence) → WiFi →
#      NTP time. MQTT is connected later by mqtt_spa from the main loop.
#
# WiFi connects here (not in main.py) so its TX spike is separated in time from the
# display SPI DMA — together they can brown out the USB rail. The board runs the
# SPIRAM build (~8 MB heap), so the old heap-fragmentation workarounds — pre-
# connecting TLS in boot, deleting every temporary before the handshake, the
# _tls_buf handoff — are gone. Never crash here: a boot exception would stop main.py.

import gc
gc.collect()

# ── OTA rollback manager ──────────────────────────────────────────────────────
# Runs before anything else and imports NO application code — this is why boot.py
# itself is never delivered over OTA. If an applied update hasn't confirmed itself
# healthy (ota.confirm() clears the marker) within OTA_MAX_BOOT_ATTEMPTS boots,
# restore the backed-up known-good files from ota_good/ and reboot. The hardware
# watchdog reboots a hung build, so a crash/hang loop is counted here and rolled
# back automatically. Must never raise.
OTA_MAX_BOOT_ATTEMPTS = 3
try:
    import ujson as _oj, uos as _oos

    def _o_exists(_p):
        try:
            _oos.stat(_p); return True
        except Exception:
            return False

    if _o_exists("ota_pending"):
        with open("ota_pending") as _pf:
            _pend = _oj.loads(_pf.read())
        _att = int(_pend.get("attempts", 0)) + 1
        _pend["attempts"] = _att
        with open("ota_pending", "w") as _pf:
            _oj.dump(_pend, _pf)
        print("boot: ota_pending v%s attempt %d" % (_pend.get("version", "?"), _att))
        if _att > OTA_MAX_BOOT_ATTEMPTS:
            print("boot: OTA update failed to confirm — ROLLING BACK")

            def _o_copy(_s, _d):
                with open(_s, "rb") as _sf:
                    with open(_d, "wb") as _df:
                        while True:
                            _b = _sf.read(512)
                            if not _b:
                                break
                            _df.write(_b)

            _existed = _pend.get("existed", {})
            for _nm in _pend.get("files", []):
                _bak = "ota_good/" + _nm
                try:
                    if _existed.get(_nm, True) and _o_exists(_bak):
                        _o_copy(_bak, _nm)                 # restore known-good
                    elif not _existed.get(_nm, True):
                        try:
                            _oos.remove(_nm)               # was newly added → drop
                        except Exception:
                            pass
                except Exception as _re:
                    print("boot: rollback err", _nm, _re)
            # clear markers
            try:
                _oos.remove("ota_pending")
            except Exception:
                pass
            try:
                for _f in _oos.listdir("ota_good"):
                    try:
                        _oos.remove("ota_good/" + _f)
                    except Exception:
                        pass
                _oos.rmdir("ota_good")
            except Exception:
                pass
            # Record the rollback so it's verifiable after the fact (boot.py can't
            # import ota.py — that's a file OTA may have just reverted).
            try:
                with open("ota.log", "a") as _rf:
                    _rf.write("boot: ROLLED BACK failed update v%s (after %d attempts)\n"
                              % (_pend.get("version", "?"), _att - 1))
            except Exception:
                pass
            print("boot: rollback complete — rebooting into known-good")
            import machine as _om
            _om.reset()
    del _oj, _oos
except Exception as _oe:
    print("boot: OTA manager error (non-fatal):", _oe)

gc.collect()

# ── Radios: BLE (coexistence) → WiFi → NTP ────────────────────────────────────
try:
    import utime, ujson as json

    try:
        with open("config.json") as _f:
            _cfg = json.loads(_f.read())
    except Exception:
        _cfg = {}
    _ssid = _cfg.get("wifi_ssid", "")
    _pwd  = _cfg.get("wifi_password", "")

    if _ssid:
        # BLE must be activated before WiFi so ESP-IDF configures BT/WiFi
        # coexistence at startup (activating BLE after WiFi fails with EIO).
        try:
            import bluetooth
            _ble = bluetooth.BLE()
            if not _ble.active():
                _ble.active(True)
        except Exception as _e:
            print("boot: BLE pre-activate failed (non-fatal):", _e)

        import network
        _wlan = network.WLAN(network.STA_IF)
        try:
            _wlan.config(txpower=8.5)     # trim TX spike — USB-rail brownout guard
        except Exception:
            pass
        if not _wlan.active():
            _wlan.active(True)
            utime.sleep_ms(1500)          # driver not ready for connect() immediately

        if not _wlan.isconnected():
            try:
                _wlan.connect(_ssid, _pwd)
            except Exception as _e:
                print("boot: connect exc:", _e)
            _deadline = utime.ticks_add(utime.ticks_ms(), 15_000)   # best-effort; main.py retries
            while not _wlan.isconnected() and utime.ticks_diff(_deadline, utime.ticks_ms()) > 0:
                utime.sleep_ms(250)

        if _wlan.isconnected():
            print("boot: WiFi connected", _wlan.ifconfig()[0])
            try:
                _wlan.config(pm=network.WLAN.PM_PERFORMANCE)
            except Exception:
                pass

            # NTP → local time in the RTC (US DST aware). main.py's schedule and
            # weekly reboot read this clock.
            try:
                import ntptime
                ntptime.settime()          # RTC/epoch to UTC
                _off = int(_cfg.get("utc_offset_hours", 0))

                def _us_dst(y, mo, d, h):  # +1h from 2nd Sun Mar 02:00 to 1st Sun Nov 02:00
                    if mo < 3 or mo > 11: return 0
                    if 3 < mo < 11:       return 1
                    _t2 = [0,3,2,5,0,3,5,1,4,6,2,4]
                    _yr = y - (mo < 3)
                    _dow = (_yr+_yr//4-_yr//100+_yr//400+_t2[mo-1]+1) % 7  # 0=Sun
                    if mo == 3:
                        _s2 = 1 + (7 - _dow) % 7 + 7
                        return 1 if (d > _s2 or (d == _s2 and h >= 2)) else 0
                    _s1 = 1 + (7 - _dow) % 7
                    return 0 if (d > _s1 or (d == _s1 and h >= 2)) else 1

                _u = utime.localtime(utime.time())
                _off += _us_dst(_u[0], _u[1], _u[2], _u[3])
                import machine
                _tm = utime.localtime(utime.time() + _off * 3600)
                machine.RTC().datetime((_tm[0], _tm[1], _tm[2], _tm[6],
                                        _tm[3], _tm[4], _tm[5], 0))
                print("boot: NTP synced, local %02d:%02d (UTC%+d)" % (_tm[3], _tm[4], _off))
            except Exception as _e:
                print("boot: NTP failed (non-fatal):", _e)
        else:
            print("boot: WiFi not connected — main.py will retry")

except Exception as _boot_e:
    print("boot: init error (non-fatal):", _boot_e)

gc.collect()
