"""Starting DiscForge's window: fonts, theme, single instance, clean shutdown."""
from __future__ import annotations

import getpass
import os
import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from ..util import cache_dir, clean_cache
from . import theme as T
from .window import APP_ID, APP_NAME, MainWindow, app_icon


def _server_name() -> str:
    return f"discforge-{getpass.getuser()}"


def hand_over(path: str | None) -> bool:
    """If DiscForge is already open, give it the file and bring it to the front."""
    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(300):
        return False
    sock.write((os.path.abspath(path) if path else "").encode() + b"\n")
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def listen(window: MainWindow) -> QLocalServer:
    QLocalServer.removeServer(_server_name())
    server = QLocalServer(window)
    server.listen(_server_name())

    def incoming():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read():
            data = bytes(conn.readAll()).decode(errors="replace").strip()
            window.showNormal()
            window.raise_()
            window.activateWindow()
            if data:
                window.show_page("burn")
                window.open_game(data)

        conn.readyRead.connect(read)

    server.newConnection.connect(incoming)
    return server


def self_test(w: MainWindow, app: QApplication, report: str | None) -> None:
    """--self-test: the packaged app opens its real window, checks what a broken package would get
    wrong (fonts, icons, styles, bundled tools, every page), writes a report and quits."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFontDatabase
    from .. import __version__, tools
    from . import icons as I

    def check():
        lines, bad = [f"DiscForge {__version__} self-test, Qt platform {app.platformName()}"], []
        if not w.isVisible():
            bad.append("the window isn't showing")
        if T.FAMILY not in QFontDatabase.families():
            bad.append(f"the {T.FAMILY} font didn't load")
        if not I._renderer("disc-3", "#ffffff", 1.8).isValid():
            bad.append("icons don't render (SVG support missing)")
        for page in ("burn", "library", "drives", "settings", "burn"):
            w.show_page(page)
            app.processEvents()
        if I.MISSING:
            bad.append("missing icons: " + ", ".join(sorted(I.MISSING)))
        for exe in tools.REQUIRED + tools.OPTIONAL:
            p = tools.find_any(exe)
            lines.append(f"  {exe:<17} {'bundled ' if p and tools.bundled(exe) else ''}{p or 'missing'}")
            if not p and exe in tools.REQUIRED and getattr(sys, "frozen", False):
                bad.append(f"{exe} isn't bundled")
        lines += [f"PROBLEM: {b}" for b in bad] or ["OK"]
        text = "\n".join(lines) + "\n"
        if report:
            with open(report, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
        app.exit(1 if bad else 0)

    QTimer.singleShot(2500, check)


def main(path: str | None = None) -> int:
    testing = "--self-test" in sys.argv
    if testing and not os.environ.get("DISCFORGE_CONFIG_DIR"):
        import tempfile
        os.environ["DISCFORGE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="discforge-selftest-")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("DiscForge")
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    if not testing and hand_over(path):
        return 0
    T.load_fonts()
    app.setFont(T.body())
    clean_cache()
    os.makedirs(cache_dir(), exist_ok=True)
    w = MainWindow(path)
    if testing:
        self_test(w, app, os.environ.get("DISCFORGE_SELF_TEST_REPORT"))
    else:
        w._server = listen(w)
    w.show()
    if not testing:
        w.play_intro()
    rc = app.exec()
    w.settings.sync()
    import shiboken6
    shiboken6.delete(w)                     # tear the window down while Qt is still whole
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
