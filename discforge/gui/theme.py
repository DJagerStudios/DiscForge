"""DiscForge's design system: colour tokens (dark and light), the Inter type scale, spacing,
radii and the application stylesheet.  Widgets read the live palette `P` when they paint, so a
theme switch is a palette swap + restyle + repaint."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication

# spacing scale (px)
XS, SM, MD, LG, XL, XXL = 4, 8, 12, 16, 24, 32
R_SM, R_MD, R_LG = 6, 8, 12           # corner radii
SIDEBAR_W = 232
TOPBAR_H = 64
CONTROL_H = 36


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str           # window
    sidebar: str
    surface: str      # cards
    raised: str       # inputs, hover rows
    sunken: str       # wells, tracks
    border: str
    border_strong: str
    text: str
    text2: str        # secondary
    text3: str        # tertiary / placeholders
    accent: str
    accent_hover: str
    accent_press: str
    accent_soft: str  # tinted backgrounds
    on_accent: str
    ok: str
    warn: str
    bad: str
    info: str


DARK = Palette("dark", bg="#0E1014", sidebar="#121419", surface="#16191F", raised="#1D2128", sunken="#101216",
               border="#252A33", border_strong="#343A46", text="#EDEFF3", text2="#A3A9B5", text3="#6F7683",
               accent="#F97316", accent_hover="#FB8B3D", accent_press="#E2640D", accent_soft="#2A1C12",
               on_accent="#FFFFFF", ok="#22C55E", warn="#F5A524", bad="#F04B4B", info="#4C8DF6")
LIGHT = Palette("light", bg="#F4F5F7", sidebar="#ECEEF1", surface="#FFFFFF", raised="#F3F4F6", sunken="#E9EBEF",
                border="#E1E4E9", border_strong="#C9CED6", text="#15181D", text2="#555C67", text3="#8A909B",
                accent="#EA580C", accent_hover="#F26A21", accent_press="#C9480A", accent_soft="#FDEBDD",
                on_accent="#FFFFFF", ok="#16A34A", warn="#C77700", bad="#D92D2D", info="#2563EB")

P: Palette = DARK


def set_palette(name: str) -> Palette:
    global P
    P = LIGHT if name == "light" else DARK
    return P


def c(hexcol: str | QColor, alpha: float = 1.0) -> QColor:
    col = QColor(hexcol)
    col.setAlphaF(max(0.0, min(1.0, alpha * col.alphaF())))
    return col


def mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    a, b = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


def rgba(col: QColor | str) -> str:
    q = QColor(col)
    return f"rgba({q.red()},{q.green()},{q.blue()},{q.alphaF():.3f})"


# --------------------------------------------------------------------------
# Type

FAMILY = "Inter"
_loaded = False


def load_fonts() -> str:
    """Register the bundled Inter weights; returns the family to use."""
    global _loaded, FAMILY
    if _loaded:
        return FAMILY
    _loaded = True
    fams = set()
    base = resources.files("discforge").joinpath("data/fonts")
    for w in ("Regular", "Medium", "SemiBold", "Bold", "ExtraBold"):
        try:
            data = base.joinpath(f"Inter-{w}.ttf").read_bytes()
        except OSError:
            continue
        fid = QFontDatabase.addApplicationFontFromData(data)
        if fid >= 0:
            fams.update(QFontDatabase.applicationFontFamilies(fid))
    if "Inter" in fams:
        FAMILY = "Inter"
    else:
        FAMILY = QGuiApplication.font().family() or "sans-serif"
    return FAMILY


W = QFont.Weight


def font(px: float, weight: QFont.Weight = W.Normal, spacing: float = 0.0, caps: bool = False) -> QFont:
    f = QFont(FAMILY)
    f.setPixelSize(max(6, round(px)))
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
    f.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def display():
    return font(26, W.DemiBold, -0.4)


def title():
    return font(19, W.DemiBold, -0.2)


def headline():
    return font(15, W.DemiBold, -0.1)


def body():
    return font(14)


def body_medium():
    return font(14, W.Medium)


def small():
    return font(13)


def small_medium():
    return font(13, W.Medium)


def caption():
    return font(12)


def overline():
    return font(11, W.DemiBold, 0.8, caps=True)


def mono():
    f = QFont("monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPixelSize(12)
    return f


# --------------------------------------------------------------------------

def stylesheet() -> str:
    p = P
    return f"""
    QWidget {{ color: {p.text}; }}
    QMainWindow, QDialog {{ background: {p.bg}; }}
    QToolTip {{ background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong};
               padding: 6px 8px; border-radius: 6px; }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ width: 10px; background: transparent; margin: 2px; }}
    QScrollBar:horizontal {{ height: 10px; background: transparent; margin: 2px; }}
    QScrollBar::handle {{ background: {p.border_strong}; border-radius: 3px; min-height: 32px; min-width: 32px; }}
    QScrollBar::handle:hover {{ background: {p.text3}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; border: none; width: 0; height: 0; }}

    QPushButton {{ background: {p.raised}; color: {p.text}; border: 1px solid {p.border};
                   border-radius: {R_MD}px; padding: 0 14px; min-height: {CONTROL_H - 2}px; }}
    QPushButton:hover {{ background: {rgba(mix(p.raised, p.text, 0.06))}; border-color: {p.border_strong}; }}
    QPushButton:pressed {{ background: {rgba(mix(p.raised, p.text, 0.10))}; }}
    QPushButton:disabled {{ color: {p.text3}; background: {p.raised}; border-color: {p.border}; }}
    QPushButton:focus {{ outline: none; border-color: {p.accent}; }}
    QPushButton[variant="primary"] {{ background: {p.accent}; color: {p.on_accent}; border: 1px solid {p.accent}; }}
    QPushButton[variant="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton[variant="primary"]:pressed {{ background: {p.accent_press}; border-color: {p.accent_press}; }}
    QPushButton[variant="primary"]:disabled {{ background: {p.raised}; color: {p.text3}; border-color: {p.border}; }}
    QPushButton[variant="primary"]:focus {{ border: 2px solid {p.text}; }}
    QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; }}
    QPushButton[variant="ghost"]:hover {{ background: {p.raised}; border-color: {p.border}; }}
    QPushButton[variant="danger"] {{ background: transparent; color: {p.bad}; border-color: {rgba(c(p.bad, 0.45))}; }}
    QPushButton[variant="danger"]:hover {{ background: {rgba(c(p.bad, 0.10))}; }}
    QPushButton[btnsize="lg"] {{ min-height: 42px; border-radius: 10px; padding: 0 20px; }}
    QPushButton[btnsize="sm"] {{ min-height: 26px; padding: 0 10px; border-radius: 7px; }}
    QPushButton[variant="ghost"]:focus, QPushButton[variant="danger"]:focus {{ border-color: {p.accent}; }}

    QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {R_MD}px; }}
    QToolButton:hover {{ background: {p.raised}; border-color: {p.border}; }}
    QToolButton:pressed {{ background: {rgba(mix(p.raised, p.text, 0.08))}; }}
    QToolButton:focus {{ border-color: {p.accent}; }}

    QLineEdit {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: {R_MD}px;
                 padding: 0 10px; min-height: {CONTROL_H - 2}px; color: {p.text};
                 selection-background-color: {p.accent}; selection-color: {p.on_accent}; }}
    QLineEdit:focus {{ border-color: {p.accent}; }}
    QLineEdit:disabled {{ color: {p.text3}; }}

    QComboBox {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: {R_MD}px;
                 padding: 0 30px 0 10px; min-height: {CONTROL_H - 2}px; color: {p.text}; }}
    QComboBox:hover {{ border-color: {p.border_strong}; }}
    QComboBox:focus {{ border-color: {p.accent}; }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox QAbstractItemView {{ background: {p.surface}; border: 1px solid {p.border_strong};
                                   border-radius: 8px; padding: 4px; outline: none;
                                   selection-background-color: {p.raised}; selection-color: {p.text}; }}

    QMenu {{ background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: 10px; padding: 5px; }}
    QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; color: {p.text}; }}
    QMenu::item:selected {{ background: {p.raised}; }}
    QMenu::item:disabled {{ color: {p.text3}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 6px; }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}

    QPlainTextEdit {{ background: {p.sunken}; border: 1px solid {p.border}; border-radius: {R_MD}px;
                      padding: 8px; color: {p.text2}; selection-background-color: {p.accent}; }}

    QTableWidget, QTableView {{ background: transparent; border: none; gridline-color: transparent;
                                selection-background-color: {p.raised}; selection-color: {p.text};
                                alternate-background-color: transparent; outline: none; }}
    QHeaderView {{ background: transparent; border: none; }}
    QHeaderView::section {{ background: transparent; color: {p.text3}; border: none;
                            border-bottom: 1px solid {p.border}; padding: 8px 10px 8px 13px; font-weight: 600; }}
    QTableView::item {{ border-bottom: 1px solid {p.border}; padding: 0 10px; }}

    QMessageBox {{ background: {p.surface}; }}
    QMessageBox QLabel {{ color: {p.text}; }}
    """
