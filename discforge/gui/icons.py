"""Lucide icons (ISC licence, data/icons) rendered crisply in any colour and size."""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


MISSING: set[str] = set()          # icon names asked for that aren't shipped (the self-test reports them)


@lru_cache(maxsize=None)
def _svg(name: str) -> bytes:
    try:
        return resources.files("discforge").joinpath(f"data/icons/{name}.svg").read_bytes()
    except (OSError, FileNotFoundError):
        MISSING.add(name)
        return resources.files("discforge").joinpath("data/icons/circle-help.svg").read_bytes()


@lru_cache(maxsize=512)
def _renderer(name: str, color: str, stroke: float) -> QSvgRenderer:
    data = _svg(name).replace(b"currentColor", color.encode())
    if stroke != 2.0:
        data = data.replace(b'stroke-width="2"', f'stroke-width="{stroke:g}"'.encode())
    return QSvgRenderer(QByteArray(data))


def paint(p: QPainter, name: str, rect: QRectF, color: QColor | str, stroke: float = 1.8) -> None:
    col = QColor(color)
    r = _renderer(name, col.name(QColor.NameFormat.HexRgb), stroke)
    p.save()
    if col.alphaF() < 1.0:
        p.setOpacity(p.opacity() * col.alphaF())
    r.render(p, rect)
    p.restore()


def pixmap(name: str, size: int, color: QColor | str, dpr: float = 2.0, stroke: float = 1.8) -> QPixmap:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint(p, name, QRectF(0, 0, size, size), color, stroke)
    p.end()
    return pm


class _Engine(QIconEngine):
    """QIcon that re-renders the SVG at the exact size Qt asks for (no blurry scaling)."""

    def __init__(self, name: str, color: str, disabled: str, stroke: float):
        super().__init__()
        self.name, self.color, self.disabled, self.stroke = name, color, disabled, stroke

    def paint(self, p, rect, mode, state):  # noqa: N802
        col = self.disabled if mode == QIcon.Mode.Disabled else self.color
        paint(p, self.name, QRectF(rect), col, self.stroke)

    def pixmap(self, size, mode, state):  # noqa: N802
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint(p, pm.rect(), mode, state)
        p.end()
        return pm

    def clone(self):
        return _Engine(self.name, self.color, self.disabled, self.stroke)


def icon(name: str, color: str, disabled: str | None = None, stroke: float = 1.8) -> QIcon:
    return QIcon(_Engine(name, color, disabled or color, stroke))


ICON_SIZE = QSize(18, 18)
