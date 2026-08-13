"""
ota.py — MQTT-delivered firmware OTA with A/B rollback (MicroPython, ESP32-S3).

App-confirmed flow:
  * The board subscribes to `spa/ota/manifest` (retained). on_manifest() parses it;
    if the manifest version is newer than the running firmware, available()
    returns it and it's advertised in spa/<id>/status as `ota_avail`.
  * The app sends {"ota_apply": "<version>"} → spa_control calls start_apply(). We
    subscribe to each file's topic `spa/ota/file/<name>` (retained, raw bytes),
    stream it to ota_new/<name>, and verify its sha256 against the manifest.
  * When every file is received + verified, apply(): back up the current copies to
    ota_good/, move the new files into place, write the `ota_pending` marker, and
    machine.reset().
  * boot.py (stable, NEVER delivered via OTA) counts boot attempts; if the new
    build doesn't confirm healthy within OTA_MAX_BOOT_ATTEMPTS it restores
    ota_good/ and reboots — the automatic rollback.
  * On a healthy boot the running app calls confirm() (loop + MQTT up ≥ commit
    window), which clears the markers and makes the new version the baseline.

Only application files are OTA-managed — never boot.py / main.py / config.json.
Never raises out to a caller.
"""

import uos as os
import ujson as json
import uhashlib
import ubinascii

try:
    from utime import ticks_ms, ticks_diff, ticks_add
except Exception:                     # desktop stub for compile/lint only
    def ticks_ms():        return 0
    def ticks_diff(a, b):  return a - b
    def ticks_add(a, b):   return a + b

MANIFEST_TOPIC    = b"spa/ota/manifest"
FILE_TOPIC_PREFIX = b"spa/ota/file/"

PENDING  = "ota_pending"
GOOD_DIR = "ota_good"
NEW_DIR  = "ota_new"

FETCH_TIMEOUT_MS = 120000
_LOG_FILE  = "ota.log"
_LOG_LINES = 25

# Never let an update replace the recovery path or the device identity.
PROTECTED = ("boot.py", "main.py", "config.json")

# ── module state ──────────────────────────────────────────────────────────────
# Files are delivered as small (CHUNK_SIZE) messages, not whole — the ESP32-S3
# heap can't allocate a contiguous multi-KB buffer for a large MQTT payload while
# the display/BLE/TLS are up. Chunks are streamed to flash at their byte offset
# (seek), so only one chunk is ever in RAM; the reassembled file is verified by
# sha256.
_cur_version  = "0.0.0"
_manifest     = None      # dict of the latest retained manifest
_avail        = None      # version string when a newer update is offered
_state        = "idle"    # idle | fetching | applying | error
_error        = ""
_needed       = None      # {name: {"sha256": hex, "size": n, "chunks": k}}
_chunk_size   = 1024
_got_idx      = None       # {name: set(received chunk indices)}
_done         = None       # set() of fully-received + verified file names
_deadline     = 0
_subscribe_cb = None      # callable(topic_bytes) — set by mqtt_spa


# ── logging ───────────────────────────────────────────────────────────────────
def _log(msg):
    try:
        line = msg + "\n"
        try:
            with open(_LOG_FILE) as f:
                lines = f.readlines()
        except Exception:
            lines = []
        with open(_LOG_FILE, "w") as f:
            f.write("".join((lines + [line])[-_LOG_LINES:]))
    except Exception:
        pass


# ── small fs / hashing helpers ────────────────────────────────────────────────
def _exists(p):
    try:
        os.stat(p)
        return True
    except Exception:
        return False


def _mkdir(d):
    try:
        os.mkdir(d)
    except Exception:
        pass


def _rmtree(d):
    try:
        for f in os.listdir(d):
            try:
                os.remove(d + "/" + f)
            except Exception:
                pass
        os.rmdir(d)
    except Exception:
        pass


def _copy(src, dst):
    with open(src, "rb") as s:
        with open(dst, "wb") as d:
            while True:
                b = s.read(512)
                if not b:
                    break
                d.write(b)


def _sha256_file(path):
    h = uhashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(512)
            if not b:
                break
            h.update(b)
    return ubinascii.hexlify(h.digest()).decode()


def _ver_tuple(s):
    out = []
    for part in str(s).split("."):
        try:
            out.append(int(part))
        except Exception:
            out.append(0)
    return tuple(out)


def _newer(a, b):
    """True if version a is strictly newer than b."""
    return _ver_tuple(a) > _ver_tuple(b)


# ── public API ────────────────────────────────────────────────────────────────
def init(cur_version, subscribe_cb):
    """Wire the running firmware version + a topic-subscribe callback (from
    mqtt_spa). Safe to call again on reconnect."""
    global _cur_version, _subscribe_cb
    _cur_version = cur_version or "0.0.0"
    _subscribe_cb = subscribe_cb


def on_manifest(payload):
    """Handle a retained manifest message. Flags an update when it is newer than
    the running version (and not while an apply is already in progress)."""
    global _manifest, _avail
    if _state in ("fetching", "applying"):
        return
    try:
        m = json.loads(payload)
        if not isinstance(m, dict):
            return
        ver = str(m.get("version", ""))
        files = m.get("files") or {}
        if not ver or not files:
            return
        # Refuse manifests that try to touch protected files.
        for name in files:
            if name in PROTECTED or "/" in name:
                _log("ota: manifest rejects protected/invalid file %s" % name)
                return
        _manifest = m
        _avail = ver if _newer(ver, _cur_version) else None
        if _avail:
            _log("ota: update available %s (running %s)" % (_avail, _cur_version))
    except Exception as e:
        _log("ota: manifest err %s" % e)


