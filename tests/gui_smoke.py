#!/usr/bin/env python3
"""End-to-end GUI run, offscreen: the real window opens a game, the Forge button burns it on the
simulated drives (tests/fake_drive) and the result page must say the disc is ready.

    QT_QPA_PLATFORM=offscreen tests/gui_smoke.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="discforge-cfg-")      # never touch real settings
os.environ["DISCFORGE_CONFIG_DIR"] = os.environ["XDG_CONFIG_HOME"]
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="discforge-cache-")
import atexit  # noqa: E402
for _d in (os.environ["XDG_CONFIG_HOME"], os.environ["XDG_CACHE_HOME"]):
    atexit.register(shutil.rmtree, _d, True)

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])

from discforge import library, tools  # noqa: E402
from discforge.gui import theme as T  # noqa: E402
from discforge.gui import window as A  # noqa: E402
from test_multi import SimDrive, mkiso, raw_mode2  # noqa: E402

library.scan = lambda *a, **kw: []
A.open_settings().setValue("intro", False)
T.load_fonts()


def pump_until(cond, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="df-smoke-")
os.makedirs(os.path.join(tmp, "ps1src"))
with open(os.path.join(tmp, "ps1src", "SYSTEM.CNF"), "wb") as f:
    f.write(b"BOOT = cdrom:\\SLUS_012.34;1\r\n")
with open(os.path.join(tmp, "ps1src", "SLUS_012.34"), "wb") as f:
    f.write(os.urandom(200_000))
iso = mkiso(os.path.join(tmp, "ps1src"), os.path.join(tmp, "ps1.iso"), "-sysid", "PLAYSTATION")
with open(iso, "rb") as f:
    user = f.read()
with open(os.path.join(tmp, "Smoke Test (USA).bin"), "wb") as f:
    f.write(raw_mode2(user))
cue = os.path.join(tmp, "Smoke Test (USA).cue")
with open(cue, "w") as f:
    f.write('FILE "Smoke Test (USA).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
os.makedirs(os.path.join(tmp, "ps2src"))
with open(os.path.join(tmp, "ps2src", "SYSTEM.CNF"), "wb") as f:
    f.write(b"BOOT2 = cdrom0:\\SLUS_200.01;1\r\n")
with open(os.path.join(tmp, "ps2src", "SLUS_200.01"), "wb") as f:
    f.write(os.urandom(900_000))
ps2 = mkiso(os.path.join(tmp, "ps2src"), os.path.join(tmp, "Smoke Two (USA).iso"), "-sysid", "PLAYSTATION", "-udf")


def burn_once(media: str, game: str, expect_platform: str, sub: str):
    with SimDrive(os.path.join(tmp, sub), media=media) as sim:
        A.devices.list_drives = lambda: [A.devices.OpticalDrive(sim.dev, "Simulated writer", True,
                                                                ("CD-R", "DVD-R", "DVD+R", "DVD+R DL"))]
        w = A.MainWindow()
        w.show()
        check(pump_until(lambda: w.drive() is not None and w.drive().state == "ready", 20), f"{media}: blank disc seen")
        w.open_game(game)
        check(pump_until(lambda: w.job is not None, 30), f"{media}: game opened")
        check(w.job.platform.id == expect_platform, f"{media}: identified as {expect_platform}")
        w.refresh()
        go = w.burn_page.burn.go
        check(go.isEnabled(), f"{media}: Burn button enabled ({w.burn_page.burn.reason.text()!r})")
        go.click()
        rv = w.burn_page.result
        check(pump_until(lambda: w.burn_page.stack.currentWidget() is rv, 90), f"{media}: burn finished")
        if rv.title.text() != "Your disc is ready":
            print("\n".join(w.burn_log[-25:]))
        check(rv.title.text() == "Your disc is ready", f"{media}: result says ready "
              f"({rv.title.text()!r}: {rv.msg.text()!r})")
        check("checked" in rv.msg.text(), f"{media}: disc was verified")
        check(w.history and w.history[0]["result"] == "ok" and w.history[0]["verified"], f"{media}: in history")
        w.close()
        import shiboken6
        shiboken6.delete(w)
        return sim.state()


st = burn_once("CD-R", cue, "ps1", "cd")
check(st.get("driver_used") == "generic-mmc-raw", "PS1 written raw by cdrdao")
st = burn_once("DVD-R", ps2, "ps2", "dvd")
if tools.DVD_WITH_CDRECORD:                       # Windows / macOS
    check(any("-sao" in c for c in st["calls"]), "PS2 written disc-at-once by cdrecord")
else:
    check(any(c[0] == "growisofs" and "-use-the-force-luke=dao" in c for c in st["calls"]),
          "PS2 written DAO by growisofs")
ds = os.path.expanduser("~/.cache/discforge-dev/dl-dreamshell/DreamShell_bootloader_v2.9.cdi")
if os.path.exists(ds):
    st = burn_once("CD-R", ds, "dreamcast", "dc")
    check(st["sessions"] == 2 and st["closed"] and st["tracks"][1]["lba"] == 11702,
          "Dreamcast written as a 2-session selfboot disc (boot session at LBA 11702)")
shutil.rmtree(tmp, ignore_errors=True)
print("GUI smoke test passed")
