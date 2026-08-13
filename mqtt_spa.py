"""
mqtt_spa.py — self-contained MQTT integration (MicroPython v1.28, ESP32-S3).

spa_control.py calls only two functions:
  mqtt_spa.setup(host, port, user, pwd, cmd_handler)   -- once at startup
  mqtt_spa.tick(inputs, outputs, ctrl, ui_state, wifi) -- every loop iteration

All state (client, timers, rx buffer) lives here. Never raises.
"""

import gc

# Reported to the app in spa/status so it can flag firmware updates.
FIRMWARE_VERSION = "1.1.0"

_LOG_FILE  = "mqtt.log"
_LOG_LINES = 30

# ── module-level state ────────────────────────────────────────────────────────
_host    = ""
_port    = 1883
_user    = ""
_pwd     = ""
_cmd_cb  = None      # callable(payload_bytes) — called for each inbound message
_cl      = None      # MQTTClient or None when disconnected
_rx_buf  = []        # inbound payloads awaiting dispatch
_ready   = False     # True after setup() called with a non-empty host

# Per-device topics for multi-tenant operation (many spas / users on one broker).
# device_id defaults to the board's hardware id; empty falls back to legacy topics.
_device_id    = ""
_status_topic = b"spa/status"
_cmd_topic    = b"spa/commands"

# Timer state — None means "not yet initialised, use first-connect delay"
_connect_ms = None   # last connect attempt ticks
_check_ms   = 0
_pub_ms     = 0
_last_sig   = None   # last published control-state signature (publish-on-change)

_CONNECT_INTERVAL_MS =  60_000   # retry after failed connect
_FIRST_CONNECT_DELAY =  30_000   # initial connect delay after WiFi up
_CHECK_INTERVAL_MS   =   1_000
_PUB_INTERVAL_MS     =  30_000   # heartbeat republish interval
_CHANGE_MIN_MS       =     250   # min gap between change-triggered publishes


# ── rolling log ───────────────────────────────────────────────────────────────

def _log(msg):
    try:
        try:
            import utime as _t
            _tm = _t.localtime()
        except Exception:
            import time as _t
            _tm = _t.localtime()
        ts   = "%02d:%02d:%02d" % (_tm[3], _tm[4], _tm[5])
        line = "%s %s\n" % (ts, msg)
        try:
            with open(_LOG_FILE, "r") as _f:
                _lines = _f.readlines()
        except Exception:
            _lines = []
        _lines = (_lines + [line])[-_LOG_LINES:]
        with open(_LOG_FILE, "w") as _f:
            _f.write("".join(_lines))
    except Exception:
        pass


# ── public API ────────────────────────────────────────────────────────────────

def setup(host, port, user, pwd, cmd_handler, device_id=""):
    """
    Configure MQTT credentials and command callback.  Call once at startup.

    cmd_handler(payload_bytes) is called for each message on the commands topic.
    With a device_id the topics are spa/<id>/status and spa/<id>/commands
    (multi-tenant); empty keeps the legacy spa/status and spa/commands.
    Calling setup() resets the client so the next tick() reconnects fresh.
    """
    global _host, _port, _user, _pwd, _cmd_cb
    global _ready, _cl, _connect_ms, _check_ms, _pub_ms
    global _device_id, _status_topic, _cmd_topic
    _host, _port, _user, _pwd, _cmd_cb = host, int(port), user, pwd, cmd_handler
    _device_id = device_id or ""
    if _device_id:
        _status_topic = ("spa/%s/status" % _device_id).encode()
        _cmd_topic    = ("spa/%s/commands" % _device_id).encode()
    else:
        _status_topic = b"spa/status"
        _cmd_topic    = b"spa/commands"
    _ready      = bool(host)
    _cl         = None
    _connect_ms = None   # triggers first-connect delay logic in tick()
    _check_ms   = 0
    _pub_ms     = 0
    if _ready:
        # MQTT connects lazily in tick() → _do_connect (no boot.py pre-connect any
        # more — the SPIRAM heap makes it unnecessary). Wire the OTA module here.
        try:
            import ota
            ota.init(FIRMWARE_VERSION, _ota_subscribe)
        except Exception:
            pass
        _log("setup host=%s port=%d" % (host, port))


def connected():
    """True when an MQTT session is live (used to gate the OTA health confirm)."""
    return _cl is not None