def start_apply(version):
    """Begin fetching the files for `version`. Returns True if the fetch started.
    Subscribes to each file's chunk wildcard `spa/ota/file/<name>/+`."""
    global _state, _needed, _chunk_size, _got_idx, _done, _deadline, _error
    if _avail is None or _manifest is None or str(version) != _avail:
        _error = "no such update"
        _log("ota: apply rejected (want %s, have %s)" % (version, _avail))
        return False
    files = _manifest.get("files") or {}
    if not files:
        _error = "empty manifest"
        return False
    _rmtree(NEW_DIR)
    _mkdir(NEW_DIR)
    _needed = files
    _chunk_size = int(_manifest.get("chunk_size", 1024))
    _got_idx = {}
    _done = set()
    _error = ""
    _state = "fetching"
    _deadline = ticks_add(ticks_ms(), FETCH_TIMEOUT_MS)
    for name in files:
        _got_idx[name] = set()
        try:
            with open(NEW_DIR + "/" + name, "wb"):   # pre-create empty staging file
                pass
        except Exception:
            pass
        if _subscribe_cb:
            try:
                _subscribe_cb(FILE_TOPIC_PREFIX + name.encode() + b"/+")
            except Exception:
                pass
    _log("ota: fetching %d file(s) for %s (chunk=%d)" % (len(files), version, _chunk_size))
    return True


def on_file(topic, payload):
    """Handle a retained chunk `spa/ota/file/<name>/<index>`: write it at its byte
    offset in the staging file. When a file's chunks are all present, verify sha256."""
    global _done
    if _state != "fetching" or _needed is None:
        return
    try:
        rest = topic[len(FILE_TOPIC_PREFIX):]         # b"<name>/<index>"
        cut = rest.rfind(b"/")
        if cut < 0:
            return
        name = rest[:cut].decode()
        idx = int(rest[cut + 1:])
    except Exception:
        return
    if name not in _needed or name in _done:
        return
    if idx < 0 or idx >= int(_needed[name].get("chunks", 0)):
        return                                        # stale/out-of-range chunk
    seen = _got_idx.get(name)
    if seen is None or idx in seen:
        return
    try:
        path = NEW_DIR + "/" + name
        with open(path, "r+b") as f:
            f.seek(idx * _chunk_size)
            f.write(payload)
        seen.add(idx)
        if len(seen) >= int(_needed[name].get("chunks", 0)):
            want = str(_needed[name].get("sha256", "")).lower()
            if want and _sha256_file(path) == want:
                _done.add(name)
                _log("ota: staged %s ok (%d/%d files)" % (name, len(_done), len(_needed)))
            else:
                _log("ota: sha256 mismatch on %s — aborting" % name)
                _fail("checksum mismatch: %s" % name)
    except Exception as e:
        _log("ota: chunk err %s %s" % (name, e))


def poll(now_ms):
    """Drive the fetch→apply state machine. Call from the main loop."""
    if _state != "fetching":
        return
    if _needed is not None and _done is not None and len(_done) == len(_needed):
        _apply()
    elif ticks_diff(now_ms, _deadline) >= 0:
        _fail("fetch timeout")


def _apply():
    """Back up current files → ota_good/, move staged files into place, write the
    ota_pending marker, and reboot. On any error we stay on the old code (safe)."""
    global _state
    _state = "applying"
    try:
        names = list(_needed.keys())
        _rmtree(GOOD_DIR)
        _mkdir(GOOD_DIR)
        existed = {}
        for name in names:
            existed[name] = _exists(name)
            if existed[name]:
                _copy(name, GOOD_DIR + "/" + name)     # snapshot current
        # Marker written BEFORE we overwrite, so a mid-apply crash still rolls back.
        with open(PENDING, "w") as f:
            json.dump({"version": _avail, "attempts": 0,
                       "files": names, "existed": existed}, f)
        for name in names:                             # move staged → active
            src = NEW_DIR + "/" + name
            try:
                os.remove(name)
            except Exception:
                pass
            os.rename(src, name)
        _rmtree(NEW_DIR)
        _log("ota: applied %s — rebooting" % _avail)
        import machine
        machine.reset()
    except Exception as e:
        _log("ota: APPLY FAILED %s (staying on current)" % e)
        _fail("apply failed: %s" % e)


def confirm():
    """Commit the running (post-OTA) build as the new known-good baseline. Called
    by the app once it is healthy. No-op if there is no pending update."""
    global _avail
    if not _exists(PENDING):
        return
    try:
        _rmtree(GOOD_DIR)
        os.remove(PENDING)
        _avail = None
        _log("ota: update confirmed healthy — committed")
    except Exception as e:
        _log("ota: confirm err %s" % e)


def _fail(msg):
    global _state, _error
    _state = "error"
    _error = msg
    _rmtree(NEW_DIR)
    _log("ota: FAIL %s" % msg)


# ── status getters (read by mqtt_spa for the status publish) ──────────────────
def available():
    return _avail


def state():
    return _state


def pending():
    return _exists(PENDING)
