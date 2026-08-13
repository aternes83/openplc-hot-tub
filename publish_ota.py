#!/usr/bin/env python3
"""
publish_ota.py — publish a firmware OTA release to the MQTT broker.

The board subscribes to the retained `spa/ota/manifest`; when its version is newer
than the running firmware it advertises `ota_avail` in status. The app then sends
{"ota_apply": "<version>"} and the board pulls each `spa/ota/file/<name>` (retained,
raw bytes), verifies sha256, backs up, applies, and reboots — with automatic
rollback if the new build won't run.

Usage:
  # compile spa_main.mpy first, then:
  python3 publish_ota.py --version 1.2.0 --files spa_main.mpy mqtt_spa.py ota.py
  python3 publish_ota.py --version 1.2.0 --files spa_main.mpy   # single-file update
  python3 publish_ota.py --clear                                # retract the manifest

Reads broker creds from config.json (never prints them). Only application files
may be shipped — boot.py / main.py / config.json are refused (they are the
recovery path / device identity and are USB-only).
"""
import argparse
import hashlib
import json
import os
import sys
import time

import paho.mqtt.client as mqtt

MANIFEST_TOPIC = "spa/ota/manifest"
FILE_PREFIX    = "spa/ota/file/"
CHUNK_TOPIC    = "spa/ota/file/%s/%d"     # <name>/<index>
PROTECTED = {"boot.py", "main.py", "config.json"}

# Files are delivered as small retained chunks — the board can't allocate a
# contiguous buffer for a whole-file MQTT payload.
CHUNK_SIZE = 1024

HERE = os.path.dirname(os.path.abspath(__file__))


def _client():
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    cl = mqtt.Client(client_id="ota-publisher", protocol=mqtt.MQTTv311)
    cl.tls_set()
    cl.username_pw_set(cfg["mqtt_user"], cfg["mqtt_password"])
    cl.connect(cfg["mqtt_host"], int(cfg["mqtt_port"]), 60)
    cl.loop_start()
    time.sleep(1)
    return cl


def _clear_files(cl):
    """Delete every retained spa/ota/file/# message (stale chunks from prior
    releases, which would otherwise corrupt a reassembled file)."""
    seen = []
    cl.subscribe("spa/ota/file/#", qos=1)
    cl.on_message = lambda c, u, m: seen.append(m.topic) if m.retain else None
    time.sleep(3)
    for t in set(seen):
        cl.publish(t, "", qos=1, retain=True)
    cl.on_message = None
    if seen:
        print("  cleared %d stale retained chunk topic(s)" % len(set(seen)))
    time.sleep(1)


def publish(version, files):
    for f in files:
        base = os.path.basename(f)
        if base in PROTECTED:
            sys.exit("refusing to ship protected file: %s" % base)
        if not os.path.exists(f):
            sys.exit("missing file: %s" % f)

    manifest = {"version": version, "chunk_size": CHUNK_SIZE, "files": {}}
    chunks = {}
    for f in files:
        base = os.path.basename(f)
        data = open(f, "rb").read()
        parts = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)] or [b""]
        manifest["files"][base] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "chunks": len(parts),
        }
        chunks[base] = parts

    cl = _client()
    _clear_files(cl)                       # wipe stale chunks first
    for base, parts in chunks.items():
        for i, ch in enumerate(parts):
            cl.publish(CHUNK_TOPIC % (base, i), ch, qos=1, retain=True)
        print("  published %s: %d chunk(s), %d bytes, sha256 %s…"
              % (base, len(parts), manifest["files"][base]["size"],
                 manifest["files"][base]["sha256"][:12]))
    time.sleep(2)                          # let the broker persist the retained chunks
    cl.publish(MANIFEST_TOPIC, json.dumps(manifest), qos=1, retain=True)
    time.sleep(2)
    cl.loop_stop()
    cl.disconnect()
    print("published manifest v%s with %d file(s)" % (version, len(files)))


def clear():
    """Retract the manifest and all file chunks (boards stop seeing an update)."""
    cl = _client()
    cl.publish(MANIFEST_TOPIC, "", qos=1, retain=True)
    _clear_files(cl)
    time.sleep(1)
    cl.loop_stop()
    cl.disconnect()
    print("cleared retained OTA manifest + chunks")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version")
    ap.add_argument("--files", nargs="+", default=[])
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    if args.clear:
        clear()
    elif args.version and args.files:
        publish(args.version, args.files)
    else:
        ap.error("give --version and --files, or --clear")


if __name__ == "__main__":
    main()
