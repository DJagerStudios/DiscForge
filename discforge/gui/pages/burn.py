"""The Burn page: Game -> Disc -> Burn, then the live progress view and the result."""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainter
from PySide6.QtWidgets import QGridLayout, QPlainTextEdit, QScrollArea, QStackedWidget, QWidget

from ... import platforms as P
from .. import art
from .. import icons as I
from .. import theme as T
from ..widgets import (Badge, Button, Callout, Card, Cover, Elided, KeyValues, Meter, ProgressBar, Spinner,
                       Text, Toggle, clear_layout, hbox, vbox, widget_of)


def human_size(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{max(1, round(n / 1e3))} KB"


def duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


class StepHeader(QWidget):
    """'1  Game' with a check once the step is done."""

    def __init__(self, n: int, title: str):
        super().__init__()
        self.n, self.title, self.done, self.active = n, title, False, True
        self.status = ""
        self.setFixedHeight(28)

    def set(self, done: bool, active: bool = True, status: str = ""):
        self.done, self.active, self.status = done, active, status
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        c = QPointF(12, r.center().y())
        if self.done:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T.P.ok))
            p.drawEllipse(c, 11, 11)
            p.setPen(art.pen("#FFFFFF", 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(art.check_path(c, 11))
        else:
            p.setPen(art.pen(T.P.accent if self.active else T.P.border_strong, 1.5))
            p.setBrush(QColor(T.P.accent_soft) if self.active else Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, 10.5, 10.5)
            p.setFont(T.font(12, T.W.Bold))
            p.setPen(QColor(T.P.accent if self.active else T.P.text3))
            p.drawText(QRectF(c.x() - 11, c.y() - 11, 22, 22), Qt.AlignmentFlag.AlignCenter, str(self.n))
        p.setFont(T.headline())
        p.setPen(QColor(T.P.text if self.active or self.done else T.P.text3))
        fm = QFontMetricsF(T.headline())
        tw = fm.horizontalAdvance(self.title)
        p.drawText(QRectF(34, 0, tw + 4, r.height()), Qt.AlignmentFlag.AlignVCenter, self.title)
        if self.status:
            f2 = T.small()
            p.setFont(f2)
            p.setPen(QColor(T.P.text3))
            fm2 = QFontMetricsF(f2)
            avail = r.width() - 34 - tw - 16
            p.drawText(QRectF(r.right() - avail, 0, avail, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       fm2.elidedText(self.status, Qt.TextElideMode.ElideRight, avail))


class DiscGlyph(QWidget):
    """The disc that's in the drive (or an outline when there's none)."""

    def __init__(self, size: int = 92):
        super().__init__()
        self.setFixedSize(size, size)
        self.state, self.dye = "none", "cdr"
        self.angle = 0.0
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set(self, state: str, dye: str):
        self.state, self.dye = state, dye
        self.update()

    def _tick(self):
        self.angle = (self.angle + (1.6 if self.state == "ready" else 0.4)) % 360
        self.update()

    def showEvent(self, e):  # noqa: N802
        self._timer.start(40)
        super().showEvent(e)

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = self.width() / 2 - 4
        t = time.monotonic() - self._t0
        if self.state in ("none", "empty", "open", "noperm", "error", "unknown"):
            art.paint_disc_outline(p, c, r, T.P.text3, -t * 8)
            return
        art.paint_disc(p, c, r, self.dye, self.angle, alpha=0.55 if self.state in ("reading", "busy") else 1.0)
        if self.state in ("reading", "busy"):
            art.paint_progress_ring(p, QRectF(c.x() - r - 2, c.y() - r - 2, 2 * r + 4, 2 * r + 4), None,
                                    T.P.accent, 2.0, t * 280)


# --------------------------------------------------------------------------

class GameCard(Card):
    def __init__(self, win):
        super().__init__(padding=20)
        self.win = win
        self.header = StepHeader(1, "Game")
        self.lay.addWidget(self.header)
        self.stack = QStackedWidget()
        self.lay.addWidget(self.stack, 1)
        # empty
        drop = QWidget()
        b_open = Button("Open a game…", "primary", "folder-open")
        b_open.clicked.connect(win.browse)
        b_lib = Button("Library", "secondary", "library")
        b_lib.clicked.connect(lambda: win.show_page("library"))
        self.drop_title = Text("Drop a game here", T.body_medium(), "text", align=Qt.AlignmentFlag.AlignHCenter)
        self.drop_text = Text("CUE/BIN, ISO, CHD, CDI, GDI, WBFS, RVZ, CSO, archives or a folder — DiscForge "
                              "works out the console and the right disc.", T.small(), "text2", wrap=True,
                              align=Qt.AlignmentFlag.AlignHCenter)
        drop.setLayout(vbox(None, self.drop_title, T.SM, self.drop_text, T.LG, hbox(None, b_open, T.SM, b_lib, None),
                            None, margins=(T.MD, 0, T.MD, 0)))
        self.stack.addWidget(drop)
        # loading
        self.spin = Spinner(22)
        self.load_title = Text("Preparing the game…", T.body_medium(), "text", align=Qt.AlignmentFlag.AlignHCenter)
        self.load_text = Elided("", T.small(), "text2", Qt.AlignmentFlag.AlignHCenter)
        self.load_bar = ProgressBar(4)
        b_cancel = Button("Cancel", "ghost", size="sm")
        b_cancel.clicked.connect(win.cancel_loading)
        self.stack.addWidget(widget_of(vbox(None, hbox(None, self.spin, None), T.MD, self.load_title, T.XS,
                                            self.load_text, T.MD, self.load_bar, T.MD, hbox(None, b_cancel, None),
                                            None, margins=(T.LG, 0, T.LG, 0))))
        # ready
        self.cover = Cover(132, 176)
        self.title = Text("", T.title(), "text", wrap=True)
        self.badge = Badge()
        self.meta = Text("", T.small(), "text2", wrap=True)
        self.b_change = Button("Change", "secondary", "refresh-cw", "sm")
        self.b_change.clicked.connect(win.browse)
        self.b_more = Button("More", "ghost", "ellipsis", "sm")
        self.b_more.clicked.connect(lambda: win.job_menu(self.b_more))
        info = vbox(self.badge, T.SM, self.title, T.XS, self.meta, None, hbox(self.b_change, T.SM, self.b_more, None))
        self.stack.addWidget(widget_of(hbox((self.cover, 0, Qt.AlignmentFlag.AlignTop), T.LG, (info, 1))))
        # error
        self.err_title = Text("", T.body_medium(), "text", wrap=True)
        self.err_text = Text("", T.small(), "text2", wrap=True)
        self.err_action = Button("", "primary", size="sm")
        self.err_back = Button("Choose another game", "secondary", size="sm")
        self.err_back.clicked.connect(win.browse)
        self._err_cb = None
        self.err_action.clicked.connect(lambda: self._err_cb and self._err_cb())
        err_icon = QWidget()
        err_icon.setFixedSize(20, 20)
        err_icon.paintEvent = lambda _e: I.paint(QPainter(err_icon), "circle-x", QRectF(1, 1, 18, 18), T.P.bad)
        self.stack.addWidget(widget_of(vbox(None, hbox((err_icon, 0, Qt.AlignmentFlag.AlignTop), T.MD,
                                                       (vbox(self.err_title, T.XS, self.err_text), 1)), T.LG,
                                            hbox(self.err_action, T.SM, self.err_back, None), None)))
        self.setAcceptDrops(False)

    def show_empty(self):
        self.stack.setCurrentIndex(0)
        self.header.set(False, True, "")

    def show_loading(self, text: str, frac: float | None):
        self.stack.setCurrentIndex(1)
        self.load_text.setText(text)
        self.load_bar.set(frac if frac is not None and frac >= 0 else None)
        self.header.set(False, True, "Preparing…")

    def show_job(self, job):
        plat = job.platform
        self.stack.setCurrentIndex(2)
        self.cover.set(job.title, plat.accent, plat.accent2, plat.short, job.game_id)
        self.title.setText(job.title)
        self.badge.set(plat.name, plat.accent)
        bits = [b for b in (job.game_id, human_size(job.size_bytes), P.media_label(job.media)) if b]
        self.meta.setText("  ·  ".join(bits))
        self.header.set(not job.blockers, True, "")

    def show_error(self, title: str, text: str, action: str = "", cb=None):
        self.stack.setCurrentIndex(3)
        self.err_title.setText(title)
        self.err_text.setText(text)
        self.err_action.setVisible(bool(action))
        self.err_action.setText(action)
        self._err_cb = cb
        self.header.set(False, True, "Can't use this file")


class DiscCard(Card):
    def __init__(self, win):
        super().__init__(padding=20)
        self.win = win
        self.header = StepHeader(2, "Disc")
        self.lay.addWidget(self.header)
        self.glyph = DiscGlyph(92)
        self.state = Text("No disc writer", T.body_medium(), "text", wrap=True)
        self.drive = Elided("", T.small(), "text2")
        self.meter = Meter()
        self.meter_text = Elided("", T.caption(), "text3")
        self.b_drive = Button("Drive", "secondary", "hard-drive", "sm")
        self.b_drive.clicked.connect(lambda: win.drive_menu(self.b_drive))
        top = hbox(self.glyph, T.LG, (vbox(None, self.state, T.XS, self.drive, None), 1))
        self.lay.addLayout(top)
        self.lay.addStretch(1)
        self.lay.addWidget(self.meter)
        self.lay.addWidget(self.meter_text)
        self.lay.addLayout(hbox(self.b_drive, None))

    def show(self, state: str, dye: str, title: str, drive: str, used: float | None, total: float | None,
             caption: str, done: bool):  # noqa: A003
        self.glyph.set(state, dye)
        self.state.setText(title)
        self.drive.setText(drive)
        self.meter.setVisible(used is not None)
        self.meter_text.setVisible(bool(caption))
        if used is not None and total:
            self.meter.set(used, total)
        self.meter_text.setText(caption)
        self.header.set(done, True, "")


class BurnCard(Card):
    def __init__(self, win):
        super().__init__(padding=20)
        self.win = win
        self.header = StepHeader(3, "Burn")
        self.lay.addWidget(self.header)
        self.summary = KeyValues(96)
        self.lay.addWidget(self.summary)
        self.verify = Toggle(True)
        self.verify.toggled.connect(win.verify_changed)
        self.lay.addLayout(hbox(Text("Check the disc afterwards", T.small(), "text2"), None, self.verify))
        self.lay.addStretch(1)
        self.go = Button("Burn disc", "primary", "flame", "lg")
        self.go.clicked.connect(win.start_burn)
        self.lay.addWidget(self.go)
        self.reason = Text("", T.small(), "text2", wrap=True, align=Qt.AlignmentFlag.AlignHCenter)
        self.lay.addWidget(self.reason)

    def set_summary(self, rows: list[tuple[str, str]]):
        self.summary.set(rows)
        self.summary.setVisible(bool(rows))

    def set_ready(self, ok: bool, reason: str, done_before: bool):
        self.go.setEnabled(ok)
        self.reason.setText(reason)
        self.reason.setVisible(bool(reason))
        self.header.set(False, done_before, "")


# --------------------------------------------------------------------------

class BurnVisual(QWidget):
    """A disc turning under a progress ring; the written area grows from the hub outwards."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(176, 176)
        self.frac, self.part, self.stage = 0.0, 0.0, "prepare"
        self.dye, self.accent = "cdr", T.P.accent
        self.angle = 0.0
        self._shown = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.reduce_motion = False

    def set(self, stage: str, frac: float | None = None, part: float | None = None):
        self.stage = stage
        if frac is not None:
            self.frac = max(0.0, min(1.0, frac))
        if part is not None:
            self.part = max(0.0, min(1.0, part))

    def _tick(self):
        speed = 0.0 if self.reduce_motion else {"write": 5.0, "fixate": 3.0, "verify": 2.4}.get(self.stage, 1.0)
        self.angle = (self.angle + speed) % 360
        self._shown += (self.frac - self._shown) * 0.15
        self.update()

    def showEvent(self, e):  # noqa: N802
        self._timer.start(33)
        super().showEvent(e)

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        ring = QRectF(4, 4, self.width() - 8, self.height() - 8)
        prog = self.part if self.stage == "verify" else self._shown
        col = T.P.info if self.stage == "verify" else self.accent
        if self.stage == "prepare":
            art.paint_progress_ring(p, ring, None, col, 6, self.angle * 3)
        else:
            art.paint_progress_ring(p, ring, prog, col, 6)
        r = self.width() / 2 - 22
        art.paint_disc(p, c, r, self.dye, self.angle,
                       written=self._shown if self.stage in ("write", "fixate", "verify") else None, accent=col)
        if self.stage == "write":
            rr = r * (0.36 + 0.60 * self._shown)
            p.setPen(art.pen(T.c("#FFFFFF", 0.55), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, rr, rr)
        elif self.stage == "verify":
            rr = r * (0.36 + 0.60 * self.part)
            p.setPen(art.pen(T.c(T.P.info, 0.9), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, rr, rr)


class StepList(QWidget):
    """Vertical: Prepare / Write / Finish / Check, with status per step."""

    def __init__(self):
        super().__init__()
        self.names = ["Prepare", "Write", "Finish", "Check"]
        self.current = 0
        self.skip_check = False
        self.failed = False
        self.setFixedHeight(4 * 28)

    def set(self, idx: int, failed: bool = False):
        self.current, self.failed = idx, failed
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        names = self.names[:3] if self.skip_check else self.names
        for i, name in enumerate(names):
            y = 14 + i * 28
            c = QPointF(9, y)
            done, cur = i < self.current, i == self.current
            if i < len(names) - 1:
                p.setPen(art.pen(T.P.ok if done else T.P.border_strong, 1.5))
                p.drawLine(QPointF(9, y + 8), QPointF(9, y + 20))
            if done:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(T.P.ok))
                p.drawEllipse(c, 8, 8)
                p.setPen(art.pen("#FFFFFF", 1.7))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(art.check_path(c, 8))
            elif cur:
                col = T.P.bad if self.failed else T.P.accent
                p.setPen(art.pen(col, 1.8))
                p.setBrush(T.c(col, 0.18))
                p.drawEllipse(c, 7.5, 7.5)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(col))
                p.drawEllipse(c, 3, 3)
            else:
                p.setPen(art.pen(T.P.border_strong, 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, 7.5, 7.5)
            p.setFont(T.font(13, T.W.DemiBold if cur else T.W.Normal))
            p.setPen(QColor(T.P.text if cur else T.P.text2 if done else T.P.text3))
            p.drawText(QRectF(28, y - 10, self.width() - 28, 20), Qt.AlignmentFlag.AlignVCenter, name)


class Stat(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self.k = Text(label, T.caption(), "text3")
        self.v = Elided("—", T.body_medium(), "text")
        self.setMinimumWidth(84)
        self.setLayout(vbox(self.k, 2, self.v))

    def set(self, value: str):
        self.v.setText(value or "—")


class ProgressView(Card):
    def __init__(self, win):
        super().__init__(padding=28)
        self.win = win
        self.visual = BurnVisual()
        self.badge = Badge()
        self.title = Text("", T.title(), "text", wrap=True)
        self.percent = Text("0%", T.font(40, T.W.Bold, -1.0), "text")
        self.stage = Text("Getting ready…", T.body_medium(), "text2", wrap=True)
        self.bar = ProgressBar(6)
        self.s_written, self.s_speed, self.s_left, self.s_time = (Stat("Written"), Stat("Speed"), Stat("Time left"),
                                                                   Stat("Elapsed"))
        stats = hbox(self.s_written, T.MD, self.s_speed, T.MD, self.s_left, T.MD, self.s_time, None)
        self.steps = StepList()
        self.note = Callout("Keep the disc in the drive. The computer stays awake until the burn is done.", "info")
        self.cancel = Button("Stop burning", "danger", "circle-stop")
        self.cancel.clicked.connect(win.cancel_burn)
        right = vbox(self.badge, T.SM, self.title, T.LG, hbox(self.percent, None), T.XS, self.stage, T.MD, self.bar,
                     T.LG, stats, T.XL, self.steps, None, self.note, T.LG, hbox(self.cancel, None))
        self.lay.addLayout(hbox((self.visual, 0, Qt.AlignmentFlag.AlignTop), T.XXL, (right, 1)))
        self.t0 = time.monotonic()
        self.write_t0: float | None = None

    def resizeEvent(self, e):  # noqa: N802
        k = 236 if self.width() >= 820 else 176
        if self.visual.width() != k:
            self.visual.setFixedSize(k, k)
        super().resizeEvent(e)

    def start(self, job, verify: bool, simulate: bool):
        plat = job.platform
        self.visual.accent = T.P.accent
        self.visual.dye = "cdr" if job.media == "cd" else "dl" if job.media == "dvd-dl" else "dvd"
        self.visual.set("prepare", 0.0, 0.0)
        self.visual._shown = 0.0
        self.badge.set(plat.name, plat.accent)
        self.title.setText(job.title)
        self.percent.setText("0%")
        self.stage.setText("Practice run — the laser stays off" if simulate else "Checking the disc and the drive…")
        self.bar.set(None)
        for s in (self.s_written, self.s_speed, self.s_left, self.s_time):
            s.set("—")
        self.steps.skip_check = not verify or simulate
        self.steps.set(0)
        self.cancel.setEnabled(True)
        self.cancel.setText("Stop burning")
        self.t0, self.write_t0 = time.monotonic(), None

    def tick_elapsed(self):
        self.s_time.set(duration(time.monotonic() - self.t0))


class ResultView(Card):
    def __init__(self, win):
        super().__init__(padding=32)
        self.win = win
        self.mark = QWidget()
        self.mark.setFixedSize(64, 64)
        self.kind = "ok"
        self.mark.paintEvent = self._paint_mark
        self.title = Text("", T.display(), "text", align=Qt.AlignmentFlag.AlignHCenter)
        self.msg = Text("", T.body(), "text2", wrap=True, align=Qt.AlignmentFlag.AlignHCenter)
        self.msg.setMaximumWidth(560)
        self.facts = KeyValues(120)
        self.facts.setMaximumWidth(440)
        self.tips = vbox(spacing=T.SM)
        tips_w = widget_of(self.tips)
        tips_w.setMaximumWidth(560)
        self.primary = Button("", "primary", size="lg")
        self.secondary = Button("", "secondary", size="lg")
        self.b_log = Button("Show log", "ghost", "scroll-text", "sm")
        self.b_log.clicked.connect(self._toggle_log)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(T.mono())
        self.log.setFixedHeight(160)
        self.log.setMaximumWidth(720)
        self.log.hide()
        self.lay.addLayout(vbox(None, hbox(None, self.mark, None), T.LG, self.title, T.SM,
                                hbox(None, (self.msg, 8), None), T.XL, hbox(None, (self.facts, 8), None), T.LG,
                                hbox(None, (tips_w, 8), None), T.XL,
                                hbox(None, self.primary, T.SM, self.secondary, None), T.MD,
                                hbox(None, self.b_log, None), T.SM, hbox(None, (self.log, 1), None), None))
        self.primary.clicked.connect(win.result_primary)
        self.secondary.clicked.connect(win.result_secondary)

    def _toggle_log(self):
        self.log.setVisible(not self.log.isVisible())
        self.b_log.setText("Hide log" if self.log.isVisible() else "Show log")

    def _paint_mark(self, _e):
        p = QPainter(self.mark)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = {"ok": T.P.ok, "warn": T.P.warn}.get(self.kind, T.P.bad)
        c = QPointF(32, 32)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.c(col, 0.16))
        p.drawEllipse(c, 32, 32)
        p.setBrush(QColor(col))
        p.drawEllipse(c, 22, 22)
        p.setPen(art.pen("#FFFFFF", 3.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "ok":
            p.drawPath(art.check_path(c, 22))
        else:
            p.drawLine(QPointF(32, 21), QPointF(32, 35))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FFFFFF"))
            p.drawEllipse(QPointF(32, 42), 2.2, 2.2)

    def show_result(self, kind: str, title: str, msg: str, facts: list[tuple[str, str]],
                    tips: list[tuple[str, str]], primary: str, secondary: str, log: str):
        self.kind = kind
        self.mark.update()
        self.title.setText(title)
        self.msg.setText(msg)
        self.facts.set(facts)
        self.facts.setVisible(bool(facts))
        clear_layout(self.tips)
        for text, k in tips:
            self.tips.addWidget(Callout(text, k))
        self.primary.setText(primary)
        self.primary.setVisible(bool(primary))
        self.secondary.setText(secondary)
        self.log.setPlainText(log)
        self.log.hide()
        self.b_log.setText("Show log")


# --------------------------------------------------------------------------

class BurnPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.game = GameCard(win)
        self.disc = DiscCard(win)
        self.burn = BurnCard(win)
        self.notes_box = vbox(spacing=T.SM)
        self.details = KeyValues(120)
        self.notes_title = Text("Compatibility", T.headline(), "text")
        self.notes_card = Card(padding=20)
        self.notes_card.lay.addWidget(self.notes_title)
        self.notes_card.lay.addLayout(hbox((self.details, 2), T.XL, (self.notes_box, 3)))
        self.steps = QWidget()
        self.grid = QGridLayout(self.steps)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(T.LG)
        self.grid.setVerticalSpacing(T.LG)
        self._wide = None
        self.setup = widget_of(vbox(self.steps, T.LG, self.notes_card, None))
        self.progress = ProgressView(win)
        self.result = ResultView(win)
        self.stack = QStackedWidget()
        for w in (self.setup, self.progress, self.result):
            self.stack.addWidget(w)
        self._show(self.setup)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = widget_of(vbox(self.stack, margins=(T.XL, T.SM, T.XL, T.XL)))
        scroll.setWidget(holder)
        self.setLayout(vbox(scroll))
        self._arrange(True)

    def _arrange(self, wide):
        """3 columns on wide windows, the game on top of Disc + Burn on medium ones, one column on narrow."""
        if wide == self._wide:
            return
        self._wide = wide
        for w in (self.game, self.disc, self.burn):
            self.grid.removeWidget(w)
        for col in range(3):
            self.grid.setColumnStretch(col, 0)
        if wide == "narrow":
            self.grid.addWidget(self.game, 0, 0)
            self.grid.addWidget(self.disc, 1, 0)
            self.grid.addWidget(self.burn, 2, 0)
            self.grid.setColumnStretch(0, 1)
        elif wide is True:
            self.grid.addWidget(self.game, 0, 0)
            self.grid.addWidget(self.disc, 0, 1)
            self.grid.addWidget(self.burn, 0, 2)
            self.grid.setColumnStretch(0, 5)
            self.grid.setColumnStretch(1, 3)
            self.grid.setColumnStretch(2, 3)
        else:
            self.grid.addWidget(self.game, 0, 0, 1, 2)
            self.grid.addWidget(self.disc, 1, 0)
            self.grid.addWidget(self.burn, 1, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)

    def resizeEvent(self, e):  # noqa: N802
        w = self.width()
        # medium (game on top, Disc + Burn side by side) down to the smallest window, so the Burn button
        # is always in view; one column only if the page gets narrower than that
        self._arrange(True if w >= 1180 else "narrow" if w < 700 else False)
        super().resizeEvent(e)

    def set_notes(self, details: list[tuple[str, str]], notes: list[tuple[str, str]]):
        if (details, notes) == getattr(self, "_notes", None):
            return
        self._notes = (list(details), list(notes))
        self.details.set(details)
        clear_layout(self.notes_box)
        for text, kind in notes:
            self.notes_box.addWidget(Callout(text, kind))
        self.notes_box.addStretch(1)
        self.notes_card.setVisible(bool(details or notes))

    def _show(self, page: QWidget):
        """Only the visible page counts towards the size (a stack is otherwise as big as its biggest page)."""
        from PySide6.QtWidgets import QSizePolicy
        for w in (self.setup, self.progress, self.result):
            pol = QSizePolicy.Policy.Preferred if w is page else QSizePolicy.Policy.Ignored
            w.setSizePolicy(pol, pol)
        self.stack.setCurrentWidget(page)
        self.stack.updateGeometry()

    def show_setup(self):
        self._show(self.setup)

    def show_progress(self):
        self._show(self.progress)

    def show_result(self):
        self._show(self.result)
