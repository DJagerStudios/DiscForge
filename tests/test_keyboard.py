"""Keyboard use: every control can be reached and worked without a mouse."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DISCFORGE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="discforge-cfg-")
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="discforge-cache-")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:                                     # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class Keyboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from discforge.gui import theme as T, window as W
        cls.app = QApplication.instance() or QApplication([])
        T.load_fonts()
        W.open_settings().setValue("intro", False)
        cls._find_emulator = W.emulator.find_emulator
        W.emulator.find_emulator = lambda: None
        cls.W = W
        cls.win = W.MainWindow(None)
        cls.win.resize(1280, 820)
        cls.win.show()
        QApplication.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.win.poll.stop()
        cls.win.clock.stop()
        for w in list(cls.win.workers):
            w.wait(5000)
        cls.win.close()
        QApplication.processEvents()
        cls.W.emulator.find_emulator = cls._find_emulator

    def test_shortcuts_switch_pages(self):
        for key, page in ((Qt.Key.Key_2, "library"), (Qt.Key.Key_3, "drives"), (Qt.Key.Key_4, "history"),
                          (Qt.Key.Key_1, "burn")):
            QTest.keyClick(self.win, key, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.win.page, page)

    def test_sidebar_works_with_space(self):
        nav = self.win.sidebar.nav["history"]
        self.assertEqual(nav.focusPolicy(), Qt.FocusPolicy.TabFocus)
        nav.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(nav, Qt.Key.Key_Space)
        self.assertEqual(self.win.page, "history")
        self.win.show_page("burn")

    def test_toggle_and_segmented(self):
        sp = self.win.settings_page
        t = sp.t_eject
        before = t.isChecked()
        QTest.keyClick(t, Qt.Key.Key_Space)
        self.assertNotEqual(t.isChecked(), before)
        QTest.keyClick(t, Qt.Key.Key_Space)
        seg = sp.speed
        self.assertEqual(seg.focusPolicy(), Qt.FocusPolicy.TabFocus)
        got = []
        seg.changed.connect(got.append)
        seg.set_current("safe")
        QTest.keyClick(seg, Qt.Key.Key_Right)
        QTest.keyClick(seg, Qt.Key.Key_Right)
        QTest.keyClick(seg, Qt.Key.Key_Right)          # already at the end: stays
        QTest.keyClick(seg, Qt.Key.Key_Left)
        self.assertEqual(got, ["balanced", "fast", "balanced"])
        seg.set_current("safe")

    def test_tab_reaches_the_controls(self):
        from discforge.gui.widgets import Button, NavButton, Segmented, Toggle
        self.win.show_page("settings")
        QApplication.processEvents()
        seen = set()
        self.win.sidebar.nav["burn"].setFocus(Qt.FocusReason.TabFocusReason)
        for _ in range(80):
            w = QApplication.focusWidget()
            if w is not None:
                seen.add(type(w))
            self.win.focusNextChild()
            QApplication.processEvents()
        self.assertTrue({NavButton, Toggle, Segmented, Button} <= seen, seen)
        self.win.show_page("burn")


if __name__ == "__main__":
    unittest.main()
