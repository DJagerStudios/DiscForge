"""DiscForge's widget kit.  Everything sizes itself from its content (sizeHint/heightForWidth),
long single-line text is elided (full text in the tooltip) and multi-line text wraps, so nothing
overlaps or gets clipped at any window size.  Colours come from theme.P at paint time; widgets
that cache colours implement retheme()."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFontMetricsF, QPainter
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout,
                               QLineEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import art
from . import icons as I
from . import theme as T


# --------------------------------------------------------------------------
# Layout helpers

def vbox(*items, spacing: int = 0, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        _add(lay, it)
    return lay


def hbox(*items, spacing: int = 0, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        _add(lay, it)
    return lay


def _add(lay, it):
    if it is None:
        lay.addStretch(1)
    elif isinstance(it, int):
        lay.addSpacing(it)
    elif isinstance(it, tuple):
        w, stretch, *align = it
        if isinstance(w, QWidget):
            lay.addWidget(w, stretch, *align)
        else:
            lay.addLayout(w, stretch)
    elif isinstance(it, QWidget):
        lay.addWidget(it)
    else:
        lay.addLayout(it)


def widget_of(layout, parent=None) -> QWidget:
    w = QWidget(parent)
    w.setLayout(layout)
    return w


def clear_layout(lay, keep_widgets: bool = False) -> None:
    """Empty a layout (and the layouts inside it).  Its widgets are deleted, or with keep_widgets just
    taken out, ready to go into another layout."""
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            if not keep_widgets:
                it.widget().hide()              # gone at once, deleted when the event loop runs
                it.widget().deleteLater()
        elif it.layout():
            clear_layout(it.layout(), keep_widgets)


# --------------------------------------------------------------------------
# Text

class Text(QLabel):
    """A label with a palette colour role (text, text2, text3, accent, ok, warn, bad)."""

    def __init__(self, text: str = "", font=None, role: str = "text", wrap: bool = False, selectable=False,
                 align=None):
        super().__init__(text)
        self.role = role
        self.setFont(font or T.body())
        self.setWordWrap(wrap)
        if align is not None:
            self.setAlignment(align)
        if selectable:
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if wrap:
            sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            sp.setHeightForWidth(True)
            self.setSizePolicy(sp)
        self.retheme()

    def set_role(self, role: str):
        self.role = role
        self.retheme()

    def retheme(self):
        self.setStyleSheet(f"color: {getattr(T.P, self.role)}; background: transparent;")

    def setText(self, text: str):  # noqa: N802
        super().setText(text)
        if self.wordWrap():
            self._fit()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.wordWrap():
            self._fit()

    def needed_height(self, width: int) -> int:
        """The height the text needs at `width`.  (QLabel.heightForWidth() never reports less than the
        current minimum height, so it can't tell when a label could get shorter again.)"""
        old = self.minimumHeight()
        if old:
            self.setMinimumHeight(0)
        h = self.heightForWidth(width)
        if old:
            self.setMinimumHeight(old)
        return h

    def _fit(self):
        """Wrapped text: reserve the height the text needs at the current width (no more, no less), so no
        layout can clip it and it never keeps a stale extra line that pushes it off-centre."""
        if self.width() <= 0:
            return
        h = self.needed_height(self.width())
        if h > 0 and h != self.minimumHeight():
            self.setMinimumHeight(h)


class Elided(QWidget):
    """One line of text, elided to fit; the full text is in the tooltip."""

    def __init__(self, text: str = "", font=None, role: str = "text", align=Qt.AlignmentFlag.AlignLeft):
        super().__init__()
        self._text, self.role, self.align = "", role, align
        self.setFont(font or T.body())
        # Preferred + a small minimumSizeHint: layouts give it room for the whole text when they can and
        # shrink it (eliding) when they can't.  Never "Ignored": that collapses hidden widgets to 0 width.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(math.ceil(QFontMetricsF(self.font()).height()) + 2)
        self.setText(text)

    def setText(self, text: str):  # noqa: N802
        changed = (text or "") != self._text
        self._text = text or ""
        fm = QFontMetricsF(self.font())
        self.setToolTip(self._text if fm.horizontalAdvance(self._text) > self.width() else "")
        if changed:
            self.updateGeometry()                   # new text, new size hint: let the layout know
        self.update()

    def text(self) -> str:
        return self._text

    def set_role(self, role: str):
        self.role = role
        self.update()

    def sizeHint(self):  # noqa: N802
        fm = QFontMetricsF(self.font())
        return QSize(math.ceil(fm.horizontalAdvance(self._text)) + 2, self.height())

    def minimumSizeHint(self):  # noqa: N802
        return QSize(min(24, self.sizeHint().width()), self.height())

    def resizeEvent(self, e):  # noqa: N802
        self.setText(self._text)
        super().resizeEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        fm = QFontMetricsF(self.font())
        txt = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, self.width())
        p.setPen(QColor(getattr(T.P, self.role)))
        p.drawText(self.rect(), int(self.align | Qt.AlignmentFlag.AlignVCenter), txt)


