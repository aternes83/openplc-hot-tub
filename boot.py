# boot.py — executed before main.py on every power-on / reset.
#
# Order: BLE → WiFi → NTP → cleanup everything → MQTT TLS
#
# MQTT TLS is intentionally last so the heap is maximally clean before
# the mbedTLS handshake.  Reason: the TLS handshake fragments the heap
# (many alloc/free cycles during certificate validation).  If we run it
# with other temporaries alive (BLE objects, NTP socket, ntptime module
# refs, etc.) the heap ends up so fragmented that main.py cannot compile.
# By deleting all temporaries *before* the handshake we give mbedTLS the
# largest possible contiguous run.  After connect() the persistent TLS
# context (~8-12 KB) is at one end of the heap; the rest is contiguous
# and available for main.py compilation.
#
# WiFi MUST connect here (not in main.py) for two reasons:
#   1. WiFi stack needs ~80 KB contiguous heap; main.py's allocs fragment
#      it first.
#   2. WiFi TX spike + display SPI DMA together can brownout the USB rail;
#      separating them in time prevents MCU resets.
#
# MQTT TLS pre-connect is here (not in main.py) because the TLS handshake
# needs ~20 KB contiguous heap; after main.py initialises the display and
# BLE the heap is fragmented and every TLS attempt fails with ENOMEM.
# The live MQTTClient is stored in _tls_buf.cl; mqtt_spa.setup() picks it
# up, wires the real callback, and subscribes.

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

    _ssid      = _cfg.get("wifi_ssid", "")
    _pwd       = _cfg.get("wifi_password", "")
    # Extract MQTT creds now so we can delete _cfg before the TLS handshake.
    _mhost     = _cfg.get("mqtt_host", "")
    _mport     = int(_cfg.get("mqtt_port", 1883))
    _muser     = _cfg.get("mqtt_user", "") or None
    _mpwd      = _cfg.get("mqtt_password", "") or None

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
            _pm = network.WLAN.PM_PERFORMANCE if _ble_active else network.WLAN.PM_POWERSAVE
            try:
                _wlan.config(pm=_pm)
                print("boot: WiFi pm=%s" % ("PERFORMANCE (BLE active)" if _ble_active else "POWERSAVE"))
            except Exception:
                pass

            # NTP sync — do this BEFORE MQTT so its temporary socket/buffers
            # are freed before the TLS handshake fragments the heap.
            try:
                import ntptime as _ntp
                _ntp.settime()   # sets utime epoch to UTC

                _utc_off = int(_cfg.get("utc_offset_hours", 0))

                # Auto-apply US DST: +1 hour from 2nd Sun Mar 02:00
                #                             to 1st Sun Nov 02:00
                def _us_dst(y, mo, d, h):
                    if mo < 3 or mo > 11: return 0
                    if 3 < mo < 11:       return 1
                    _t2 = [0,3,2,5,0,3,5,1,4,6,2,4]
                    _yr = y - (mo < 3)
                    _dow = (_yr+_yr//4-_yr//100+_yr//400+_t2[mo-1]+1) % 7  # 0=Sun
                    if mo == 3:
                        _sun2 = 1 + (7 - _dow) % 7 + 7
                        return 1 if (d > _sun2 or (d == _sun2 and h >= 2)) else 0
                    _sun1 = 1 + (7 - _dow) % 7
                    return 0 if (d > _sun1 or (d == _sun1 and h >= 2)) else 1

                _utc_tm = utime.localtime(utime.time())
                _utc_off += _us_dst(_utc_tm[0], _utc_tm[1], _utc_tm[2], _utc_tm[3])

                import machine as _mc
                _t = utime.time() + _utc_off * 3600
                _tm = utime.localtime(_t)
                # datetime tuple: (year, month, day, weekday, hour, min, sec, subsec)
                _mc.RTC().datetime((_tm[0], _tm[1], _tm[2], _tm[6],
                                    _tm[3], _tm[4], _tm[5], 0))
                print("boot: NTP synced, local time %02d:%02d (UTC%+d)" % (
                    _tm[3], _tm[4], _utc_off))
            except Exception as _ntp_e:
                print("boot: NTP failed (non-fatal):", _ntp_e)

            # ── Maximise contiguous free heap before TLS handshake ────────
            # Delete every Python-layer temporary.  The GC can then reclaim
            # all unreferenced objects, leaving the heap as clean as possible
            # for mbedTLS's alloc/free storm during the TLS handshake.
            try: del _ble_pre, _bt, _ble_active
            except Exception: pass
            try: del _ntp, _mc, _utc_off, _utc_tm, _t, _tm, _us_dst
            except Exception: pass
            try: del _pm, _deadline
            except Exception: pass
            del _wlan, network
            del _ssid, _pwd, _cfg, json, utime
            gc.collect()
            gc.collect()
            gc.collect()
            print("boot: pre-MQTT free:", gc.mem_free())

            # MQTT TLS pre-connect — heap is maximally clean here.
            # We only call connect() here.  set_callback + subscribe happen
            # in mqtt_spa.setup() after main.py wires the real _on_msg cb.
            _mcl = None
            try:
                if _mhost:
                    from umqtt.simple import MQTTClient as _MQC
                    import ubinascii as _ubi, machine as _mc2
                    _cid = b"spa-" + _ubi.hexlify(_mc2.unique_id())
                    _use_ssl = (_mport == 8883)
                    _ssl_p = {"server_hostname": _mhost} if _use_ssl else {}
                    if _use_ssl:
                        import network as _net2
                        _wlan2 = _net2.WLAN(_net2.STA_IF)
                        _wlan2.config(pm=_net2.WLAN.PM_NONE)
                        import utime as _ut2
                        _ut2.sleep_ms(500)
                        del _ut2
                    _mcl = _MQC(
                        _cid, _mhost, port=_mport,
                        user=_muser, password=_mpwd,
                        ssl=_use_ssl, ssl_params=_ssl_p, keepalive=120,
                    )
                    _mcl.connect()
                    if _use_ssl:
                        _wlan2.config(pm=_net2.WLAN.PM_PERFORMANCE)
                        del _wlan2, _net2
                    import _tls_buf
                    _tls_buf.cl = _mcl
                    _mcl = None   # ownership transferred to _tls_buf.cl
                    del _MQC, _ubi, _mc2, _cid, _use_ssl, _ssl_p
                    gc.collect()
                    print("boot: MQTT connected free:", gc.mem_free())
            except Exception as _me:
                print("boot: MQTT skipped:", _me)
                if _mcl is not None:
                    try: _mcl.disconnect()
                    except Exception: pass
                    _mcl = None
                # Restore PM if SSL path set PM_NONE and then failed
                try:
                    import network as _net3
                    _net3.WLAN(_net3.STA_IF).config(pm=_net3.WLAN.PM_PERFORMANCE)
                    del _net3
                except Exception:
                    pass
                gc.collect()

            del _mhost, _mport, _muser, _mpwd

        else:
            print("boot: WiFi not connected after 15s (status:",
                  _wlan.status(), ") — main.py will retry")
            del _wlan, network
            del _ssid, _pwd, _cfg, json, utime
            del _mhost, _mport, _muser, _mpwd

    else:
        del _ssid, _pwd, _cfg, json, utime
        del _mhost, _mport, _muser, _mpwd

except Exception as _boot_e:
    # Never crash in boot.py — a boot exception prevents main.py from running.
    print("boot: WiFi init error (non-fatal):", _boot_e)

gc.collect()
