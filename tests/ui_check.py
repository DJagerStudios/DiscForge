#!/usr/bin/env python3
"""UI quality gate: renders every page and state at several window sizes in both themes, saves
screenshots, and checks every visible widget:
  * siblings in the same layout never overlap,
  * a widget never sticks out of its parent,
  * label text is never clipped (single-line fits, wrapped text has the height it needs),
  * buttons are never narrower or lower than their content,
  * widgets are never squeezed below their minimum size.

    QT_QPA_PLATFORM=offscreen tests/ui_check.py OUTDIR [--sizes 980x660,1280x820,1680x1040]
Exit status 1 when anything is wrong.  Uses a throwaway config folder and stubbed drives.
"""
import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CFG = tempfile.mkdtemp(prefix="discforge-cfg-")
os.environ["XDG_CONFIG_HOME"] = CFG
os.environ["DISCFORGE_CONFIG_DIR"] = CFG
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="discforge-cache-")

from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QAbstractScrollArea, QApplication, QComboBox, QLabel,  # noqa: E402
                               QLineEdit, QPushButton, QScrollBar, QSizePolicy, QStackedWidget, QWidget)

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--sizes", default="980x660,1280x820,1680x1040")
ap.add_argument("--themes", default="dark,light")
args = ap.parse_args()
os.makedirs(args.out, exist_ok=True)
app = QApplication(sys.argv[:1])

from discforge import burner as B  # noqa: E402
from discforge import library  # noqa: E402
from discforge.gui import theme as T  # noqa: E402
from discforge.gui import window as Wn  # noqa: E402
from discforge.gui.widgets import Toast  # noqa: E402

T.load_fonts()
app.setFont(T.body())
Wn.open_settings().setValue("intro", False)

MEDIA = {"kind": "DVD-R", "capacity": 2298496, "present": True, "blank": True}


class FakeDrive:
    def __init__(self, dev, name, writes, accessible=True):
        self.dev, self.name, self.writes, self.accessible, self.group = dev, name, writes, accessible, "optical"


DRIVES = [FakeDrive("/dev/sr0", "ASUS DRW-24D5MT", ("CD-R", "CD-RW", "DVD-R", "DVD+R", "DVD+R DL"))]
Wn.devices.list_drives = lambda: list(DRIVES)
B.drive_status = lambda dev: B.CDS_DISC_OK if MEDIA["present"] else B.CDS_NO_DISC
B.media_info = lambda dev: B.Media(present=MEDIA["present"], kind=MEDIA["kind"], blank=MEDIA["blank"],
                                   status="empty" if MEDIA["blank"] else "complete", capacity=MEDIA["capacity"],
                                   erasable="RW" in MEDIA["kind"])
B.drive_caps = lambda dev: B.DriveCaps(True, [10, 16, 24, 32, 48])
Wn.emulator.find_emulator = lambda: ["retroarch"]
GAMES = [("ps2", "Neon Drift (USA).iso", "SLUS-29901", "ISO", 2.6e9),      # made-up titles, for screenshots
         ("gamecube", "Pixel Garden (USA).iso", "GPGE01", "ISO", 1.46e9),
         ("wii", "Starlight Courier (Europe).wbfs", "RSCP01", "WBFS", 4.3e9),
         ("xbox", "Moonbase Mayhem (USA).iso", "DF-001", "ISO", 6.8e9),
         ("ps1", "Lantern Quest (USA) (Disc 1).cue", "SLUS-09901", "CUE", 6.9e8),
         ("dreamcast", "Tidal Runner (USA).gdi", "", "GDI", 1.1e9),
         ("saturn", "Echo Station (Europe).cue", "", "CUE", 6.1e8),
         ("dreamcast", "Orbit Odyssey (World) (Homebrew).cdi", "", "CDI", 6.4e8),
         ("ps2", "Circuit Breakers (Europe).iso", "SLES-59902", "ISO", 3.9e9),
         ("segacd", "Cosmic Kart (USA).cue", "", "CUE", 5.2e8),
         ("pc", "A really rather long title for a game that tests eliding properly (Europe).iso", "", "ISO", 6e8)]
FOUND = [library.Found(f"/home/user/Games/{n}", k, library.title_from_name(n)[0], "", int(s), time.time() - i, p, g)
         for i, (p, n, g, k, s) in enumerate(GAMES)]
library.scan = lambda *a, **kw: list(FOUND)

problems: list[str] = []
SKIP_TYPES = (QScrollBar, Toast)


def pump(ms: int):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.004)


def name_of(w: QWidget) -> str:
    bits = []
    while w is not None and len(bits) < 6:
        t = type(w).__name__
        txt = ""
        if isinstance(w, (QLabel, QAbstractButton)) and w.text():
            txt = f"'{w.text()[:28]}'"
        bits.append(t + txt)
        w = w.parentWidget()
    return " < ".join(bits)