def tick(inputs, outputs, ctrl, ui_state, wifi_connected):
    """
    Drive MQTT connect / check / publish.  Call every main-loop iteration.

    wifi_connected: bool — pass _wlan.isconnected() from spa_control.py.
    Never raises.
    """
    global _cl, _connect_ms, _check_ms, _pub_ms, _last_sig

    if not _ready or not wifi_connected:
        return

    try:
        import utime as _t
    except Exception:
        import time as _t

    now = _t.ticks_ms()

    # ── connection management ─────────────────────────────────────────────────
    if _cl is None:
        if _connect_ms is None:
            # First call after setup(): schedule initial connect after 30 s.
            # ticks_add handles 32-bit wrap correctly; pure Python subtraction
            # produces a negative int that breaks ticks_diff.
            _connect_ms = _t.ticks_add(now, -(_CONNECT_INTERVAL_MS - _FIRST_CONNECT_DELAY))
        if _t.ticks_diff(now, _connect_ms) >= _CONNECT_INTERVAL_MS:
            _connect_ms = now
            _do_connect(_t)
            _check_ms = now
            _pub_ms   = now
        return   # skip check/pub until connected

    # ── check inbound messages ────────────────────────────────────────────────
    if _t.ticks_diff(now, _check_ms) >= _CHECK_INTERVAL_MS:
        _check_ms = now
        try:
            _cl.check_msg()
        except Exception as e:
            _log("check failed: %s" % e)
            _cl = None
            _connect_ms = now
            return

    # During an OTA fetch, drain queued chunk messages fast (they arrive in a burst
    # after the wildcard subscribe). Dispatch + gc.collect after EACH message so only
    # one ~1 KB chunk is ever resident — the heap is tiny and can't hold a backlog.
    try:
        import ota
        if ota.state() == "fetching":
            for _ in range(60):
                _cl.check_msg()
                while _rx_buf:
                    _t2, _p2 = _rx_buf.pop(0)
                    _route(_t2, _p2)
                gc.collect()
    except Exception as e:
        _log("ota drain: %s" % e)
        _cl = None
        _connect_ms = now
        return

    # ── publish: immediately on control-state change, else 30 s heartbeat ──────
    if _cl is not None:
        sig = _status_signature(outputs, ctrl, ui_state)
        due_change = (sig != _last_sig) and _t.ticks_diff(now, _pub_ms) >= _CHANGE_MIN_MS
        due_beat   = _t.ticks_diff(now, _pub_ms) >= _PUB_INTERVAL_MS
        if due_change or due_beat:
            _last_sig = sig
            _pub_ms = now
            _do_publish(inputs, outputs, ctrl, ui_state)

    # ── dispatch inbound messages (commands + OTA) ────────────────────────────
    while _rx_buf:
        try:
            topic, payload = _rx_buf.pop(0)
            _route(topic, payload)
        except Exception:
            pass


# ── internal helpers ──────────────────────────────────────────────────────────

def _on_msg(topic, payload):
    # Buffer (topic, payload) so tick() can route commands vs OTA off the MQTT
    # callback stack. Copy both — umqtt reuses its receive buffers.
    try:
        _rx_buf.append((bytes(topic), bytes(payload)))
    except Exception:
        pass


def _route(topic, payload):
    """Dispatch a buffered message by topic: OTA manifest/file → ota, else the
    normal command handler."""
    try:
        import ota
        if topic == ota.MANIFEST_TOPIC:
            ota.on_manifest(payload)
            return
        if topic.startswith(ota.FILE_TOPIC_PREFIX):
            ota.on_file(topic, payload)
            return
    except Exception:
        pass
    if _cmd_cb:
        _cmd_cb(payload)


def _ota_subscribe(topic):
    """Subscribe callback handed to ota.py so it can pull file topics on demand."""
    try:
        if _cl is not None:
            _cl.subscribe(topic)
    except Exception:
        pass


def _subscribe_ota():
    """Subscribe to the retained OTA manifest so the board learns about updates."""
    try:
        import ota
        if _cl is not None:
            _cl.subscribe(ota.MANIFEST_TOPIC)
    except Exception:
        pass


def _ota_status():
    """(ota_avail_version_or_None, ota_state) for the status publish."""
    try:
        import ota
        return ota.available(), ota.state()
    except Exception:
        return None, "idle"


