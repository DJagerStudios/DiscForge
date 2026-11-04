"""The start-up intro: the mark draws itself, the wordmark rises in, then everything fades to the app.
About 1.7 s; a click or any key skips it; it never plays with Reduce motion on."""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from . import art
from . import theme as T


class BootOverlay(QWidget):
    finished = Signal()
    LENGTH = 1.7

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(parent.size())
        parent.installEventFilter(self)
        self._t0 = time.monotonic()
        self._skip: float | None = None
        self._done = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._frame)
        self._timer.start(16)

    @property
    def t(self) -> float:
        return time.monotonic() - self._t0

    def eventFilter(self, obj, e):  # noqa: N802
        if obj is self.parent() and e.type() == e.Type.Resize:
            self.resize(self.parent().size())
        return False

    def skip(self):
        if self._skip is None:
            self._skip = self.t

    def mousePressEvent(self, e):  # noqa: N802
        self.skip()

    def keyPressEvent(self, e):  # noqa: N802
        self.skip()

    def _frame(self):
        end = self.LENGTH if self._skip is None else min(self.LENGTH, self._skip + 0.2)
        if self.t >= end and not self._done:
            self._done = True
            self._timer.stop()
            self.hide()
            self.finished.emit()
            self.deleteLater()
            return
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.t
        if self._skip is not None:
            fade = 1 - art.phase(t, self._skip, self._skip + 0.2)
        else:
            fade = 1 - art.ease_in_out(art.phase(t, 1.32, self.LENGTH))
        p.setOpacity(fade)
        p.fillRect(self.rect(), QColor(T.P.bg))
        w, h = self.width(), self.height()
        r = max(34.0, min(w, h) * 0.075)
        cy = h / 2 - r * 0.9
        c = QPointF(w / 2, cy)
        a = art.ease_out(art.phase(t, 0.05, 0.55))
        art.glow(p, c, r * 3.0, T.P.accent, 0.10 * a)
        s = 0.86 + 0.14 * a
        p.save()
        p.setOpacity(fade * a)
        art.paint_disc(p, c, r * 0.68 * s, "silver", angle=40 + t * 60)
        p.restore()
        art.paint_ring(p, c, r * 0.86 * s, max(2.5, r * 0.13), art.ease_out(art.phase(t, 0.12, 0.78)), spin=t * 30,
                       alpha=fade)
        wt = art.ease_out(art.phase(t, 0.42, 0.92))
        if wt > 0:
            px = max(22.0, r * 0.62)
            ww = art.wordmark_width(px)
            art.paint_wordmark(p, w / 2 - ww / 2, cy + r * 1.75 + (1 - wt) * 10, px, alpha=fade * wt)
        tt = art.ease_out(art.phase(t, 0.70, 1.10))
        if tt > 0:
            p.setOpacity(fade * tt)
            p.setFont(T.font(max(12.0, r * 0.26), T.W.Medium, 0.2))
            p.setPen(QColor(T.P.text2))
            p.drawText(QRectF(0, cy + r * 2.15, w, r * 0.7), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "Retro games, homebrew and software — burned right")