def visible(w: QWidget) -> bool:
    return w.isVisible() and w.width() > 0 and w.height() > 0


def check_tree(root: QWidget, where: str):
    for w in [root] + root.findChildren(QWidget):
        if not visible(w) or isinstance(w, SKIP_TYPES) or w.window() is not root.window():
            continue
        par = w.parentWidget()
        if par is not None and visible(par) and not isinstance(par, (QAbstractScrollArea, QStackedWidget)) \
                and par.objectName() != "qt_scrollarea_viewport" and not w.isWindow():
            pr = QRect(0, 0, par.width(), par.height())
            g = w.geometry()
            if not pr.adjusted(-1, -1, 1, 1).contains(g):
                problems.append(f"{where}: outside its parent: {name_of(w)} {g.getRect()} in {pr.getRect()}")
        lay = w.layout()
        if lay is not None and not isinstance(w, QStackedWidget):
            check_layout(lay, where)
        if isinstance(w, QAbstractScrollArea) and hasattr(w, "widget") and w.widget() is not None \
                and w.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            if w.widget().width() > w.viewport().width() + 1:
                problems.append(f"{where}: content cut off on the right ({w.widget().width()}px in a "
                                f"{w.viewport().width()}px view): {name_of(w)}")
        if isinstance(w, QLabel) and w.text() and w.pixmap().isNull() and w.textFormat() != Qt.TextFormat.RichText:
            fm = QFontMetrics(w.font())
            cr = w.contentsRect()
            if w.wordWrap():
                need = w.needed_height(w.width()) if hasattr(w, "needed_height") else w.heightForWidth(cr.width())
                if need > cr.height() + 2:
                    problems.append(f"{where}: wrapped text clipped ({need}px > {cr.height()}px): {name_of(w)}")
                elif (need > 0 and cr.height() - need >= fm.lineSpacing() / 2
                      and w.sizePolicy().verticalPolicy() != QSizePolicy.Policy.Expanding):
                    problems.append(f"{where}: wrapped text has a blank line's worth of extra height "
                                    f"({cr.height()}px for {need}px), so it sits off-centre: {name_of(w)}")
            else:
                longest = max(fm.horizontalAdvance(line) for line in w.text().split("\n"))
                if longest > cr.width() + 2:
                    problems.append(f"{where}: text clipped ({longest}px > {cr.width()}px): {name_of(w)}")
        if isinstance(w, QComboBox) and w.currentText():
            fm = QFontMetrics(w.font())
            room = w.width() - 42                     # theme padding: 10px left, 30px right (arrow), border
            if fm.horizontalAdvance(w.currentText()) > room + 1:
                problems.append(f"{where}: drop-down text clipped ({fm.horizontalAdvance(w.currentText())}px > "
                                f"{room}px): {name_of(w)} '{w.currentText()}'")
        if isinstance(w, QLineEdit) and w.placeholderText() and not w.text():
            fm = QFontMetrics(w.font())
            m = w.textMargins()
            room = w.width() - m.left() - m.right() - 20
            if fm.horizontalAdvance(w.placeholderText()) > room + 1:
                problems.append(f"{where}: placeholder text clipped: {name_of(w)} '{w.placeholderText()}'")
        if isinstance(w, QPushButton) and w.text():
            sh = w.sizeHint()
            if sh.width() > w.width() + 2 or sh.height() > w.height() + 2:
                problems.append(f"{where}: button squeezed {w.size().toTuple()} < {sh.toTuple()}: {name_of(w)}")
        msh = w.minimumSizeHint()
        if (msh.isValid() and not isinstance(w, (QAbstractScrollArea, QStackedWidget)) and w.layout() is not None
                and (w.width() < msh.width() - 2 or w.height() < msh.height() - 2)):
            problems.append(f"{where}: squeezed below minimum {w.size().toTuple()} < {msh.toTuple()}: {name_of(w)}")


def check_layout(lay, where: str):
    """Every item of every (nested) layout: no two overlap, and each widget really sits in its cell."""
    from PySide6.QtWidgets import QStackedLayout
    if isinstance(lay, QStackedLayout):
        return
    items = []
    for i in range(lay.count()):
        it = lay.itemAt(i)
        wid, sub = it.widget(), it.layout()
        if wid is not None:
            if not visible(wid) or isinstance(wid, SKIP_TYPES):
                continue
            cell = it.geometry()
            g = wid.geometry()
            if cell.isValid() and not cell.adjusted(-1, -1, 1, 1).contains(g):
                problems.append(f"{where}: not where its layout put it: {name_of(wid)} {g.getRect()} vs "
                                f"{cell.getRect()}")
            items.append((name_of(wid), g))
        elif sub is not None:
            check_layout(sub, where)
            if sub.geometry().isValid() and not sub.isEmpty():
                items.append((f"layout in {name_of(sub.parentWidget()) if sub.parentWidget() else '?'}",
                              sub.geometry()))
    for i, (na, ga) in enumerate(items):
        for nb, gb in items[i + 1:]:
            inter = ga.intersected(gb)
            if inter.width() > 1 and inter.height() > 1:
                problems.append(f"{where}: overlap: {na} / {nb} {inter.getRect()}")