# --------------------------------------------------------------------------
# Buttons

ICON_COLOURS = {"primary": "on_accent", "secondary": "text", "ghost": "text2", "danger": "bad"}


class Button(QPushButton):
    HEIGHTS = {"sm": 28, "md": 36, "lg": 44}

    GAP = "\u2002"                  # en space between the icon and the text

    def __init__(self, text: str = "", variant: str = "secondary", icon: str | None = None, size: str = "md"):
        super().__init__()
        self.variant, self.icon_name, self.size_name = variant, icon, size
        self._label = text
        self.setProperty("variant", variant)
        self.setProperty("btnsize", size)       # not "size": that's QWidget's own property
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.HEIGHTS[size])
        self.setFont(T.font(13 if size == "sm" else 14, T.W.Medium))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setText(text)
        self.retheme()

    def setText(self, text: str):  # noqa: N802
        self._label = text
        super().setText((self.GAP + text) if self.icon_name and text else text)
        self.updateGeometry()

    def label(self) -> str:
        return self._label

    def set_variant(self, variant: str):
        self.variant = variant
        self.setProperty("variant", variant)
        self.style().unpolish(self)
        self.style().polish(self)
        self.retheme()

    def set_icon(self, name: str | None):
        self.icon_name = name
        self.setText(self._label)
        self.retheme()

    def retheme(self):
        if self.icon_name:
            col = getattr(T.P, ICON_COLOURS.get(self.variant, "text"))
            self.setIcon(I.icon(self.icon_name, col, T.P.text3))
            k = 14 if self.size_name == "sm" else 16
            self.setIconSize(QSize(k, k))
        else:
            self.setIcon(I.icon("x", "#00000000"))
            self.setIconSize(QSize(0, 0))

    def sizeHint(self):  # noqa: N802
        fm = QFontMetricsF(self.font())
        pad = {"sm": 20, "md": 28, "lg": 40}[self.size_name]
        w = fm.horizontalAdvance(self.text()) + pad + (self.iconSize().width() + 8 if self.icon_name else 0)
        return QSize(math.ceil(w) + 2, self.HEIGHTS[self.size_name])

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()


class IconButton(QToolButton):
    def __init__(self, icon: str, tip: str = "", size: int = 34, role: str = "text2"):
        super().__init__()
        self.icon_name, self.role = icon, role
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(18, 18) if size >= 32 else QSize(16, 16))
        self.retheme()

    def set_icon(self, name: str):
        self.icon_name = name
        self.retheme()

    def retheme(self):
        self.setIcon(I.icon(self.icon_name, getattr(T.P, self.role), T.P.text3))


class Hover:
    def init_hover(self, ms: int = 120):
        self._hv = 0.0
        self._hanim = QVariantAnimation(self)
        self._hanim.setDuration(ms)
        self._hanim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hanim.valueChanged.connect(self._set_hv)

    def _set_hv(self, v):
        self._hv = float(v)
        self.update()

    def enterEvent(self, e):  # noqa: N802
        self._hanim.stop()
        self._hanim.setStartValue(self._hv)
        self._hanim.setEndValue(1.0)
        self._hanim.start()
        super().enterEvent(e)

    def leaveEvent(self, e):  # noqa: N802
        self._hanim.stop()
        self._hanim.setStartValue(self._hv)
        self._hanim.setEndValue(0.0)
        self._hanim.start()
        super().leaveEvent(e)


