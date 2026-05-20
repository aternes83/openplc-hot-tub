# boot.py — executed before main.py on every power-on / reset.
#
# WiFi MUST connect here, before main.py runs.
# Reason 1 (heap): WiFi stack needs ~80 KB contiguous heap.  After main.py's
#   module-level allocations (~27 KB) the heap is fragmented; at boot.py time
#   it is clean.
# Reason 2 (brownout): WiFi association TX spikes (~300 mA) and the ST7796S
#   display SPI DMA together can drop the USB rail below the brownout threshold
#   (~3.0 V) and reset the MCU.  Completing WiFi association here (before the
#   display is ever initialised) separates the two peak-current events in time.
# Reason 3 (LED inrush): after WiFi connects, we enable power-save mode so
#   the radio sleeps between DTIM beacons.  This reduces idle WiFi current
#   from ~80 mA to ~15 mA, giving headroom for the backlight soft-ramp later.

import gc
gc.collect()

try:
    import utime
    import ujson as json

    _cfg = {}
    try:
        with open("config.json", "r") as _f:
            _loaded = json.loads(_f.read())
        if isinstance(_loaded, dict):
            _cfg = _loaded
    except Exception:
        pass

    _ssid = _cfg.get("wifi_ssid", "")
    _pwd  = _cfg.get("wifi_password", "")

    if _ssid:
        # BLE must be activated before WiFi so ESP-IDF configures BT+WiFi
        # coexistence at startup.  Activating BLE after WiFi fails with EIO.
        _ble_active = False
        try:
            import bluetooth as _bt
            _ble_pre = _bt.BLE()
            if not _ble_pre.active():
                _ble_pre.active(True)
            _ble_active = True
            print("boot: BLE pre-activated, free:", gc.mem_free())
        except Exception as _ble_pre_e:
            print("boot: BLE pre-activate failed (non-fatal):", _ble_pre_e)

        import network
        gc.collect()
        print("boot: pre-WiFi free:", gc.mem_free())

        _wlan = network.WLAN(network.STA_IF)
        try:
            _wlan.config(txpower=8.5)
        except Exception:
            pass
        gc.collect()

        if not _wlan.active():
            _wlan.active(True)
            # 1500 ms: ESP32 WiFi driver is not ready for connect() immediately
            # after active(True) — prevents RuntimeError 0x0102.
            utime.sleep_ms(1500)

        print("boot: WiFi active, status:", _wlan.status(), "free:", gc.mem_free())

        if not _wlan.isconnected():
            try:
                _wlan.connect(_ssid, _pwd)
            except Exception as _ce:
                print("boot: connect exc:", _ce)

            # Wait up to 15 s for association.  If it times out, main.py
            # _wifi_task will retry — this is best-effort, not mandatory.
            _deadline = utime.ticks_add(utime.ticks_ms(), 15_000)
            while not _wlan.isconnected():
                if utime.ticks_diff(_deadline, utime.ticks_ms()) <= 0:
                    break
                utime.sleep_ms(250)

        if _wlan.isconnected():
            print("boot: WiFi CONNECTED ip:", _wlan.ifconfig()[0],
                  "free:", gc.mem_free())
            try:
                # PM_NONE starves BLE of radio time (WiFi wins every arbitration).
                # PM_POWERSAVE creates DTIM sleep windows BLE can use but can
                # cause both to drop if the coexistence scheduler glitches.
                # PM_PERFORMANCE (wake on every beacon, light PS) is the middle
                # ground: WiFi stays responsive but yields radio gaps for BLE.
                _pm = network.WLAN.PM_PERFORMANCE if _ble_active else network.WLAN.PM_POWERSAVE
                _wlan.config(pm=_pm)
                print("boot: WiFi pm=%s" % ("PERFORMANCE (BLE active)" if _ble_active else "POWERSAVE"))
            except Exception:
                pass
            # NTP sync — set RTC to local time using utc_offset_hours from config
            try:
                import ntptime as _ntp
                _ntp.settime()   # sets utime epoch to UTC
                _utc_off = int(_cfg.get("utc_offset_hours", 0))
                if _utc_off:
                    import machine as _mc
                    _t = utime.time() + _utc_off * 3600
                    _tm = utime.localtime(_t)
                    # datetime tuple: (year, month, day, weekday, hour, min, sec, subsec)
                    _mc.RTC().datetime((_tm[0], _tm[1], _tm[2], _tm[6],
                                        _tm[3], _tm[4], _tm[5], 0))
                print("boot: NTP synced, local time %02d:%02d" % (
                    utime.localtime(utime.time() + _utc_off * 3600)[3],
                    utime.localtime(utime.time() + _utc_off * 3600)[4]))
            except Exception as _ntp_e:
                print("boot: NTP failed (non-fatal):", _ntp_e)
        else:
            print("boot: WiFi not connected after 15s (status:",
                  _wlan.status(), ") — main.py will retry")

        del _wlan, network

    del _ssid, _pwd, _cfg, json, utime

except Exception as _boot_e:
    # Never crash in boot.py — a boot exception prevents main.py from running.
    print("boot: WiFi init error (non-fatal):", _boot_e)

gc.collect()