def _do_connect(_t):
    global _cl
    gc.collect()
    free = gc.mem_free()
    _log("connect host=%s port=%d free=%d" % (_host, _port, free))

    use_ssl   = (_port == 8883)
    _pm_saved = None

    if use_ssl and free < 20_000:
        _log("skip TLS low mem=%d" % free)
        return

    import network as _net
    _wlan = _net.WLAN(_net.STA_IF)

    try:
        if use_ssl:
            try:
                _pm_saved = _wlan.config("pm")
                _wlan.config(pm=_net.WLAN.PM_NONE)
                _t.sleep_ms(500)   # let PM_NONE take effect before TLS handshake
            except Exception as e:
                _log("PM warn: %s" % e)

        from umqtt.simple import MQTTClient
        import ubinascii
        import machine as _m

        cid        = b"spa-" + ubinascii.hexlify(_m.unique_id())
        ssl_params = {"server_hostname": _host} if use_ssl else {}

        cl = MQTTClient(
            cid, _host,
            port=_port,
            user=_user if _user else None,
            password=_pwd if _pwd else None,
            ssl=use_ssl,
            ssl_params=ssl_params,
            keepalive=120,
        )
        cl.set_callback(_on_msg)
        cl.connect()
        cl.subscribe(_cmd_topic)
        gc.collect()
        _cl = cl
        _subscribe_ota()
        _log("connected free=%d" % gc.mem_free())

    except Exception as e:
        _log("connect failed: %s" % e)
        _cl = None

    finally:
        if _pm_saved is not None:
            try:
                _wlan.config(pm=_pm_saved)
            except Exception as e:
                _log("PM restore warn: %s" % e)


def _status_signature(outputs, ctrl, ui_state):
    """Control-state fields that trigger an immediate publish when they change.
    Water temp is intentionally excluded so its continuous drift doesn't spam
    publishes — the 30 s heartbeat carries fresh temperature."""
    return (
        round(ctrl.temp_setpoint_f, 1),
        bool(outputs.get("xHeater")),
        bool(outputs.get("xPump1_Low")),
        bool(outputs.get("xPump1_High")),
        bool(outputs.get("xPump2")),
        bool(outputs.get("xPump3")),
        bool(ui_state.get("xLightRequest", False)),
        bool(ui_state.get("eco_mode", False)),
        bool(ui_state.get("max_jet_on", False)),
        bool((ui_state.get("schedule") or {}).get("enabled", False)),
        bool(ui_state.get("_sched_active", False)),
        bool(outputs.get("xFault")),
        int(outputs.get("iFaultCode", 0)),
        _ota_status(),   # (avail_version, state) — publish promptly when it changes
    )


def _do_publish(inputs, outputs, ctrl, ui_state):
    global _cl
    try:
        import ujson as _j
        _ro = inputs.get("rWaterOhms")
        _ota_avail, _ota_state = _ota_status()
        msg = _j.dumps({
            "id":         _device_id,
            "temp_f":     round(inputs.get("rWaterTemp_F", 0.0), 1),
            # Live NTC probe resistance (Ω) for the app's calibration wizard; None
            # for non-analog sensors or before the first read.
            "r_ohms":     (int(_ro) if isinstance(_ro, (int, float)) else None),
            "setpoint":   round(ctrl.temp_setpoint_f, 1),
            "heater":     bool(outputs.get("xHeater")),
            # Publish ACTUAL pump outputs (not the user's setting) so the app
            # stays honest like the LCD — e.g. MAX JET forcing all jets high, or
            # the heater forcing Jet 1 low.
            "pump1":      (2 if outputs.get("xPump1_High") else
                           (1 if outputs.get("xPump1_Low") else 0)),
            "pump2":      bool(outputs.get("xPump2")),
            "pump3":      bool(outputs.get("xPump3")),
            "light":      bool(ui_state.get("xLightRequest", False)),
            "eco":        bool(ui_state.get("eco_mode", False)),
            "max_jet":    bool(ui_state.get("max_jet_on", False)),
            "schedule_on":     bool((ui_state.get("schedule") or {}).get("enabled", False)),
            "schedule_active": bool(ui_state.get("_sched_active", False)),
            "fault":      bool(outputs.get("xFault")),
            "fault_code": int(outputs.get("iFaultCode", 0)),
            "fw":         FIRMWARE_VERSION,
            # OTA: newer version available (or null) + current updater state.
            "ota_avail":  _ota_avail,
            "ota_state":  _ota_state,
        })
        _cl.publish(_status_topic, msg.encode(), retain=True, qos=0)
        _log("pub OK")
    except Exception as e:
        _log("pub failed: %s" % e)
        _cl = None
