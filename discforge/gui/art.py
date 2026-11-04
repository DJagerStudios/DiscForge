"""Painted artwork: discs, generated game covers, the DiscForge mark and wordmark, progress rings."""
from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient, QTextLayout, QTextOption)

from . import theme as T


def pen(col, width: float = 1.0, cap=Qt.PenCapStyle.RoundCap) -> QPen:
    p = QPen(QColor(col) if not isinstance(col, QColor) else col)
    p.setWidthF(width)
    p.setCapStyle(cap)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return p


def clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def ease_out(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp01(t)
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def phase(t: float, a: float, b: float) -> float:
    return clamp01((t - a) / max(1e-6, b - a))


def rounded(rect: QRectF, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, r, r)
    return path


def glow(p: QPainter, center: QPointF, radius: float, col, alpha: float) -> None:
    if radius <= 0 or alpha <= 0:
        return
    g = QRadialGradient(center, radius)
    g.setColorAt(0.0, T.c(col, alpha))
    g.setColorAt(0.5, T.c(col, alpha * 0.30))
    g.setColorAt(1.0, T.c(col, 0.0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(center, radius, radius)


# --------------------------------------------------------------------------
# Discs

DYES = {"cdr": ("#1B4B57", "#39A39A", "#D9C27A"), "dvd": ("#2A1D55", "#6C55D9", "#4BA9F0"),
        "dl": ("#331543", "#A35BD1", "#F08AC3"), "silver": ("#6A7282", "#C2C9D5", "#F2F5F9"),
        "used": ("#3E434E", "#757D8C", "#A9B0BD")}


def paint_disc(p: QPainter, center: QPointF, r: float, dye: str = "silver", angle: float = 0.0,
               written: float | None = None, accent: str | None = None, alpha: float = 1.0) -> None:
    """A recordable disc, data side up.  `written` darkens the burned area from the hub out."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setOpacity(p.opacity() * alpha)
    base, mid, edge = DYES.get(dye, DYES["silver"])
    whole = QPainterPath()
    whole.addEllipse(center, r, r)
    whole.addEllipse(center, r * 0.115, r * 0.115)
    whole.setFillRule(Qt.FillRule.OddEvenFill)
    p.save()
    p.setClipPath(whole, Qt.ClipOperation.IntersectClip)
    g = QRadialGradient(center, r)
    g.setColorAt(0.0, QColor(mid))
    g.setColorAt(0.36, QColor(mid))
    g.setColorAt(0.74, QColor(base))
    g.setColorAt(0.95, T.mix(base, edge, 0.5))
    g.setColorAt(1.0, QColor(edge))
    p.fillPath(whole, g)
    con = QConicalGradient(center, -angle)
    for i, col in enumerate(["#FF5D73", "#FFD36B", "#2ED3A6", "#4FC3F7", "#A98BFF", "#FF5D73"]):
        con.setColorAt(i / 5, T.c(col, 0.20 if dye != "used" else 0.08))
    p.fillPath(whole, con)
    con2 = QConicalGradient(center, -angle + 90)
    for i in range(9):
        con2.setColorAt(i / 8, T.c("#FFFFFF", 0.16 if i % 4 == 0 else 0.0))
    p.fillPath(whole, con2)
    if written:
        rr = r * (0.36 + 0.60 * clamp01(written))
        ring = QPainterPath()
        ring.addEllipse(center, rr, rr)
        ring.addEllipse(center, r * 0.36, r * 0.36)
        ring.setFillRule(Qt.FillRule.OddEvenFill)
        p.fillPath(ring, T.c(T.mix("#000000", accent or T.P.accent, 0.25), 0.42))
    p.restore()
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(pen(T.c("#FFFFFF", 0.30), max(1.0, r * 0.012)))
    p.drawEllipse(center, r - r * 0.006, r - r * 0.006)
    hub = QPainterPath()
    hub.addEllipse(center, r * 0.335, r * 0.335)
    hub.addEllipse(center, r * 0.115, r * 0.115)
    hub.setFillRule(Qt.FillRule.OddEvenFill)
    hg = QRadialGradient(center, r * 0.335)
    hg.setColorAt(0.30, T.c("#0B0D12", 0.80))
    hg.setColorAt(0.82, T.c("#1C212C", 0.88))
    hg.setColorAt(0.95, T.c("#E6EAF2", 0.50))
    hg.setColorAt(1.0, T.c("#FFFFFF", 0.60))
    p.fillPath(hub, hg)
    p.setPen(pen(T.c("#FFFFFF", 0.18), max(1.0, r * 0.008)))
    p.drawEllipse(center, r * 0.21, r * 0.21)
    p.setPen(pen(T.c("#FFFFFF", 0.32), max(1.0, r * 0.01)))
    p.drawEllipse(center, r * 0.115, r * 0.115)
    p.restore()


def paint_disc_outline(p: QPainter, center: QPointF, r: float, col, dash_offset: float = 0.0) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    d = pen(T.c(col, 0.7), 1.5)
    d.setDashPattern([3.0, 4.0])
    d.setDashOffset(dash_offset)
    p.setPen(d)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(center, r, r)
    p.setPen(pen(T.c(col, 0.45), 1.3))
    p.drawEllipse(center, r * 0.34, r * 0.34)
    p.drawEllipse(center, r * 0.115, r * 0.115)
    p.restore()


# --------------------------------------------------------------------------
# Covers

def wrap(text: str, f: QFont, width: float, max_lines: int) -> list[str]:
    fm = QFontMetricsF(f)
    layout = QTextLayout(text, f)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(opt)
    layout.beginLayout()
    lines = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        lines.append(text[line.textStart():line.textStart() + line.textLength()].strip())
    layout.endLayout()
    lines = [ln for ln in lines if ln]
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)]
    return lines


def paint_cover(p: QPainter, rect: QRectF, title: str, accent: str, accent2: str, tag: str = "",
                sub: str = "", radius: float = 10.0, highlight: float = 0.0) -> None:
    """A clean, typographic stand-in cover: console colour, a big faint console mark, the title."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    path = rounded(rect, radius)
    p.setClipPath(path, Qt.ClipOperation.IntersectClip)
    w, h = rect.width(), rect.height()
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    g.setColorAt(0.0, T.mix(accent, "#FFFFFF", 0.06))
    g.setColorAt(0.55, T.mix(accent, "#0B0C10", 0.42))
    g.setColorAt(1.0, T.mix(accent2, "#07080B", 0.82))
    p.fillPath(path, g)
    rnd = random.Random(sum(map(ord, title)) & 0xFFFF)
    # fine diagonal texture
    p.setPen(pen(T.c("#FFFFFF", 0.035), 1.0))
    step = max(6.0, w / 18)
    off = rnd.random() * step
    x = rect.left() - h + off
    while x < rect.right():
        p.drawLine(QPointF(x, rect.bottom()), QPointF(x + h, rect.top()))
        x += step
    # disc, quarter visible at the top right
    dc = QPointF(rect.right() - w * 0.08, rect.top() + h * 0.16)
    p.setOpacity(0.28)
    paint_disc(p, dc, w * 0.50, "silver", angle=rnd.random() * 360)
    p.setOpacity(1.0)
    # console mark
    if tag:
        mf = T.font(max(12.0, w * 0.30), T.W.ExtraBold, -1.0)
        mfm = QFontMetricsF(mf)
        if mfm.horizontalAdvance(tag) > w * 0.86:
            mf = T.font(max(10.0, w * 0.30 * (w * 0.86) / mfm.horizontalAdvance(tag)), T.W.ExtraBold, -1.0)
            mfm = QFontMetricsF(mf)
        p.setFont(mf)
        p.setPen(T.c("#FFFFFF", 0.055))
        p.drawText(QPointF(rect.left() + w * 0.07, rect.top() + h * 0.50), tag)
    shade = QLinearGradient(QPointF(0, rect.top() + h * 0.40), QPointF(0, rect.bottom()))
    shade.setColorAt(0.0, T.c("#000000", 0.0))
    shade.setColorAt(1.0, T.c("#000000", 0.72))
    p.fillPath(path, shade)
    pad = max(8.0, round(w * 0.08))
    if tag:
        tf = T.font(max(8.0, w * 0.064), T.W.Bold, 0.6, caps=True)
        tfm = QFontMetricsF(tf)
        th = round(tfm.height() + pad * 0.45)
        tw = tfm.horizontalAdvance(tag) + pad * 1.1
        tr = QRectF(rect.left() + pad, rect.top() + pad, tw, th)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.c("#000000", 0.38))
        p.drawRoundedRect(tr, min(6.0, th / 2), min(6.0, th / 2))
        p.setFont(tf)
        p.setPen(T.c("#FFFFFF", 0.95))
        p.drawText(tr, Qt.AlignmentFlag.AlignCenter, tag)
    f = T.font(max(11.0, w * 0.098), T.W.Bold, -0.2)
    fm = QFontMetricsF(f)
    lines = wrap(title or "Untitled", f, w - 2 * pad, 3)
    lh = fm.lineSpacing()
    y = rect.bottom() - pad - lh * len(lines)
    if sub:
        sf = T.font(max(8.0, w * 0.060), T.W.Medium, 0.3)
        sfm = QFontMetricsF(sf)
        p.setFont(sf)
        p.setPen(T.c("#FFFFFF", 0.62))
        p.drawText(QRectF(rect.left() + pad, y - sfm.height() - 3, w - 2 * pad, sfm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   sfm.elidedText(sub, Qt.TextElideMode.ElideRight, w - 2 * pad))
    p.setFont(f)
    p.setPen(QColor("#FFFFFF"))
    for ln in lines:
        p.drawText(QRectF(rect.left() + pad, y, w - 2 * pad, lh),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ln)
        y += lh
    p.setClipping(False)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(pen(T.mix(T.c("#FFFFFF", 0.10), T.c(T.P.accent, 0.95), highlight), 1.0 + highlight))
    p.drawPath(rounded(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius))
    p.restore()


# --------------------------------------------------------------------------
# Brand

def paint_ring(p: QPainter, center: QPointF, r: float, width: float, sweep: float = 1.0, spin: float = 0.0,
               alpha: float = 1.0) -> None:
    if sweep <= 0:
        return
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    con = QConicalGradient(center, 90 - spin)
    con.setColorAt(0.00, T.c("#FFC46B", alpha))
    con.setColorAt(0.35, T.c("#F97316", alpha))
    con.setColorAt(0.70, T.c("#F0457A", alpha))
    con.setColorAt(1.00, T.c("#FFC46B", alpha))
    pe = QPen(con, width)
    pe.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pe)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r)
    if sweep >= 1.0:
        p.drawEllipse(rect)
    else:
        p.drawArc(rect, int((90 - spin) * 16), int(-360 * sweep * 16))
    p.restore()


def paint_mark(p: QPainter, rect: QRectF, spin: float = 0.0) -> None:
    c0 = rect.center()
    r = min(rect.width(), rect.height()) / 2
    paint_disc(p, c0, r * 0.68, "silver", angle=spin)
    paint_ring(p, c0, r * 0.86, max(1.4, r * 0.15), 1.0, spin)


def paint_wordmark(p: QPainter, left: float, baseline: float, px: float, alpha: float = 1.0) -> float:
    f = T.font(px, T.W.Bold, -px * 0.02)
    fm = QFontMetricsF(f)
    w1 = fm.horizontalAdvance("Disc")
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setOpacity(p.opacity() * alpha)
    p.setFont(f)
    p.setPen(QColor(T.P.text))
    p.drawText(QPointF(left, baseline), "Disc")
    p.setPen(QColor(T.P.accent))
    p.drawText(QPointF(left + w1, baseline), "Forge")
    p.restore()
    return fm.horizontalAdvance("DiscForge")


def wordmark_width(px: float) -> float:
    return QFontMetricsF(T.font(px, T.W.Bold, -px * 0.02)).horizontalAdvance("DiscForge")


def paint_progress_ring(p: QPainter, rect: QRectF, frac: float | None, color, width: float = 6.0,
                        spin: float = 0.0) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = rect.adjusted(width / 2, width / 2, -width / 2, -width / 2)
    p.setPen(pen(T.P.sunken if T.P.name == "light" else T.P.raised, width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(r)
    p.setPen(pen(color, width))
    if frac is None:
        p.drawArc(r, int(-spin * 16), 90 * 16)
    elif frac > 0:
        p.drawArc(r, 90 * 16, int(-360 * clamp01(frac) * 16))
    p.restore()


def check_path(center: QPointF, size: float, t: float = 1.0) -> QPainterPath:
    pts = [QPointF(center.x() - size * 0.42, center.y() + size * 0.02),
           QPointF(center.x() - size * 0.12, center.y() + size * 0.32),
           QPointF(center.x() + size * 0.44, center.y() - size * 0.30)]
    segs = [(a, b, math.hypot(b.x() - a.x(), b.y() - a.y())) for a, b in zip(pts, pts[1:])]
    left = sum(s[2] for s in segs) * clamp01(t)
    path = QPainterPath(pts[0])
    for a, b, ln in segs:
        if left <= 0:
            break
        k = min(1.0, left / ln)
        path.lineTo(QPointF(a.x() + (b.x() - a.x()) * k, a.y() + (b.y() - a.y()) * k))
        left -= ln
    return path