def paint_focus(p: QPainter, w: QWidget, rect: QRectF, radius: float) -> None:
    """The keyboard focus ring for custom-drawn controls (they take focus from Tab only, so the ring
    never shows after a mouse click)."""
    if w.hasFocus():
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(art.pen(T.P.accent, 2))
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)
        p.restore()


class NavButton(Hover, QAbstractButton):
    """Sidebar navigation item."""

    def __init__(self, icon: str, text: str, badge: str = ""):
        super().__init__()
        self.icon_name, self.badge = icon, badge
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(38)
        self.setFont(T.font(14, T.W.Medium))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(text)
        self.init_hover()

    def set_badge(self, text: str):
        self.badge = text
        self.update()

    def sizeHint(self):  # noqa: N802
        return QSize(180, 38)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        on = self.isChecked()
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T.P.raised))
            p.drawRoundedRect(r, T.R_MD, T.R_MD)
            p.setBrush(QColor(T.P.accent))
            p.drawRoundedRect(QRectF(r.left(), r.top() + 9, 3, r.height() - 18), 1.5, 1.5)
        elif self._hv > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.c(T.P.raised, 0.7 * self._hv))
            p.drawRoundedRect(r, T.R_MD, T.R_MD)
        paint_focus(p, self, r, T.R_MD)
        col = T.P.text if on else T.mix(T.P.text2, T.P.text, self._hv)
        I.paint(p, self.icon_name, QRectF(r.left() + 14, r.center().y() - 9, 18, 18), col)
        p.setFont(self.font())
        p.setPen(col)
        tr = QRectF(r.left() + 44, r.top(), r.width() - 52, r.height())
        bw = 0.0
        if self.badge:
            bf = T.font(11, T.W.DemiBold)
            bfm = QFontMetricsF(bf)
            bw = bfm.horizontalAdvance(self.badge) + 12
            br = QRectF(r.right() - bw - 8, r.center().y() - 9, bw, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T.P.accent) if on else QColor(T.P.border_strong))
            p.drawRoundedRect(br, 9, 9)
            p.setFont(bf)
            p.setPen(QColor(T.P.on_accent if on else T.P.text))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, self.badge)
            p.setFont(self.font())
            p.setPen(col)
        fm = QFontMetricsF(self.font())
        p.drawText(tr.adjusted(0, 0, -(bw + 6), 0), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, tr.width() - bw - 6))


class Toggle(QAbstractButton):
    def __init__(self, checked: bool = False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)          # Space switches it
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set)
        self.toggled.connect(self._go)

    def _set(self, v):
        self._pos = float(v)
        self.update()

    def _go(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool):  # noqa: N802
        super().setChecked(on)
        self._pos = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        off_col = T.P.border_strong
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.mix(off_col, T.P.accent, self._pos) if self.isEnabled() else QColor(T.P.border))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        k = r.height() - 6
        x = r.left() + 3 + (r.width() - k - 6) * self._pos
        p.setBrush(QColor("#FFFFFF") if self.isEnabled() else QColor(T.P.text3))
        p.drawEllipse(QRectF(x, r.top() + 3, k, k))
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(art.pen(T.P.text, 1.5))
            p.drawRoundedRect(r.adjusted(0.75, 0.75, -0.75, -0.75), r.height() / 2 - 0.75, r.height() / 2 - 0.75)


