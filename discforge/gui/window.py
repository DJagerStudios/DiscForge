"""DiscForge's main window and everything it coordinates: drives, the chosen game, the burn."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import resources

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontMetricsF, QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFileDialog, QMainWindow, QMenu,
                               QMessageBox, QStackedWidget, QWidget)

from .. import __version__
from .. import burner as B
from .. import detect as D
from .. import devices
from .. import emulator
from .. import formats as F
from .. import jobs as J
from .. import library
from .. import platforms as P
from .. import tools
from ..engine import media_ok
from ..util import DcError, cache_dir, clean_cache
from . import art
from . import icons as I
from . import theme as T
from .boot import BootOverlay
from .pages.burn import BurnPage, duration, human_size
from .pages.other import DrivesPage, HistoryPage, LibraryPage, SettingsPage
from .widgets import IconButton, NavButton, Text, Tile, Toast, hbox, vbox, widget_of

APP_NAME = "DiscForge"
APP_ID = "discforge"
PROJECT_URL = "https://github.com/DJagerStudios/DiscForge"

FRIENDLY = [  # (substring of a technical message, what a person wants to know)
    ("looks UNscrambled", "This image probably won't start on a real Dreamcast: its boot file wasn't prepared "
     "for CD-R."),
    ("Windows CE", "This is a Windows CE game. Those often need extra patches to start from a CD-R."),
    ("Experimental GD-ROM conversion", "Converted from a GD-ROM dump automatically. Most games work this way, "
     "but test it in the emulator first if you can."),
    ("CD audio track", "The game's CD music tracks can't be included, so some music may be silent."),
    ("Couldn't tell whether", "We couldn't tell how this program is packed. Test it in the emulator first."),
    ("not a Dreamcast boot disc", "This doesn't look like a Dreamcast disc."),
    ("No valid IP.BIN", "This doesn't look like a Dreamcast disc (no boot header)."),
    ("was not found in the disc", "The game's start file is missing from this image."),
    ("bad version marker", "This isn't a disc image, or it's incomplete (maybe the download didn't finish)."),
    ("not a DiscJuggler", "This isn't a disc image, or it's incomplete (maybe the download didn't finish)."),
    ("too small to be a CDI", "This file is damaged or incomplete."),
    ("is truncated", "This file is incomplete (maybe the download didn't finish)."),
    ("does not exist", "That file isn't there any more."),
    ("doesn't fit on a CD-R", "This game is too big to fit on a CD-R."),
    ("scrambled for you", "Fixed automatically: this image's start file wasn't prepared for CD-R, so it has "
     "been scrambled."),
    ("almost full", "The disc will be almost full. Use a good-quality disc; the outer edge is the hardest part "
     "for an old laser to read."),
    ("XA Form 2", "This image contains streamed video/audio sectors that can't be put on a Dreamcast CD-R."),
]


def friendly(msg: str) -> str:
    for key, text in FRIENDLY:
        if key in msg:
            return text
    return msg


def app_icon() -> QIcon:
    themed = QIcon.fromTheme(APP_ID)
    if not themed.isNull():
        return themed
    return QIcon(str(resources.files("discforge").joinpath("data/discforge.svg")))


def config_dir() -> str:
    """Settings and history.  DISCFORGE_CONFIG_DIR overrides it (tests, --self-test)."""
    d = (os.environ.get("DISCFORGE_CONFIG_DIR")
         or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
         or os.path.expanduser("~/.config/DiscForge"))
    os.makedirs(d, exist_ok=True)
    return d


def open_settings() -> QSettings:
    if os.environ.get("DISCFORGE_CONFIG_DIR"):
        return QSettings(os.path.join(config_dir(), "DiscForge.conf"), QSettings.Format.IniFormat)
    return QSettings("DiscForge", "DiscForge")


def notify(title: str, body: str) -> None:
    exe = shutil.which("notify-send")
    if exe:
        subprocess.Popen([exe, "-a", APP_NAME, "-i", APP_ID, title, body], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    elif sys.platform == "darwin" and shutil.which("osascript"):
        subprocess.Popen(["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_as_admin(args: list[str]) -> tuple[bool, str]:
    """Run a command with administrator rights (pkexec on Linux, doas/sudo through a terminal elsewhere)."""
    if sys.platform.startswith("linux") and shutil.which("pkexec"):
        r = subprocess.run(["pkexec"] + args, capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    if sys.platform == "darwin":
        cmd = " ".join(f"'{a}'" for a in args)
        r = subprocess.run(["osascript", "-e", f'do shell script "{cmd}" with administrator privileges'],
                           capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    return False, "Run this in a terminal as administrator: " + " ".join(args)


def install_command(pkgs: list[str]) -> list[str] | None:
    """The system package manager command for the burning tools, on systems that have one."""
    if sys.platform == "darwin":
        brew = shutil.which("brew") or next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                             if os.path.exists(b)), None)
        names = {"mame-tools": "rom-tools", "7zip": "sevenzip", "dolphin-emu": "--cask dolphin"}
        return [brew, "install"] + [w for p in pkgs for w in names.get(p, p).split()] if brew else None
    managers = [("pacman", ["pacman", "-S", "--needed", "--noconfirm"], {}),
                ("apt-get", ["apt-get", "install", "-y"], {"cdrtools": "wodim genisoimage", "dvd+rw-tools": "growisofs",
                                                           "7zip": "7zip"}),
                ("dnf", ["dnf", "install", "-y"], {"cdrtools": "cdrecord genisoimage", "7zip": "p7zip"}),
                ("zypper", ["zypper", "--non-interactive", "install"], {"cdrtools": "cdrecord genisoimage",
                                                                       "7zip": "7zip"}),
                ("pkg", ["pkg", "install", "-y"], {"mame-tools": "mame", "7zip": "7-zip"}),
                ("xbps-install", ["xbps-install", "-y"], {}), ("apk", ["apk", "add"], {})]
    for exe, cmd, names in managers:
        if shutil.which(exe):
            out = []
            for p in pkgs:
                out += names.get(p, p).split()
            return cmd + out
    return None


class Taskbar:
    """Progress on the task manager entry (Unity LauncherEntry over D-Bus; Plasma, GNOME docks...)."""

    def __init__(self):
        self.ok = sys.platform.startswith(("linux", "freebsd"))

    def set(self, progress: float | None):
        if not self.ok:
            return
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusMessage
            msg = QDBusMessage.createSignal(f"/com/canonical/unity/launcherentry/{APP_ID}",
                                            "com.canonical.Unity.LauncherEntry", "Update")
            msg.setArguments([f"application://{APP_ID}.desktop",
                              {"progress-visible": progress is not None, "progress": float(progress or 0.0)}])
            QDBusConnection.sessionBus().send(msg)
        except Exception:  # noqa: BLE001
            self.ok = False


class Worker(QThread):
    progress = Signal(str, float)
    event = Signal(object)
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel = F.Cancel()

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except DcError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - shown to the user, never a crash
            self.failed.emit(f"{type(e).__name__}: {e}")


@dataclass
class DriveState:
    dev: str
    name: str
    accessible: bool = True
    writes: tuple = ()
    group: str = ""
    status: int = -99
    state: str = "unknown"          # empty open busy reading ready used rw noperm error unknown
    media: B.Media | None = None
    caps: B.DriveCaps | None = None
    error: str = ""
    tries: int = 0
    last_read: float = 0.0


# ============================================================================ chrome

class Sidebar(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setFixedWidth(T.SIDEBAR_W)
        self.logo = QWidget()
        self.logo.setFixedHeight(T.TOPBAR_H)
        self.logo.paintEvent = self._paint_logo
        self.nav: dict[str, NavButton] = {}
        top = vbox(spacing=2)
        for key, icon, text in (("burn", "flame", "Burn"), ("library", "library", "Library"),
                                ("drives", "hard-drive", "Drives"), ("history", "history", "History")):
            b = NavButton(icon, text)
            b.clicked.connect(lambda _c=False, k=key: win.show_page(k))
            self.nav[key] = b
            top.addWidget(b)
        s = NavButton("settings", "Settings")
        s.clicked.connect(lambda: win.show_page("settings"))
        self.nav["settings"] = s
        self.version = Text(f"Version {__version__}", T.caption(), "text3")
        self.setLayout(vbox(self.logo, T.SM, widget_of(top), None, s, T.MD, self.version,
                            margins=(T.MD, 0, T.MD, T.LG)))

    def _paint_logo(self, _e):
        p = QPainter(self.logo)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.logo.height()
        art.paint_mark(p, QRectF(6, h / 2 - 14, 28, 28))
        art.paint_wordmark(p, 44, h / 2 + 6.5, 18)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.P.sidebar))
        p.fillRect(QRectF(self.width() - 1, 0, 1, self.height()), QColor(T.P.border))

    def select(self, key: str):
        for k, b in self.nav.items():
            b.setChecked(k == key)


class DrivePill(QAbstractButton):
    def __init__(self):
        super().__init__()
        self.name, self.status, self.dot = "Looking for a disc writer…", "", T.P.text3
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        self.setFont(T.font(13, T.W.Medium))

    def set(self, name: str, status: str, dot: str):
        self.name, self.status, self.dot = name, status, dot
        self.setToolTip(f"{name} — {status}" if status else name)
        self.updateGeometry()
        self.update()

    def _name(self) -> str:
        fm = QFontMetricsF(self.font())
        return fm.elidedText(self.name, Qt.TextElideMode.ElideRight, 220)

    def sizeHint(self):  # noqa: N802
        fm1, fm2 = QFontMetricsF(self.font()), QFontMetricsF(T.small())
        w = 34 + fm1.horizontalAdvance(self._name()) + (fm2.horizontalAdvance(self.status) + 21 if self.status else 0)
        return QSize(int(w) + 34, 36)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hover = self.underMouse()
        p.setPen(art.pen(T.P.border_strong if hover else T.P.border, 1))
        p.setBrush(QColor(T.P.raised if hover else T.P.surface))
        p.drawRoundedRect(r, T.R_MD, T.R_MD)
        cy = r.center().y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.c(self.dot, 0.22))
        p.drawEllipse(QPointF(r.left() + 17, cy), 7, 7)
        p.setBrush(QColor(self.dot))
        p.drawEllipse(QPointF(r.left() + 17, cy), 3.5, 3.5)
        x = r.left() + 32
        name = self._name()
        p.setFont(self.font())
        p.setPen(QColor(T.P.text))
        fm1 = QFontMetricsF(self.font())
        p.drawText(QRectF(x, r.top(), fm1.horizontalAdvance(name) + 2, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   name)
        x += fm1.horizontalAdvance(name) + 10
        if self.status:
            p.setPen(art.pen(T.P.border_strong, 1))
            p.drawLine(QPointF(x, cy - 7), QPointF(x, cy + 7))
            p.setFont(T.small())
            p.setPen(QColor(T.P.text2))
            fm2 = QFontMetricsF(T.small())
            p.drawText(QRectF(x + 10, r.top(), fm2.horizontalAdvance(self.status) + 2, r.height()),
                       Qt.AlignmentFlag.AlignVCenter, self.status)
        I.paint(p, "chevron-down", QRectF(r.right() - 24, cy - 7, 14, 14), T.P.text3)


class Topbar(QWidget):
    TITLES = {"burn": ("Burn", "Pick a game, insert a disc, press Burn."),
              "library": ("Library", "Games found on this computer."),
              "drives": ("Drives", "Disc writers connected to this computer."),
              "history": ("History", "Every disc you've burned, with its log."),
              "settings": ("Settings", "")}

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setFixedHeight(T.TOPBAR_H + 8)
        self.title = Text("", T.display(), "text")
        self.sub = Text("", T.small(), "text3")
        self.pill = DrivePill()
        self.pill.clicked.connect(lambda: win.drive_menu(self.pill))
        self.help = IconButton("circle-help", "How DiscForge works", 36)
        self.help.clicked.connect(win.show_help)
        self.setLayout(hbox((vbox(None, self.title, 2, self.sub, None), 1), T.LG, self.pill, T.SM, self.help,
                            margins=(T.XL, T.SM, T.XL, 0)))

    def set_page(self, key: str):
        t, s = self.TITLES.get(key, ("", ""))
        self.title.setText(t)
        self.sub.setText(s)
        self.sub.setVisible(bool(s))


# ============================================================================ window

HELP = """<h3>How DiscForge works</h3>
<p><b>1. Pick a game.</b> Drop it on the window, choose it in the Library or use <i>Open a game</i>.
Archives (zip, 7z, rar), CUE/BIN, ISO, CHD, CDI, GDI, WBFS, RVZ, GCZ, CSO/ZSO, ECM, CloneCD and folders all
work: DiscForge unpacks and converts automatically.</p>
<p><b>2. Insert a blank disc.</b> The Disc step says which kind the game needs: CD-R for Dreamcast, PlayStation,
Saturn, Sega CD, PC Engine CD and 3DO; DVD-R or DVD+R for PlayStation 2, GameCube, Wii and Xbox (DVD+R DL for
the biggest games).</p>
<p><b>3. Press Burn.</b> The right write mode and a safe speed are chosen for that console. Afterwards the whole
disc is read back and compared with the game.</p>
<p style="color:#8a909b">Most consoles only play burned discs when they're modded; each game shows what its
console needs. PAL PlayStation games with LibCrypt protection are patched automatically (on a copy).</p>"""


class MainWindow(QMainWindow):
    def __init__(self, initial: str | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(980, 660)
        self.settings = open_settings()
        size = self.settings.value("size")
        self.resize(size if isinstance(size, QSize) else QSize(1280, 820))
        self.setAcceptDrops(True)
        self.workers: set[Worker] = set()
        self.drives: dict[str, DriveState] = {}
        self.current_dev: str | None = self.settings.value("drive", None)
        self.job: J.Job | None = None
        self.loading: Worker | None = None
        self.load_token = 0
        self.burning: Worker | None = None
        self.installing: Worker | None = None
        self.burner_holder: list = []
        self.burn_log: list[str] = []
        self.burn_opts: B.BurnOptions | None = None
        self.found: list[library.Found] = []
        self.dc_opts: dict = {"title": None, "scrambled": None, "gd_lba": False}
        self.last_path: str | None = None
        self.emu = False                    # found in the background (it may ask Flatpak)
        self.taskbar = Taskbar()
        self.page = "burn"
        self.history = self._load_history()
        self._build()
        self.apply_theme(self.settings.value("theme", "system"))
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.poll_drives)
        self.poll.start(1500)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        QTimer.singleShot(60, self.poll_drives)
        QTimer.singleShot(250, self.start_scan)
        QTimer.singleShot(400, self._find_emulator)
        if initial:
            QTimer.singleShot(0, lambda: self.open_game(initial))
        self.refresh()

    def _find_emulator(self):
        def found(ok):
            self.emu = bool(ok)             # read when the game's menu opens

        self.run(lambda _w: emulator.find_emulator() is not None, found, lambda _m: None)

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        self.sidebar = Sidebar(self)
        self.topbar = Topbar(self)
        self.burn_page = BurnPage(self)
        self.library_page = LibraryPage(self)
        self.drives_page = DrivesPage(self)
        self.history_page = HistoryPage(self)
        self.settings_page = SettingsPage(self)
        self.pages = QStackedWidget()
        self.page_of = {"burn": self.burn_page, "library": self.library_page, "drives": self.drives_page,
                        "history": self.history_page, "settings": self.settings_page}
        for w in self.page_of.values():
            self.pages.addWidget(w)
        root.setLayout(hbox(self.sidebar, (vbox(self.topbar, (self.pages, 1)), 1)))
        self.toast = Toast(root)
        self.burn_page.burn.verify.setChecked(self.settings.value("verify", True, bool))
        for seq, fn in ((QKeySequence.StandardKey.Open, self.browse), ("F1", self.show_help),
                        ("Ctrl+F", self._focus_search), ("F5", self.refresh_everything),
                        ("Ctrl+1", lambda: self.show_page("burn")), ("Ctrl+2", lambda: self.show_page("library")),
                        ("Ctrl+3", lambda: self.show_page("drives")), ("Ctrl+4", lambda: self.show_page("history")),
                        ("Ctrl+,", lambda: self.show_page("settings")), ("Esc", self.cancel_loading)):
            QShortcut(QKeySequence(seq), self, fn)
        self.settings_page.set_folders(self.library_roots())
        self._update_tools()
        self.history_page.set_entries(self.history)
        self.show_page("burn")

    def _focus_search(self):
        self.show_page("library")
        self.library_page.search.setFocus()

    def show_page(self, key: str):
        self.page = key
        self.pages.setCurrentWidget(self.page_of[key])
        self.sidebar.select(key)
        self.topbar.set_page(key)
        if key == "settings":
            self.settings_page.cache.setText(human_size(_cache_size()))

    def play_intro(self):
        if not self.settings.value("intro", True, bool) or self.settings.value("reduce_motion", False, bool):
            return
        self.boot = BootOverlay(self.centralWidget())
        self.boot.show()
        self.boot.raise_()
        self.boot.setFocus()

    # ------------------------------------------------------------------ theme
    def apply_theme(self, mode: str):
        name = mode
        if mode == "system":
            try:
                scheme = QApplication.styleHints().colorScheme()
                name = "light" if scheme == Qt.ColorScheme.Light else "dark"
            except AttributeError:
                name = "dark"
        T.set_palette(name)
        app = QApplication.instance()
        app.setStyleSheet(T.stylesheet())
        for w in app.allWidgets():
            if hasattr(w, "retheme"):
                w.retheme()
            w.update()

    def setting_changed(self, key: str, value):
        self.settings.setValue(key, value)
        if key == "theme":
            self.apply_theme(value)
        elif key == "verify":
            self.burn_page.burn.verify.setChecked(bool(value))
        elif key in ("libcrypt", "dc_region_free", "dc_vga") and self.job and self.last_path:
            self.open_game(self.last_path, keep_opts=True)
        elif key == "reduce_motion":
            self.burn_page.progress.visual.reduce_motion = bool(value)
        self.refresh()

    def verify_changed(self, on: bool):
        self.settings.setValue("verify", on)
        self.settings_page.t_verify.blockSignals(True)
        self.settings_page.t_verify.setChecked(on)
        self.settings_page.t_verify.blockSignals(False)

    # ------------------------------------------------------------------ workers
    def run(self, fn, done, failed=None, *, progress=None, event=None, log=None) -> Worker:
        w = Worker(fn, self)
        if progress:
            w.progress.connect(progress)
        if event:
            w.event.connect(event)
        if log:
            w.log.connect(log)
        w.done.connect(done)
        w.failed.connect(failed or (lambda m: self.toast.show_message(m, "bad", 6000)))
        w.finished.connect(lambda: self.workers.discard(w))
        w.finished.connect(w.deleteLater)
        self.workers.add(w)
        w.start()
        return w

    def _tick(self):
        if self.burning:
            self.burn_page.progress.tick_elapsed()

    # ------------------------------------------------------------------ library
    def library_roots(self) -> list[str]:
        roots = self.settings.value("library_roots", None)
        if isinstance(roots, str):
            roots = [roots]
        if not roots:
            roots = library.default_roots()
        return [r for r in roots if r]

    def add_library_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Add a folder with games", os.path.expanduser("~"))
        if not path:
            return
        roots = self.library_roots()
        if path not in roots:
            roots.append(path)
            self.settings.setValue("library_roots", roots)
        self.settings_page.set_folders(roots)
        self.start_scan()

    def remove_library_folder(self, path: str):
        roots = [r for r in self.library_roots() if r != path]
        self.settings.setValue("library_roots", roots or ["/nonexistent-placeholder"])
        self.settings_page.set_folders(roots)
        self.start_scan()

    def start_scan(self):
        lp = self.library_page
        lp.spin.show()
        lp.b_refresh.setEnabled(False)
        roots = [r for r in self.library_roots() if os.path.isdir(r)]
        recent = [p for p in (self.settings.value("recent", [], list) or []) if os.path.exists(p)]

        def work(w: Worker):
            out = []
            for pth in recent:
                try:
                    st = os.stat(pth)
                    kind = os.path.splitext(pth)[1][1:].upper() or "FOLDER"
                    title, regions, plat, gid = library.describe(pth, kind)
                    out.append(library.Found(pth, kind, title or library.title_from_name(pth)[0], regions,
                                             st.st_size, st.st_mtime, plat or "unknown", gid))
                except OSError:
                    pass
            return out + library.scan(roots, should_stop=lambda: bool(w.cancel))

        def done(found):
            seen, uniq = set(), []
            for f in found:
                rp = os.path.realpath(f.path)
                if rp not in seen:
                    seen.add(rp)
                    uniq.append(f)
            self.found = uniq
            lp.spin.hide()
            lp.b_refresh.setEnabled(True)
            self.fill_library()

        def failed(_m):
            lp.spin.hide()
            lp.b_refresh.setEnabled(True)

        self.run(work, done, failed)

    def fill_library(self):
        lp = self.library_page
        counts: dict[str, int] = {}
        for f in self.found:
            counts[f.platform] = counts.get(f.platform, 0) + 1
        lp.set_consoles(counts)
        want = lp.console.currentData() or "all"
        q = lp.search.text().strip().lower()
        items = [f for f in self.found if (want == "all" or f.platform == want)
                 and (not q or q in f.title.lower() or q in f.path.lower() or q in (f.game_id or "").lower())]
        key = lp.sort.currentData() or "recent"
        if key == "name":
            items.sort(key=lambda f: f.title.lower())
        elif key == "console":
            items.sort(key=lambda f: (P.get(f.platform).name, f.title.lower()))
        elif key == "size":
            items.sort(key=lambda f: -f.size)
        else:
            items.sort(key=lambda f: -f.mtime)
        tiles = []
        for f in items:
            plat = P.get(f.platform)
            t = Tile(f.path, f.title, plat.accent, plat.accent2, plat.short if f.platform != "unknown" else "?",
                     f"{plat.name if f.platform not in ('unknown',) else 'Unknown'} · {human_size(f.size)}",
                     f.game_id)
            t.selected = bool(self.last_path and os.path.realpath(f.path) == os.path.realpath(self.last_path))
            t.clicked.connect(self._tile_clicked)
            t.menu.connect(self._tile_menu)
            tiles.append(t)
        lp.set_tiles(tiles, len(self.found))
        self.sidebar.nav["library"].set_badge(str(len(self.found)) if self.found else "")

    def _tile_clicked(self, path: str):
        self.show_page("burn")
        self.open_game(path)

    def _tile_menu(self, path: str, pos):
        m = QMenu(self)
        m.addAction(I.icon("flame", T.P.text), "Burn this game", lambda: self._tile_clicked(path))
        m.addAction(I.icon("folder-open", T.P.text), "Show in folder",
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path))))
        m.exec(pos)

    def remember(self, path: str):
        rec = [p for p in (self.settings.value("recent", [], list) or []) if p != path]
        self.settings.setValue("recent", [path] + rec[:19])

    # ------------------------------------------------------------------ choosing a game
    def browse(self):
        start = os.path.dirname(self.last_path) if self.last_path else os.path.expanduser("~/Downloads")
        exts = " ".join(f"*{e}" for e in sorted(D.ALL_EXTS))
        path, _ = QFileDialog.getOpenFileName(self, "Choose a game", start, f"Games ({exts});;All files (*)")
        if path:
            self.show_page("burn")
            self.open_game(path)

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a folder (homebrew, an Xbox game or files to burn)",
                                                os.path.expanduser("~"))
        if path:
            self.show_page("burn")
            self.open_game(path)

    def clear_game(self):
        self.cancel_loading()
        if self.job:
            self.job.cleanup()
        self.job = None
        self.last_path = None
        self.burn_page.game.show_empty()
        self.fill_library()
        self.refresh()

    def cancel_loading(self):
        if self.loading:
            self.loading.cancel.set()
            self.loading = None
            self.load_token += 1
            self.burn_page.game.show_empty()
            self.refresh()

    def options(self) -> dict:
        o = dict(self.dc_opts, region_free=self.settings.value("dc_region_free", True, bool),
                 vga=self.settings.value("dc_vga", True, bool), libcrypt=self.settings.value("libcrypt", True, bool))
        return {k: v for k, v in o.items() if v is not None or k == "scrambled"}

    def open_game(self, path: str, keep_opts: bool = False):
        if self.burning:
            self.toast.show_message("Wait until the current disc is finished.", "warn")
            return
        path = os.path.abspath(os.path.expanduser(path))
        if not keep_opts:
            self.dc_opts = {"title": None, "scrambled": None, "gd_lba": False}
        if self.job:
            self.job.cleanup()
            self.job = None
        if self.loading:
            self.loading.cancel.set()
        self.load_token += 1
        token = self.load_token
        self.last_path = path
        self.burn_page.show_setup()
        self.burn_page.game.show_loading("Looking at " + os.path.basename(path), None)
        self.refresh()
        opts = self.options()

        def work(w: Worker):
            return J.open_job(path, progress=lambda t, f: w.progress.emit(t, f), cancel=w.cancel, opts=opts)

        def progress(text: str, frac: float):
            if token == self.load_token:
                self.burn_page.game.show_loading(text, frac)

        def done(job: J.Job):
            if token != self.load_token:
                job.cleanup()
                return
            self.loading = None
            if job.method == "none" or job.ident.platform == "unknown":
                why = job.blockers[0] if job.blockers else "DiscForge doesn't recognise this file."
                job.cleanup()
                self.burn_page.game.show_error("This file can't be burned", friendly(why))
                self.refresh()
                return
            self.job = job
            self.remember(path)
            self.burn_page.game.show_job(job)
            self.fill_library()
            self.refresh()

        def failed(msg: str):
            if token != self.load_token:
                return
            self.loading = None
            if msg == "Cancelled.":
                self.burn_page.game.show_empty()
                self.refresh()
                return
            pkg = re.search(r"\(package ([\w+.-]+)\)|pacman -S ([\w+.-]+)", msg)
            if pkg:
                name = pkg.group(1) or pkg.group(2)
                self.burn_page.game.show_error("One more tool is needed", friendly(msg), f"Install {name}",
                                               lambda: self.install_packages([name], path))
            else:
                self.burn_page.game.show_error("This file can't be burned", friendly(msg))
            self.refresh()

        self.loading = self.run(work, done, failed, progress=progress)

    def job_menu(self, anchor: QWidget):
        job = self.job
        if not job:
            return
        m = QMenu(self)
        m.addAction(I.icon("folder-open", T.P.text), "Show the file in its folder",
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(job.source))))
        if job.method == "dvd" and job.dvd and job.dvd.image != job.source:
            m.addAction(I.icon("save", T.P.text), "Save the converted image…", self.save_image)
        if job.method == "cd" and any(k == "LibCrypt" and "removed" in v for k, v in job.details):
            m.addAction(I.icon("save", T.P.text), "Save the patched game…", self.save_image)
        if job.method == "dc":
            if self.emu and not job.blockers:
                m.addAction(I.icon("play", T.P.text), "Test in the emulator", self.test_emulator)
            m.addAction(I.icon("save", T.P.text), "Save as a CDI file…", self.save_cdi)
            if job.disc is not None and getattr(job.disc, "kind", "") == "homebrew":
                m.addAction(I.icon("sliders-horizontal", T.P.text), "Homebrew options…", self.homebrew_options)
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def save_image(self):
        job = self.job
        if not job:
            return
        if job.method == "dvd":
            src = job.dvd.image
            ext = os.path.splitext(src)[1] or ".iso"
            start = os.path.join(os.path.dirname(job.source), f"{job.title}{ext}")
            path, _ = QFileDialog.getSaveFileName(self, "Save the converted image", start)
            if path:
                self.run(lambda _w: shutil.copyfile(src, path), lambda _r: self.toast.show_message(
                    f"Saved {os.path.basename(path)}"))
            return
        bins = [v.file for v in job.cd.verify]
        if not bins:
            return
        start = os.path.join(os.path.dirname(job.source), f"{job.title} (LibCrypt patched).bin")
        path, _ = QFileDialog.getSaveFileName(self, "Save the patched game", start, "CUE/BIN image (*.bin)")
        if not path:
            return
        if not path.lower().endswith(".bin"):
            path += ".bin"
        src_cue = job.ident.cue

        def work(_w):
            shutil.copyfile(bins[0], path)
            if src_cue is not None:
                with open(src_cue.path, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
                first = os.path.basename(src_cue.tracks[0].file)
                text = text.replace(first, os.path.basename(path), 1)
                with open(os.path.splitext(path)[0] + ".cue", "w") as f:
                    f.write(text)
            return path

        self.run(work, lambda p: self.toast.show_message(f"Saved {os.path.basename(p)}"))

    def homebrew_options(self):
        from .dialogs import HomebrewDialog
        dlg = HomebrewDialog(self, self.dc_opts)
        if dlg.exec() and self.last_path:
            self.dc_opts = dlg.values()
            self.open_game(self.last_path, keep_opts=True)

    def test_emulator(self):
        job = self.job
        if not job or job.method != "dc" or job.blockers:
            return
        plan = job.plan

        def work(_w):
            path = emulator.build_test_image(plan)
            emulator.launch(path)
            return path

        self.toast.show_message("Starting the emulator with exactly what will be burned…", "info")
        self.run(work, lambda _p: None)

    def save_cdi(self):
        job = self.job
        if not job or job.method != "dc" or job.blockers:
            return
        from ..cdi import write_cdi
        base = os.path.splitext(os.path.basename(os.path.normpath(job.source)))[0]
        start = os.path.join(os.path.dirname(job.source), base + " (selfboot).cdi")
        path, _ = QFileDialog.getSaveFileName(self, "Save as CDI file", start, "CDI image (*.cdi)")
        if not path:
            return
        path = path if path.lower().endswith(".cdi") else path + ".cdi"
        plan, vol = job.plan, (job.title[:32] or "DREAMCAST")
        self.run(lambda _w: write_cdi(path, plan.cdi_sessions(), vol),
                 lambda _r: self.toast.show_message(f"Saved {os.path.basename(path)}"))

    # ------------------------------------------------------------------ drives
    def refresh_drives(self):
        for d in self.drives.values():
            d.status, d.tries, d.state = -99, 0, "busy"
        self.poll_drives()

    def refresh_everything(self):
        self.refresh_drives()
        self.start_scan()

    def drive_count(self) -> int:
        return len(self.drives)

    def poll_drives(self):
        if self.burning:
            return
        listed = devices.list_drives()
        devs = [x.dev for x in listed]
        changed = set(devs) != set(self.drives)
        for dev in list(self.drives):
            if dev not in devs:
                del self.drives[dev]
        for x in listed:
            d = self.drives.get(x.dev)
            if d is None:
                d = self.drives[x.dev] = DriveState(x.dev, x.name or "Disc writer", x.accessible, x.writes, x.group)
                changed = True
            d.accessible, d.writes, d.group = x.accessible, x.writes, x.group
            if not x.accessible:
                if d.state != "noperm":
                    d.state, changed = "noperm", True
                continue
            if d.state == "reading":
                continue
            try:
                st = B.drive_status(x.dev)
            except OSError:
                st = -1
            if st == d.status:
                if st == B.CDS_NO_INFO and d.state in ("empty", "error", "unknown") and \
                        time.monotonic() - d.last_read > 8:
                    self.read_media(d)
                continue
            d.status, changed = st, True
            d.media, d.error = None, ""
            if st in (B.CDS_DISC_OK, B.CDS_NO_INFO):
                self.read_media(d)
            else:
                d.tries = 0
                d.state = {B.CDS_NO_DISC: "empty", B.CDS_TRAY_OPEN: "open",
                           B.CDS_DRIVE_NOT_READY: "busy"}.get(st, "empty")
        if self.current_dev not in self.drives:
            self.current_dev = devs[0] if devs else None
        if changed:
            self.refresh()

    def read_media(self, d: DriveState):
        d.state = "reading"
        d.last_read = time.monotonic()
        dev, need_caps = d.dev, d.caps is None
        self.refresh()

        def work(_w):
            m = B.media_info(dev)
            if not m.present:
                from ..dvd import media_from_mediainfo
                m = media_from_mediainfo(dev) or m
            caps = B.drive_caps(dev) if need_caps and m.present else None
            return m, caps

        def done(res):
            m, caps = res
            dd = self.drives.get(dev)
            if dd is None:
                return
            if caps:
                dd.caps = caps
            dd.media = m
            if not m.present:
                if dd.status == B.CDS_NO_INFO:
                    dd.state = "empty"
                else:
                    dd.tries += 1
                    if dd.tries < 3:
                        dd.state, dd.status = "busy", -99
                        return
                    dd.state, dd.error = "error", "The drive didn't report a disc."
            elif m.blank:
                dd.state = "ready"
            elif m.erasable:
                dd.state = "rw"
            else:
                dd.state = "used"
            self.refresh()

        def failed(msg):
            dd = self.drives.get(dev)
            if dd:
                dd.state, dd.error = "error", msg
                self.refresh()

        self.run(work, done, failed)

    def drive(self) -> DriveState | None:
        return self.drives.get(self.current_dev or "")

    def media_text(self, m: B.Media | None) -> str:
        if not m or not m.kind:
            return "disc"
        return m.kind.replace("/DL", " DL")

    def dye_for(self, d: DriveState) -> str:
        m = d.media
        if d.state == "used":
            return "used"
        if m and m.kind.upper().startswith("DVD"):
            return "dl" if "DL" in m.kind.upper() else "dvd"
        return "cdr"

    def drive_status_text(self, d: DriveState) -> str:
        m = d.media
        return {"empty": "No disc inserted.", "open": "The tray is open.", "busy": "Reading the disc…",
                "reading": "Reading the disc…", "unknown": "Checking…",
                "noperm": "This account isn't allowed to use the drive yet.",
                "error": d.error or "The drive reported a problem.",
                "used": f"{self.media_text(m)} that has already been written.",
                "rw": f"Rewritable {self.media_text(m)} with data on it — erase it to reuse it.",
                "ready": f"Blank {self.media_text(m)}" + (f", {human_size((m.capacity or 0) * 2048)} free."
                                                          if m and m.capacity else ".")}.get(d.state, "")

    def drive_menu(self, anchor: QWidget):
        m = QMenu(self)
        for d in self.drives.values():
            a = m.addAction(d.name, lambda dev=d.dev: self.choose_drive(dev))
            a.setCheckable(True)
            a.setChecked(d.dev == self.current_dev)
        if not self.drives:
            m.addAction("No disc writer found").setEnabled(False)
        d = self.drive()
        if d:
            m.addSeparator()
            if d.state == "noperm":
                m.addAction("Allow access to the drive…", lambda: self.drive_action(d.dev, "perm"))
            if d.state == "rw":
                m.addAction(I.icon("trash-2", T.P.text), "Erase this disc…", lambda: self.drive_action(d.dev, "erase"))
            m.addAction(I.icon("eject", T.P.text), "Open the tray", lambda: self.drive_action(d.dev, "eject"))
            m.addAction("Close the tray", lambda: self.drive_action(d.dev, "close"))
            m.addAction(I.icon("refresh-cw", T.P.text), "Look at the disc again",
                        lambda: self.drive_action(d.dev, "retry"))
        m.addSeparator()
        m.addAction(I.icon("hard-drive", T.P.text), "All drives…", lambda: self.show_page("drives"))
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def choose_drive(self, dev: str):
        self.current_dev = dev
        self.settings.setValue("drive", dev)
        self.refresh()

    def drive_action(self, dev: str, act: str):
        d = self.drives.get(dev)
        if not d or self.burning:
            return
        if act == "eject":
            B.eject(dev)
        elif act == "close":
            B.close_tray(dev)
        elif act == "retry":
            d.status, d.tries, d.state = -99, 0, "busy"
        elif act == "perm":
            od = devices.OpticalDrive(dev, d.name, d.accessible, d.writes, group=d.group)
            cmd = devices.fix_permission_command(od)
            if not cmd:
                QMessageBox.information(self, APP_NAME, "Give your account read/write access to the drive in your "
                                        "system settings, then look again.")
                return
            ok, err = run_as_admin(cmd)
            if ok:
                QMessageBox.information(self, APP_NAME, "Done. Log out and back in once, then the drive will work.")
            elif err:
                QMessageBox.warning(self, APP_NAME, f"That didn't work:\n{err}")
        elif act == "erase":
            self.erase_disc(d)
            return
        QTimer.singleShot(400, self.poll_drives)

    def erase_disc(self, d: DriveState):
        if not d.media:
            return
        if QMessageBox.question(self, APP_NAME, f"Erase everything on this {self.media_text(d.media)}?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) != QMessageBox.StandardButton.Yes:
            return
        from ..engine import erase
        dev, kind = d.dev, d.media.kind
        d.state = "reading"
        self.refresh()

        def done(_r):
            dd = self.drives.get(dev)
            if dd:
                dd.status, dd.tries, dd.state = -99, 0, "busy"
            self.toast.show_message("The disc is erased.")
            QTimer.singleShot(300, self.poll_drives)

        def failed(msg):
            QMessageBox.warning(self, APP_NAME, msg)
            done(None)

        self.run(lambda _w: erase(dev, kind), done, failed)

    # ------------------------------------------------------------------ state -> UI
    def readiness(self) -> tuple[bool, str]:
        job, d = self.job, self.drive()
        if self.loading:
            return False, "Getting the game ready…"
        if self.installing:
            return False, "Installing the burning tools…"
        if not job:
            return False, "Choose a game first."
        if job.blockers:
            return False, "This game can't be burned — see Compatibility below."
        if missing_tool(job):
            return True, f"Burn installs {missing_tool(job)[1]} first."
        if not d:
            return False, "Connect a CD/DVD writer."
        need = P.media_label(job.media)
        if d.state == "noperm":
            return False, "Allow access to the drive first (Drives page)."
        if d.state in ("empty", "open", "unknown", "error"):
            return False, f"Insert a blank {need}."
        if d.state in ("reading", "busy"):
            return False, "Checking the disc…"
        why = self.media_problem(job, d.media) if d.media else f"Insert a blank {need}."
        if why:
            return False, why
        overwrite = job.method == "dvd" and d.media and d.media.kind.upper().startswith("DVD+RW")
        if d.state == "rw" and d.media and not d.media.blank and not overwrite:
            return False, "This rewritable disc isn't empty. Erase it (Drive menu) or insert a blank one."
        return True, ""

    @staticmethod
    def media_problem(job: J.Job, m: B.Media) -> str | None:
        return media_ok(m, job.media, job.sectors, job.platform.id, job.ident.plus_dl_only)

    def burn_speed(self, job: J.Job, d: DriveState | None) -> int:
        pref = self.settings.value("speed", "safe")
        speeds = d.caps.cd_write_speeds if d and d.caps else []
        if pref == "fast":
            return 0
        base = job.platform.speed if job.method != "dc" else B.DREAMCAST_SPEED
        if pref == "balanced":
            base *= 2
        if job.method in ("dc", "cd"):
            return B.choose_speed(speeds, base)
        return base if pref == "balanced" else 0

    def write_mode(self, job: J.Job) -> str:
        if job.method == "dc":
            return "2 sessions (selfboot)"
        if job.method == "cd":
            return "Raw disc-at-once" if job.cd and job.cd.raw else "Disc-at-once"
        return "Disc-at-once, closed" if job.media != "dvd-dl" else "Dual layer, closed"

    def refresh(self):
        bp = self.burn_page
        d = self.drive()
        job = self.job
        # top bar pill + sidebar
        if not self.drives:
            self.topbar.pill.set("No disc writer", "", T.P.text3)
        elif d:
            dot = {"ready": T.P.ok, "used": T.P.warn, "rw": T.P.warn, "noperm": T.P.warn, "error": T.P.bad,
                   "reading": T.P.info, "busy": T.P.info}.get(d.state, T.P.text3)
            short = {"ready": f"Blank {self.media_text(d.media)}", "empty": "No disc", "open": "Tray open",
                     "reading": "Reading…", "busy": "Reading…", "noperm": "No access", "error": "Problem",
                     "used": "Used disc", "rw": "Not empty", "unknown": "Checking…"}.get(d.state, "")
            self.topbar.pill.set(d.name, short, dot)
        self.sidebar.nav["drives"].set_badge(str(len(self.drives)) if len(self.drives) > 1 else "")
        self.drives_page.set_drives(list(self.drives.values()))
        # disc card
        need = P.media_label(job.media) if job else "CD or DVD"
        m = d.media if d else None
        if not d:
            bp.disc.show("none", "cdr", "No disc writer", "Connect a CD/DVD writer.", None, None, "", False)
        else:
            title = {"ready": f"Blank {self.media_text(m)}", "empty": f"Insert a blank {need}",
                     "open": "The tray is open", "reading": "Reading the disc…", "busy": "Reading the disc…",
                     "noperm": "No permission for the drive", "error": "The drive has a problem",
                     "used": "This disc is already used", "rw": f"{self.media_text(m)} isn't empty",
                     "unknown": "Checking the drive…"}.get(d.state, "")
            used = total = None
            caption = ""
            if job and not job.blockers:
                cap = (m.capacity if m and m.capacity and d.state == "ready" else
                       {"cd": P.CD80, "dvd": P.DVD5, "dvd-dl": P.DVD9}[job.media])
                used, total = job.sectors, cap
                unit = 2352 if job.method == "cd" else 2048
                caption = f"{human_size(job.size_bytes)} of {human_size(cap * unit)}"
            ok_media = d.state == "ready" and (not job or not self.media_problem(job, m))
            bp.disc.show(d.state, self.dye_for(d), title, d.name, used, total, caption, ok_media)
        # burn card
        ok, why = self.readiness()
        bp.burn.set_ready(ok, why, bool(job and not job.blockers))
        if job and not job.blockers:
            sp = self.burn_speed(job, d)
            unit = "CD" if job.media == "cd" else "DVD"
            bp.burn.set_summary([("Console", job.platform.name), ("Disc", P.media_label(job.media)),
                                 ("Write mode", self.write_mode(job)),
                                 ("Speed", f"{sp}× {unit}" if sp else "Automatic")])
        else:
            bp.burn.set_summary([])
        # compatibility notes
        if job:
            plat = job.platform
            details = [(k, v) for k, v in job.details if v][:6]
            if job.game_id:
                details.insert(0, ("Game ID", job.game_id))
            details.insert(0, ("Source", os.path.basename(job.source)))
            notes = [(friendly(b), "bad") for b in job.blockers]
            miss = missing_tool(job)
            if miss:
                notes.append((f"Burning {plat.name} discs needs {miss[0]}, which isn't installed yet. "
                              "Burn installs it first.", "warn"))
            notes += [(friendly(w), "warn") for w in job.warnings[:4]]
            notes += [(f"LibCrypt copy protection: {v}.", "ok") for k, v in job.details if k == "LibCrypt"]
            m80 = d.media if d else None
            if (plat.id in ("ps1", "ps2") and job.media == "cd" and m80 and m80.capacity
                    and m80.capacity > 340_000):
                notes.append(("This is an 80-minute CD-R. Early PS2 consoles (SCPH-10000 to 39004) can't read some "
                              "games from those; 74-minute discs work everywhere.", "info"))
            if not job.blockers:
                notes.append((plat.needs, "info"))
            bp.set_notes(details, notes)
        else:
            bp.set_notes([], [])

    def verify_enabled(self) -> bool:
        return self.burn_page.burn.verify.isChecked()

    # ------------------------------------------------------------------ burning
    def start_burn(self):
        ok, _why = self.readiness()
        job, d = self.job, self.drive()
        if not ok or not job:
            return
        miss = missing_tool(job)
        if miss:
            self.install_packages([miss[1]], None, then=self.start_burn)
            return
        if not d:
            return
        simulate = self.settings.value("simulate", False, bool)
        verify = self.verify_enabled()
        opts = B.BurnOptions(dev=d.dev, speed=self.burn_speed(job, d), simulate=simulate, verify=verify,
                             eject=self.settings.value("eject", True, bool))
        self.burn_opts = opts
        self.burn_log = [f"DiscForge {__version__} on {sys.platform}: {job.title} ({job.platform.name}) -> "
                         f"{d.name} ({d.dev}), {self.media_text(d.media)}"]
        bp = self.burn_page
        bp.progress.start(job, verify, simulate)
        bp.progress.visual.reduce_motion = self.settings.value("reduce_motion", False, bool)
        bp.show_progress()
        self.show_page("burn")
        self.burner_holder = []
        holder = self.burner_holder
        self.taskbar.set(0.0)
        self.burn_started = time.monotonic()
        self.burning = self.run(lambda w: J.burn_job(job, opts, log=w.log.emit, events=w.event.emit, holder=holder),
                                self.burn_done, self.burn_failed, event=self.burn_event,
                                log=lambda s: self.burn_log.append(s))
        self.sidebar.nav["burn"].set_badge("•")

    def burn_event(self, e: dict):
        pv = self.burn_page.progress
        st = e.get("stage")
        if st == "prepare":
            pv.steps.set(0)
            return
        if st == "write":
            frac = float(e.get("frac", 0.0))
            pv.visual.set("write", frac)
            pv.steps.set(1)
            if pv.write_t0 is None:
                pv.write_t0 = time.monotonic()
            pv.percent.setText(f"{int(frac * 100)}%")
            sess = f" — part {e['session']} of {e['sessions']}" if e.get("sessions", 1) > 1 else ""
            pv.stage.setText(("Practice writing" if self.burn_opts.simulate else "Writing the disc") + sess)
            pv.bar.set(frac)
            if e.get("mb_total"):
                pv.s_written.set(f"{e.get('mb', 0):,} / {e['mb_total']:,} MB")
            if e.get("speed"):
                pv.s_speed.set(f"{e['speed']:.0f}×")
            eta = e.get("eta")
            if eta is None and frac > 0.03 and pv.write_t0:
                el = time.monotonic() - pv.write_t0
                eta = el / frac * (1 - frac)
            if eta is not None:
                pv.s_left.set(duration(eta))
            self.taskbar.set(frac * (0.85 if self.burn_opts.verify else 1.0))
        elif st in ("fixate", "check"):
            pv.visual.set("fixate", 1.0)
            pv.steps.set(2)
            pv.percent.setText("100%")
            pv.bar.set(None)
            pv.stage.setText("Finishing the disc — almost there")
            pv.s_left.set("")
        elif st == "verify":
            part = float(e.get("part", 0.0))
            pv.visual.set("verify", 1.0, part)
            pv.steps.set(3)
            pv.percent.setText(f"{int(part * 100)}%")
            pv.bar.set(part, T.P.info)
            pv.stage.setText("Checking the disc against the game")
            self.taskbar.set(0.85 + 0.15 * part)

    def cancel_burn(self):
        b = self.burner_holder[0] if self.burner_holder else None
        if not self.burning:
            return
        writing = bool(b and getattr(b, "writing", False))
        msg = ("The disc is being written. Stopping now leaves it unusable." if writing
               else "Stop before anything is written?")
        if QMessageBox.question(self, APP_NAME, msg + "\n\nStop burning?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) != QMessageBox.StandardButton.Yes:
            return
        if b:
            b.cancel()
        pv = self.burn_page.progress
        pv.cancel.setEnabled(False)
        pv.cancel.setText("Stopping…")

    def _finish(self):
        self.burning = None
        self.taskbar.set(None)
        self.sidebar.nav["burn"].set_badge("")
        QTimer.singleShot(200, self.poll_drives)

    def _record(self, result: str, verified: bool | None):
        job = self.job
        d = self.drive()
        entry = {"time": time.time(), "title": job.title if job else "", "platform": job.platform.id if job else "cd",
                 "source": job.source if job else "", "media": self.media_text(d.media) if d else "",
                 "drive": d.name if d else "", "result": result, "verified": verified,
                 "seconds": round(time.monotonic() - getattr(self, "burn_started", time.monotonic())),
                 "log": "\n".join(self.burn_log)[-200_000:]}
        self.history.insert(0, entry)
        self.history = self.history[:200]
        self._save_history()
        self.history_page.set_entries(self.history)

    def next_disc(self) -> str | None:
        """Disc 2 after Disc 1 of a multi-disc game, if it's next to it."""
        src = self.job.source if self.job else ""
        m = re.search(r"\((Disc|CD)\s*(\d+)\)", os.path.basename(src), re.I)
        if not m:
            return None
        nxt = os.path.basename(src).replace(m.group(0), f"({m.group(1)} {int(m.group(2)) + 1})")
        cand = os.path.join(os.path.dirname(src), nxt)
        return cand if os.path.exists(cand) else None

    def burn_done(self, res: B.BurnResult):
        self._finish()
        job = self.job
        plat = job.platform if job else P.get("cd")
        log = "\n".join(self.burn_log)
        took = duration(time.monotonic() - getattr(self, "burn_started", time.monotonic()))
        facts = [("Console", plat.name), ("Disc", P.media_label(job.media) if job else ""), ("Time", took)]
        tips = [(t, "ok") for t in plat.tips] + [(plat.needs, "info")]
        self._next = self.next_disc() if res.ok and not res.simulated else None
        rv = self.burn_page.result
        if res.simulated:
            self._record("practice", None)
            rv.show_result("ok", "Practice run complete", "Everything worked with the laser off. Turn off "
                           "practice runs in Settings to burn for real.", facts, [], "Back to the game", "Done", log)
        elif res.ok:
            self._record("ok", res.verified)
            checked = res.verified is True
            facts.append(("Check", "Every sector matches the game" if checked else
                          "Partly checked" if self.burn_opts.verify else "Skipped"))
            primary = f"Burn {os.path.basename(self._next)[:40]}…" if self._next else "Burn another copy"
            notes = [(m, "info") for m in res.messages] if not checked and self.burn_opts.verify else []
            rv.show_result("ok", "Your disc is ready", f"{job.title if job else 'The game'} was burned"
                           + (" and checked." if checked else "."), facts, notes + tips, primary, "Done", log)
            if not self.isActiveWindow():
                notify("Your disc is ready", job.title if job else "")
        else:
            self._record("verify", False)
            rv.show_result("warn", "The disc didn't check out", "Reading it back found differences, so it may not "
                           "work. Burn another copy on a different brand of disc.", facts,
                           [(m, "warn") for m in res.messages], "Try again", "Done", log)
            notify("The disc didn't check out", job.title if job else "")
        self.burn_page.show_result()
        self.refresh()

    def burn_failed(self, msg: str):
        self._finish()
        log = "\n".join(self.burn_log)
        self._next = None
        rv = self.burn_page.result
        b = self.burner_holder[0] if self.burner_holder else None
        if msg.startswith("Cancelled") and b is not None and getattr(b, "written", False):
            self._record("ok", None)
            rv.show_result("warn", "Check stopped", "The disc was written completely, but checking it was "
                           "stopped, so it hasn't been compared with the game.", [], [], "Burn another copy",
                           "Done", log + "\nCheck cancelled.")
        elif msg.startswith("Cancelled"):
            self._record("cancelled", None)
            rv.show_result("warn", "Burning stopped", "If writing had started, that disc can't be used any more.",
                           [], [], "Try again", "Done", log + "\nCancelled.")
        else:
            self._record("failed", None)
            rv.show_result("bad", "Burning didn't work", friendly(msg), [], [], "Try again", "Done", log + "\n" + msg)
            notify("Burning didn't work", msg)
        self.burn_page.show_result()
        self.refresh()

    def result_primary(self):
        nxt = getattr(self, "_next", None)
        self.burn_page.show_setup()
        if nxt:
            self.open_game(nxt)
        else:
            self.refresh_drives()
        self.refresh()

    def result_secondary(self):
        self.burn_page.show_setup()
        self.refresh()

    # ------------------------------------------------------------------ history
    def _history_path(self) -> str:
        return os.path.join(config_dir(), "history.json")

    def _load_history(self) -> list[dict]:
        try:
            with open(self._history_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        tmp = self._history_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.history, f)
            os.replace(tmp, self._history_path())
        except OSError:
            pass

    def clear_history(self):
        if QMessageBox.question(self, APP_NAME, "Clear the burn history?") == QMessageBox.StandardButton.Yes:
            self.history = []
            self._save_history()
            self.history_page.set_entries([])

    # ------------------------------------------------------------------ settings actions
    def clear_cache(self):
        if self.burning or self.loading:
            self.toast.show_message("Wait until the current job is finished.", "warn")
            return
        if self.job:
            self.job.cleanup()
            self.job = None
            self.burn_page.game.show_empty()
        J.clear_conversions()
        clean_cache(0)
        self.settings_page.cache.setText(human_size(_cache_size()))
        self.toast.show_message("Prepared games cleared.")
        self.refresh()

    def _update_tools(self):
        rows, missing = [], False
        for exe in tools.REQUIRED + tools.OPTIONAL:
            p = tools.find_any(exe)
            where = "built in" if p and tools.bundled(exe) else (p or "not found")
            rows.append((exe, where, bool(p)))
            missing |= not p and exe in tools.REQUIRED
        self.settings_page.set_tools(rows, missing and install_command(["cdrtools"]) is not None)
        self.settings_page.about.setText(
            f"DiscForge {__version__} · Python {sys.version.split()[0]} · Qt {qt_version()}\n"
            f"LibCrypt patches from LibCrypt Patcher by Alex Free (BSD 3-Clause). Interface font Inter (SIL OFL), "
            f"icons Lucide (ISC).")

    def install_tools(self):
        need = sorted({tools.package(e) for e in tools.REQUIRED if not tools.find_any(e)})
        self.install_packages(need, None)

    def install_packages(self, pkgs: list[str], retry: str | None, then=None):
        """Install system packages in the background (the package manager can take minutes), then
        reopen the game (`retry`) or carry on with what was asked (`then`)."""
        cmd = install_command(pkgs)
        if not cmd:
            QMessageBox.information(self, APP_NAME, "Install these with your system's package manager: "
                                    + ", ".join(pkgs))
            return
        if self.installing:
            return

        def work(_w):
            if os.path.basename(cmd[0]) == "brew":          # Homebrew refuses to run as root
                r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                return r.returncode == 0, (r.stderr or r.stdout).strip()[-600:]
            return run_as_admin(cmd)

        def finish(res):
            self.installing = None
            ok, err = res
            self._update_tools()
            if ok:
                self.toast.show_message("Installed.")
                if retry:
                    self.open_game(retry)
                elif then:
                    then()
            elif err:
                QMessageBox.warning(self, APP_NAME, f"That didn't work:\n{err}")
            self.refresh()

        def failed(msg):
            self.installing = None
            QMessageBox.warning(self, APP_NAME, f"That didn't work:\n{msg}")
            self.refresh()

        self.toast.show_message(f"Installing {', '.join(pkgs)}…", "info", 600_000)
        self.installing = self.run(work, finish, failed)
        self.refresh()

    def show_licences(self):
        from .dialogs import TextDialog
        parts = []
        for rel in ("data/libcrypt/LICENSE", "data/fonts/OFL.txt", "data/icons/LICENSE-lucide.txt",
                    "data/IP.TMPL.LICENSE"):
            try:
                parts.append(f"==== {rel} ====\n" + resources.files("discforge").joinpath(rel).read_text())
            except OSError:
                pass
        TextDialog(self, "Licences", "\n\n".join(parts)).exec()

    def open_project_page(self):
        QDesktopServices.openUrl(QUrl(PROJECT_URL))

    def show_help(self):
        box = QMessageBox(self)
        box.setWindowTitle("How DiscForge works")
        box.setIconPixmap(app_icon().pixmap(56, 56))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(HELP)
        box.exec()

    # ------------------------------------------------------------------ window events
    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls() and not self.burning:
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.show_page("burn")
            self.open_game(paths[0])

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.toast.isVisible():
            par = self.toast.parentWidget()
            self.toast.move(par.width() - self.toast.width() - 24, par.height() - self.toast.height() - 24)

    def closeEvent(self, e):  # noqa: N802
        if self.burning:
            QMessageBox.warning(self, APP_NAME, "A disc is being burned. Please wait until it's finished.")
            e.ignore()
            return
        if self.installing:
            QMessageBox.information(self, APP_NAME, "The burning tools are still being installed. "
                                    "Please wait a moment.")
            e.ignore()
            return
        self.settings.setValue("size", self.size())
        if self.current_dev:
            self.settings.setValue("drive", self.current_dev)
        self.poll.stop()
        self.clock.stop()
        for w in list(self.workers):
            w.cancel.set()
        for w in list(self.workers):
            w.wait(5000)
        if self.job:
            self.job.cleanup()
        e.accept()


def missing_tool(job: J.Job) -> tuple[str, str] | None:
    dvd = ["cdrecord"] if tools.DVD_WITH_CDRECORD or not tools.find("growisofs") else ["growisofs"]
    need = {"dc": ["cdrecord", "mkisofs"], "cd": ["cdrdao"], "dvd": dvd}.get(job.method, [])
    for exe in need:
        if not tools.find(exe):
            return exe, tools.package(exe)
    return None


def qt_version() -> str:
    from PySide6.QtCore import qVersion
    return qVersion()


def _cache_size() -> int:
    total = 0
    for root, _d, files in os.walk(cache_dir()):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
                total += getattr(st, "st_blocks", st.st_size // 512) * 512
            except OSError:
                pass
    return total
