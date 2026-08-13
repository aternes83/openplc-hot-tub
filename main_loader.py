import sys

# Run the application. If this is an OTA *trial* boot (ota_pending exists) and the
# app exits — because spa_main failed to import, or main() raised or returned —
# reboot so boot.py counts it as a failed attempt and eventually rolls back. On a
# normal boot (no ota_pending) we just log and stop, exactly as before.
try:
    import spa_main
    spa_main.main()
except Exception as e:
    try:
        import uio as _io
        buf = _io.StringIO()
        sys.print_exception(e, buf)
        with open("ldr.log", "w") as f:
            f.write(buf.getvalue())
    except Exception:
        try:
            with open("ldr.log", "w") as f:
                f.write(str(e) + "\n")
        except Exception:
            pass

# Reached only if main() exited. During an OTA trial, force a reboot so the
# failed attempt is counted (→ rollback). Never reboots on a normal boot.
try:
    import uos
    uos.stat("ota_pending")        # raises if there is no pending update
    import machine
    machine.reset()
except Exception:
    pass