class Segmented(QWidget):
    """A segmented control (choose one of a few options)."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], current: str):
        super().__init__()
        self.options, self.current = options, current
        self.setFont(T.font(13, T.W.Medium))
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)          # Left / Right choose
        self._hover = -1
        self.setMouseTracking(True)

    def keyPressEvent(self, e):  # noqa: N802
        keys = [k for k, _l in self.options]
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1}.get(e.key())
        if step is None or self.current not in keys:
            return super().keyPressEvent(e)
        i = max(0, min(len(keys) - 1, keys.index(self.current) + step))
        if keys[i] != self.current:
            self.current = keys[i]
            self.update()
            self.changed.emit(self.current)

    def sizeHint(self):  # noqa: N802
        fm = QFontMetricsF(self.font())
        w = sum(fm.horizontalAdvance(label) + 28 for _k, label in self.options) + 6
        return QSize(math.ceil(w), 32)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def _rects(self) -> list[QRectF]:
        fm = QFontMetricsF(self.font())
        widths = [fm.horizontalAdvance(label) + 28 for _k, label in self.options]
        scale = (self.width() - 6) / max(1, sum(widths))
        x, out = 3.0, []
        for w in widths:
            out.append(QRectF(x, 3, w * scale, self.height() - 6))
            x += w * scale
        return out

    def set_current(self, key: str):
        self.current = key
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        h = next((i for i, r in enumerate(self._rects()) if r.contains(e.position())), -1)
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        for (key, _l), r in zip(self.options, self._rects()):
            if r.contains(e.position()) and key != self.current:
                self.current = key
                self.update()
                self.changed.emit(key)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(art.pen(T.P.border, 1))
        p.setBrush(QColor(T.P.sunken))
        p.drawRoundedRect(r, T.R_MD, T.R_MD)
        paint_focus(p, self, r, T.R_MD)
        p.setFont(self.font())
        for i, ((key, label), rr) in enumerate(zip(self.options, self._rects())):
            if key == self.current:
                p.setPen(art.pen(T.P.border_strong, 1))
                p.setBrush(QColor(T.P.surface if T.P.name == "light" else T.P.raised))
                p.drawRoundedRect(rr, 6, 6)
                p.setPen(QColor(T.P.text))
            else:
                p.setPen(QColor(T.P.text if i == self._hover else T.P.text2))
            p.drawText(rr, Qt.AlignmentFlag.AlignCenter, label)


class SearchBox(QLineEdit):
    def __init__(self, placeholder: str = "Search"):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setFixedHeight(T.CONTROL_H)
        self.setFont(T.body())
        self._act = self.addAction(I.icon("search", T.P.text3), QLineEdit.ActionPosition.LeadingPosition)

    def retheme(self):
        self._act.setIcon(I.icon("search", T.P.text3))


# --------------------------------------------------------------------------
# Surfaces

class Card(QFrame):
    def __init__(self, radius: int = T.R_LG, padding: int = 20, fill: str = "surface"):
        super().__init__()
        self.radius, self.fill = radius, fill
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(padding, padding, padding, padding)
        self.lay.setSpacing(T.MD)
        self.dim = False

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(art.pen(T.P.border, 1))
        p.setBrush(QColor(getattr(T.P, self.fill)))
        p.drawRoundedRect(r, self.radius, self.radius)


class Badge(QWidget):
    """A small tag: optional dot + text."""

    def __init__(self, text: str = "", colour: str | None = None, dot: bool = True):
        super().__init__()
        self.text, self.colour, self.dot = text, colour, dot
        self.setFont(T.font(12, T.W.DemiBold))
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set(self, text: str, colour: str | None = None):
        self.text, self.colour = text, colour
        self.updateGeometry()
        self.update()

    def sizeHint(self):  # noqa: N802
        fm = QFontMetricsF(self.font())
        return QSize(math.ceil(fm.horizontalAdvance(self.text)) + (26 if self.dot else 16), 22)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, _e):  # noqa: N802
        if not self.text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(self.colour or T.P.text2)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(art.pen(T.c(col, 0.38), 1))
        p.setBrush(T.c(col, 0.12))
        p.drawRoundedRect(r, 6, 6)
        x = r.left() + 8
        if self.dot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(x + 3, r.center().y()), 3, 3)
            x += 11
        p.setFont(self.font())
        p.setPen(T.mix(col, T.P.text, 0.35))
        p.drawText(QRectF(x, r.top(), r.width(), r.height()), Qt.AlignmentFlag.AlignVCenter, self.text)


class Callout(QFrame):
    """A note with an icon: kind is info, ok, warn or bad."""

    ICONS = {"info": "info", "ok": "circle-check", "warn": "triangle-alert", "bad": "circle-x"}

    def __init__(self, text: str, kind: str = "info", title: str = "", action: str = "", on_action=None):
        super().__init__()
        self.kind = kind
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        self.title = Text(title, T.small_medium(), "text", wrap=True) if title else None
        self.body = Text(text, T.small(), "text2", wrap=True)
        col = vbox(spacing=2)
        if self.title:
            col.addWidget(self.title)
        col.addWidget(self.body)
        lay = hbox((self.icon, 0, Qt.AlignmentFlag.AlignTop), T.MD, (col, 1), spacing=0,
                   margins=(T.MD, T.MD - 1, T.MD, T.MD - 1))
        if action:
            b = Button(action, "secondary", size="sm")
            if on_action:
                b.clicked.connect(on_action)
            lay.addSpacing(T.MD)
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setLayout(lay)
        self.retheme()

    def retheme(self):
        col = getattr(T.P, self.kind if self.kind in ("ok", "warn", "bad", "info") else "info")
        self.icon.setPixmap(I.pixmap(self.ICONS.get(self.kind, "info"), 18, col, self.devicePixelRatioF() or 2))

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = getattr(T.P, self.kind if self.kind in ("ok", "warn", "bad", "info") else "info")
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(art.pen(T.c(col, 0.30), 1))
        p.setBrush(T.c(col, 0.08))
        p.drawRoundedRect(r, T.R_MD, T.R_MD)


class Separator(QWidget):
    def __init__(self, vertical: bool = False):
        super().__init__()
        self.vertical = vertical
        if vertical:
            self.setFixedWidth(1)
        else:
            self.setFixedHeight(1)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.P.border))


class KeyValues(QWidget):
    """Two-column details: a muted label and a value per row."""

    def __init__(self, label_width: int = 110):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(6)
        self.label_width = label_width

    def set(self, rows: list[tuple[str, str]]):
        if rows == getattr(self, "_rows", None):
            return                                  # unchanged: no rebuild, no flicker
        self._rows = list(rows)
        clear_layout(self.lay)
        for k, v in rows:
            key = Elided(k, T.small(), "text3")
            key.setFixedWidth(self.label_width)
            val = Elided(v, T.small(), "text")
            self.lay.addLayout(hbox(key, T.MD, (val, 1)))


# --------------------------------------------------------------------------
# Progress

class Spinner(QWidget):
    def __init__(self, size: int = 18):
        super().__init__()
        self.setFixedSize(size, size)
        self._a = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _tick(self):
        self._a = (self._a + 14) % 360
        self.update()

    def showEvent(self, e):  # noqa: N802
        self._timer.start(30)
        super().showEvent(e)

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        art.paint_progress_ring(p, QRectF(self.rect()), None, T.P.accent, 2.2, self._a)


class ProgressBar(QWidget):
    def __init__(self, height: int = 6):
        super().__init__()
        self.frac: float | None = None
        self.colour: str | None = None
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)

    def set(self, frac: float | None, colour: str | None = None):
        self.frac, self.colour = frac, colour
        if frac is None and self.isVisible():
            self._timer.start(30)
        elif frac is not None:
            self._timer.stop()
        self.update()

    def showEvent(self, e):  # noqa: N802
        if self.frac is None:
            self._timer.start(30)
        super().showEvent(e)

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T.P.sunken if T.P.name == "light" else T.P.raised))
        p.drawRoundedRect(r, rad, rad)
        col = QColor(self.colour or T.P.accent)
        p.setBrush(col)
        if self.frac is None:
            t = ((time.monotonic() - self._t0) * 0.7) % 1.0
            w = r.width() * 0.28
            x = r.left() - w + (r.width() + w) * t
            p.setClipRect(r)
            p.drawRoundedRect(QRectF(x, r.top(), w, r.height()), rad, rad)
        elif self.frac > 0:
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(r.height(), r.width() * min(1.0, self.frac)), r.height()),
                              rad, rad)


class Meter(QWidget):
    """Capacity: how much of the disc the game uses."""

    def __init__(self):
        super().__init__()
        self.used, self.total = 0.0, 1.0
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, used: float, total: float):
        self.used, self.total = used, max(1.0, total)
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T.P.sunken if T.P.name == "light" else T.P.raised))
        p.drawRoundedRect(r, 4, 4)
        frac = self.used / self.total
        col = QColor(T.P.bad if frac > 1.0 else T.P.warn if frac > 0.97 else T.P.accent)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(r.left(), r.top(), max(r.height(), r.width() * min(1.0, frac)), r.height()), 4, 4)


# --------------------------------------------------------------------------
# Covers and tiles

class Cover(QWidget):
    """A game's generated cover at a fixed size."""

    def __init__(self, w: int, h: int, radius: float = 10):
        super().__init__()
        self.setFixedSize(w, h)
        self.radius = radius
        self.title, self.accent, self.accent2, self.tag, self.sub = "", T.P.accent, T.P.accent, "", ""
        self.empty = True

    def set(self, title: str, accent: str, accent2: str, tag: str, sub: str = ""):
        self.title, self.accent, self.accent2, self.tag, self.sub = title, accent, accent2, tag, sub
        self.empty = False
        self.update()

    def clear(self):
        self.empty = True
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.empty:
            d = art.pen(T.P.border_strong, 1.2)
            d.setDashPattern([3, 3])
            p.setPen(d)
            p.setBrush(QColor(T.P.sunken))
            p.drawRoundedRect(r, self.radius, self.radius)
            k = min(r.width(), r.height()) * 0.28
            I.paint(p, "gamepad-2", QRectF(r.center().x() - k / 2, r.center().y() - k / 2, k, k), T.P.text3, 1.6)
            return
        art.paint_cover(p, r, self.title, self.accent, self.accent2, self.tag, self.sub, self.radius)