def shot_and_check(w, label: str):
    pump(160)
    w.grab().save(os.path.join(args.out, label + ".png"))
    check_tree(w, label)


def make_ps2_iso(tmp: str) -> str:
    src = os.path.join(tmp, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "SYSTEM.CNF"), "wb") as f:
        f.write(b"BOOT2 = cdrom0:\\SLUS_299.01;1\r\nVER = 1.00\r\n")
    with open(os.path.join(src, "SLUS_299.01"), "wb") as f:
        f.write(random.randbytes(1_500_000))
    iso = os.path.join(tmp, "Neon Drift (USA).iso")
    subprocess.run(["mkisofs", "-quiet", "-sysid", "PLAYSTATION", "-udf", "-o", iso, src], check=True)
    return iso


def wait(cond, ms=6000):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end and not cond():
        pump(30)


tmp = tempfile.mkdtemp(prefix="df-ui-")
iso = make_ps2_iso(tmp)
junk = os.path.join(tmp, "notes.iso")
with open(junk, "wb") as f:
    f.write(random.randbytes(1 << 20))
sizes = [tuple(int(x) for x in s.split("x")) for s in args.sizes.split(",")]
for theme in args.themes.split(","):
    Wn.open_settings().setValue("theme", theme)
    w = Wn.MainWindow()
    w.show()
    w.apply_theme(theme)
    for (sw, sh) in sizes:
        tag = f"{theme}-{sw}x{sh}"
        w.resize(sw, sh)
        MEDIA.update(kind="DVD-R", present=True, blank=True, capacity=2298496)
        w.refresh_drives()
        pump(300)
        w.clear_game()
        shot_and_check(w, f"{tag}-01-burn-empty")
        w.open_game(iso)
        wait(lambda w=w: w.job is not None)
        pump(200)
        shot_and_check(w, f"{tag}-02-burn-ready")
        MEDIA.update(kind="CD-R", capacity=359849)
        w.refresh_drives()
        pump(400)
        shot_and_check(w, f"{tag}-03-burn-wrong-disc")
        MEDIA.update(kind="DVD-R", capacity=2298496)
        w.refresh_drives()
        pump(300)
        w.burn_opts = B.BurnOptions(dev="/dev/sr0", speed=4, verify=True)
        w.burn_page.progress.start(w.job, True, False)
        w.burn_page.show_progress()
        w.burn_event({"stage": "write", "frac": 0.43, "mb": 1066, "mb_total": 2480, "speed": 4.0, "eta": 312})
        shot_and_check(w, f"{tag}-04-burning")
        w.burn_event({"stage": "verify", "part": 0.61})
        shot_and_check(w, f"{tag}-05-verifying")
        w.burn_started = time.monotonic() - 754
        w.burn_done(B.BurnResult(ok=True, simulated=False, verified=True))
        shot_and_check(w, f"{tag}-06-done")
        w.burn_failed("The drive reported a write error. Try a fresh blank DVD, ideally another brand, at a "
                      "slower speed.")
        shot_and_check(w, f"{tag}-07-failed")
        w.burn_page.show_setup()
        w.open_game(junk)
        wait(lambda w=w: w.loading is None)
        shot_and_check(w, f"{tag}-08-burn-error")
        w.show_page("library")
        w.found = list(FOUND)
        w.fill_library()
        shot_and_check(w, f"{tag}-09-library")
        w.found = []
        w.fill_library()
        shot_and_check(w, f"{tag}-10-library-empty")
        w.found = list(FOUND)
        w.fill_library()
        w.show_page("drives")
        shot_and_check(w, f"{tag}-11-drives")
        w.show_page("history")
        shot_and_check(w, f"{tag}-12-history")
        w.show_page("settings")
        shot_and_check(w, f"{tag}-13-settings")
        w.show_page("burn")
    w.close()
    import shiboken6
    shiboken6.delete(w)
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(CFG, ignore_errors=True)
uniq = list(dict.fromkeys(problems))
for p in uniq:
    print("PROBLEM", p)
print(f"{len(uniq)} problem(s)")
sys.exit(1 if uniq else 0)
