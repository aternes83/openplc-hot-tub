import spa_main, sys
try:
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