class Tile(Hover, QWidget):
    """A library entry: cover with title and details underneath."""

    clicked = Signal(str)
    menu = Signal(str, QPoint)

    W, COVER_H, TEXT_H = 156, 208, 52            # natural size; a TileGrid widens tiles to fill its rows

    def __init__(self, path: str, title: str, accent: str, accent2: str, tag: str, meta: str, sub: str = ""):
        super().__init__()
        self.path, self.title, self.accent, self.accent2, self.tag, self.meta, self.sub = (
            path, title, accent, accent2, tag, meta, sub)
        self.selected = False
        # Preferred, not Fixed: a Fixed widget can't grow past its size hint, and the grid widens tiles
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumSize(120, round(120 * self.COVER_H / self.W) + self.TEXT_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{title}\n{path}")
        self.init_hover()

    def sizeHint(self):  # noqa: N802
        return QSize(self.W, self.COVER_H + self.TEXT_H)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.path)

    def contextMenuEvent(self, e):  # noqa: N802
        self.menu.emit(self.path, e.globalPos())

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        lift = 3 * self._hv
        w = self.width()
        cover_h = self.height() - self.TEXT_H
        cr = QRectF(1, 1 + 3 - lift, w - 2, cover_h - 4)
        art.paint_cover(p, cr, self.title, self.accent, self.accent2, self.tag, self.sub, 10,
                        1.0 if self.selected else self._hv * 0.6)
        f1, f2 = T.font(13, T.W.DemiBold), T.font(12)
        fm1, fm2 = QFontMetricsF(f1), QFontMetricsF(f2)
        y = cover_h + 6
        p.setFont(f1)
        p.setPen(QColor(T.P.text))
        p.drawText(QRectF(2, y, w - 4, fm1.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm1.elidedText(self.title, Qt.TextElideMode.ElideRight, w - 4))
        p.setFont(f2)
        p.setPen(QColor(T.P.text3))
        p.drawText(QRectF(2, y + fm1.height() + 2, w - 4, fm2.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm2.elidedText(self.meta, Qt.TextElideMode.ElideRight, w - 4))


class FlowLayout(QLayout):
    """Lays items out in rows that wrap (Qt's flow layout example, with uniform spacing)."""

    def __init__(self, parent=None, spacing: int = 16):
        super().__init__(parent)
        self.items: list = []
        self._sp = spacing

    def addItem(self, item):  # noqa: N802
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, i):  # noqa: N802
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i):  # noqa: N802
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._do(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        s = QSize()
        for it in self.items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self.items:
            hint = it.sizeHint()
            if x + hint.width() > r.right() + 1 and line_h > 0:
                x = r.x()
                y += line_h + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._sp
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class TileGrid(FlowLayout):
    """Equal columns that fill the width: as many tiles per row as fit at `min_w`, widened (up to
    `max_w`, keeping the cover's shape) so a row ends flush with the content edge instead of leaving
    a ragged gap."""

    def __init__(self, parent=None, spacing: int = 16, min_w: int = 150, max_w: int = 200):
        super().__init__(parent, spacing)
        self.min_w, self.max_w = min_w, max_w

    def _cell(self, width: int) -> tuple[int, int, int]:
        sp = self._sp
        cols = max(1, (width + sp) // (self.min_w + sp))
        w = max(1, min(self.max_w, (width - (cols - 1) * sp) // cols))
        return cols, w, round(w * Tile.COVER_H / Tile.W) + Tile.TEXT_H

    def minimumSize(self):  # noqa: N802
        m = self.contentsMargins()
        return QSize(self.min_w + m.left() + m.right(),
                     round(self.min_w * Tile.COVER_H / Tile.W) + Tile.TEXT_H + m.top() + m.bottom())

    def _do(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        cols, w, h = self._cell(r.width())
        if not test:
            for i, it in enumerate(self.items):
                row, col = divmod(i, cols)
                it.setGeometry(QRect(r.x() + col * (w + self._sp), r.y() + row * (h + self._sp), w, h))
        rows = -(-len(self.items) // cols)
        return m.top() + rows * h + max(0, rows - 1) * self._sp + m.bottom()


class EmptyState(QWidget):
    def __init__(self, icon: str, title: str, text: str, buttons: list[QWidget] | None = None):
        super().__init__()
        self.icon_name = icon
        self.glyph = QLabel()
        self.glyph.setFixedSize(56, 56)
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = Text(title, T.headline(), "text", align=Qt.AlignmentFlag.AlignHCenter)
        self.text = Text(text, T.small(), "text2", wrap=True, align=Qt.AlignmentFlag.AlignHCenter)
        self.text.setMaximumWidth(420)
        row = hbox(None, spacing=T.SM)
        for b in buttons or []:
            row.addWidget(b)
        row.addStretch(1)
        self.setLayout(vbox(None, hbox(None, self.glyph, None), T.LG, self.title, T.SM,
                            hbox(None, (self.text, 1), None), T.XL if buttons else 0, row, None))
        self.retheme()

    def retheme(self):
        self.glyph.setPixmap(I.pixmap(self.icon_name, 26, T.P.text2, 2.0, 1.7))
        self.glyph.setStyleSheet(f"background: {T.P.raised}; border: 1px solid {T.P.border}; border-radius: 28px;")


class Toast(QWidget):
    """A short message at the bottom right that fades away."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.label = Text("", T.small_medium(), "text")
        self.glyph = QLabel()
        self.glyph.setFixedSize(16, 16)
        self.setLayout(hbox(self.glyph, T.SM, self.label, margins=(14, 10, 16, 10)))
        self.fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.fx)
        self.anim = QPropertyAnimation(self.fx, b"opacity", self)
        self.anim.setDuration(220)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fade)
        self.hide()

    def show_message(self, text: str, kind: str = "ok", ms: int = 3500):
        col = getattr(T.P, kind if kind in ("ok", "warn", "bad", "info") else "info")
        self.glyph.setPixmap(I.pixmap({"ok": "circle-check", "warn": "triangle-alert", "bad": "circle-x"}.get(
            kind, "info"), 16, col))
        self.label.setText(text)
        self.adjustSize()
        par = self.parentWidget()
        self.move(par.width() - self.width() - 24, par.height() - self.height() - 24)
        self.anim.stop()
        self.fx.setOpacity(1.0)
        self.show()
        self.raise_()
        self.timer.start(ms)

    def _fade(self):
        self.anim.stop()
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.finished.connect(self.hide)
        self.anim.start()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(art.pen(T.P.border_strong, 1))
        p.setBrush(QColor(T.P.surface))
        p.drawRoundedRect(r, 10, 10)
