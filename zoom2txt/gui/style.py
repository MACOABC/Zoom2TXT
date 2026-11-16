"""Sistema de diseño de Zoom2TXT 3 — «Grafito y esmeralda».

Una sola fuente de verdad para color, tipografía, radios y espaciado, en modo
claro y oscuro. ``apply_theme(app, mode)`` publica la paleta activa como
variables de módulo (``style.BG``, ``style.TEXT``…) para los widgets pintados a
mano, aplica el QSS y avisa a quien se haya suscrito con ``on_theme_change``.

Reglas de contraste (verificadas en las pruebas): texto sobre acento, enlaces
sobre superficie y éxito sobre consola cumplen WCAG AA (≥ 4.5:1).
"""

from __future__ import annotations

import weakref
from collections.abc import Callable

PALETTES = {
    "dark": dict(
        BG="#0a0e0d",
        SIDEBAR="#0d1211",
        SURFACE="#111716",
        SURFACE_2="#171e1c",
        SURFACE_3="#1f2825",
        BORDER="#222c29",
        BORDER_HI="#3a4843",
        TEXT="#ecf2ef",
        MUTED="#a9b8b1",
        FAINT="#84958d",
        ACCENT="#34d399",
        ACCENT_HI="#5fe0b0",
        ACCENT_DN="#22b783",
        ACCENT_SUBTLE="#12251e",
        ACCENT_LINE="#1f4a3a",
        ON_ACCENT="#03221a",
        LINK="#6ee7b7",
        SUCCESS="#6ee7b7",
        SUCCESS_BG="#11261e",
        ERROR="#ffa3a3",
        ERROR_BG="#2b1616",
        WARNING="#f7cf73",
        WARNING_BG="#2a2212",
        INFO="#9cc7ff",
        INFO_BG="#131f30",
        VIOLET="#c4b5fd",
        VIOLET_BG="#1d1a2e",
        CONSOLE_BG="#080b0a",
        CONSOLE_FG="#c3d2cb",
        OVERLAY="#000000",
        USER_BUBBLE="#1a2421",
    ),
    "light": dict(
        BG="#f3f6f4",
        SIDEBAR="#eaefec",
        SURFACE="#ffffff",
        SURFACE_2="#f1f5f3",
        SURFACE_3="#e4ebe7",
        BORDER="#dbe4df",
        BORDER_HI="#aebdb5",
        TEXT="#101915",
        MUTED="#44554c",
        FAINT="#5f7067",
        ACCENT="#0b7a57",
        ACCENT_HI="#096a4b",
        ACCENT_DN="#075a40",
        ACCENT_SUBTLE="#e2f3eb",
        ACCENT_LINE="#a9d9c4",
        ON_ACCENT="#ffffff",
        LINK="#0b7a57",
        SUCCESS="#0b7a57",
        SUCCESS_BG="#e2f3eb",
        ERROR="#b3261e",
        ERROR_BG="#fbe9e7",
        WARNING="#8a5200",
        WARNING_BG="#fbf0d9",
        INFO="#1d5fae",
        INFO_BG="#e6effa",
        VIOLET="#5b3fc4",
        VIOLET_BG="#ece8fb",
        CONSOLE_BG="#f7f9f8",
        CONSOLE_FG="#22362c",
        OVERLAY="#0b1410",
        USER_BUBBLE="#e9f1ed",
    ),
}

FONT_UI = '"Inter", "Segoe UI", "Noto Sans", sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'

RADIUS = 12
RADIUS_SM = 8
SPACE = 16

# Paleta activa (defaults; apply_theme la reemplaza)
current_mode = "dark"
CHECK_ICON = ""
DOWN_ICON = ""
UP_ICON = ""
ROW_HEIGHT = 38
HEADER_HEIGHT = 36
globals().update(PALETTES["dark"])

_listeners: list = []


def on_theme_change(callback: Callable[[], None]) -> None:
    """Registra un callback (método enlazado o función) que se invoca tras cada
    cambio de tema. Los métodos se guardan con referencia débil."""
    try:
        ref = weakref.WeakMethod(callback)  # type: ignore[arg-type]
    except TypeError:
        ref = lambda: callback  # noqa: E731 — función libre: referencia fuerte
    _listeners.append(ref)


def _notify_theme_change() -> None:
    alive = []
    for ref in _listeners:
        callback = ref()
        if callback is None:
            continue
        alive.append(ref)
        try:
            callback()
        except Exception:  # noqa: BLE001 — repintar nunca debe impedir el cambio de tema
            # Widget C++ ya destruido o vista sin datos: se descarta el suscriptor.
            alive.pop()
    _listeners[:] = alive


def rgba(hex_color: str, alpha: float) -> str:
    """'#rrggbb' + alfa 0-1 → 'rgba(r,g,b,a)' para QSS y HTML."""
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha:.3f})"


def rgba_color(hex_color: str, alpha: float):
    """QColor con alfa para los widgets pintados a mano."""
    from PySide6.QtGui import QColor

    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def mix(a: str, b: str, t: float) -> str:
    """Interpola dos colores hex (t=0 → a, t=1 → b)."""
    pa, pb = a.lstrip("#"), b.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        va, vb = int(pa[i : i + 2], 16), int(pb[i : i + 2], 16)
        parts.append(round(va + (vb - va) * t))
    return "#{:02x}{:02x}{:02x}".format(*parts)


def _build_qss() -> str:
    g = globals()
    return f"""
* {{
    font-family: {FONT_UI};
    font-size: 14px;
    color: {g["TEXT"]};
    outline: 0;
}}

QMainWindow, QDialog, QWizard, QWidget#root {{
    background-color: {g["BG"]};
}}
QWidget#sidebar {{
    background-color: {g["SIDEBAR"]};
    border-right: 1px solid {g["BORDER"]};
}}
QWidget#topbar {{
    background-color: {g["BG"]};
    border-bottom: 1px solid {g["BORDER"]};
}}
QWidget#pageHost, QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {{
    background-color: {g["BG"]};
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}
QScrollArea > QWidget > QWidget#plain, QWidget#plain {{ background: transparent; }}

/* ── Tipografía ────────────────────────────────────────────────── */
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 23px; font-weight: 700; letter-spacing: -0.4px; }}
QLabel#h2 {{ font-size: 15.5px; font-weight: 650; letter-spacing: -0.1px; }}
QLabel#h3 {{ font-size: 13.5px; font-weight: 650; }}
QLabel#muted, QLabel#hint {{ color: {g["MUTED"]}; font-size: 13px; }}
QLabel#faint {{ color: {g["FAINT"]}; font-size: 12px; }}
QLabel#accent {{ color: {g["LINK"]}; font-weight: 600; }}
QLabel#eyebrow {{
    color: {g["FAINT"]}; font-size: 11px; font-weight: 700; letter-spacing: 1.1px;
}}
QLabel#brand {{ font-size: 18px; font-weight: 750; letter-spacing: -0.4px; }}
QLabel#kpiValue {{ font-size: 26px; font-weight: 720; letter-spacing: -0.6px; }}
QLabel#kpiLabel {{ color: {g["MUTED"]}; font-size: 12.5px; font-weight: 500; }}
QLabel#kbd {{
    color: {g["FAINT"]}; background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER"]}; border-radius: 5px;
    padding: 1px 6px; font-size: 11px; font-weight: 600;
}}

/* ── Superficies ───────────────────────────────────────────────── */
QFrame#card {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER"]};
    border-radius: {RADIUS}px;
}}
QFrame#cardRaised {{
    background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER"]};
    border-radius: {RADIUS}px;
}}
QFrame#cardAccent {{
    background-color: {g["ACCENT_SUBTLE"]};
    border: 1px solid {g["ACCENT_LINE"]};
    border-radius: {RADIUS}px;
}}
QFrame#workspace, QFrame#metric, QFrame#plain {{ background: transparent; border: none; }}
QFrame#healthSummary {{
    background-color: {g["ACCENT_SUBTLE"]};
    border: 1px solid {g["ACCENT_LINE"]};
    border-radius: {RADIUS}px;
}}
QFrame#activityDock {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER"]};
    border-radius: {RADIUS}px;
}}
QFrame#divider {{ background-color: {g["BORDER"]}; border: none; max-height: 1px; min-height: 1px; }}
QLabel#dockBadge {{
    background-color: {g["ACCENT_SUBTLE"]};
    color: {g["LINK"]};
    font-size: 11.5px; font-weight: 700;
    border: 1px solid {g["ACCENT_LINE"]};
    border-radius: 9px; padding: 3px 9px;
}}
QLabel#healthHeadline {{ font-size: 19px; font-weight: 700; letter-spacing: -0.2px; }}
QLabel[chip="true"] {{
    border-radius: 9px; padding: 2px 9px; font-size: 11.5px; font-weight: 650;
    background-color: {g["SURFACE_3"]}; color: {g["MUTED"]};
    border: 1px solid {g["BORDER"]};
}}
QLabel[chip="true"][tone="success"] {{ background-color: {g["SUCCESS_BG"]}; color: {g["SUCCESS"]}; border-color: {g["ACCENT_LINE"]}; }}
QLabel[chip="true"][tone="warning"] {{ background-color: {g["WARNING_BG"]}; color: {g["WARNING"]}; border-color: {rgba(g["WARNING"], 0.30)}; }}
QLabel[chip="true"][tone="error"] {{ background-color: {g["ERROR_BG"]}; color: {g["ERROR"]}; border-color: {rgba(g["ERROR"], 0.30)}; }}
QLabel[chip="true"][tone="info"] {{ background-color: {g["INFO_BG"]}; color: {g["INFO"]}; border-color: {rgba(g["INFO"], 0.30)}; }}
QLabel[chip="true"][tone="violet"] {{ background-color: {g["VIOLET_BG"]}; color: {g["VIOLET"]}; border-color: {rgba(g["VIOLET"], 0.30)}; }}
QFrame[banner="true"] {{
    border-radius: 10px; border: 1px solid {g["BORDER"]}; background-color: {g["SURFACE_2"]};
}}
QFrame[banner="true"][tone="success"] {{ background-color: {g["SUCCESS_BG"]}; border-color: {g["ACCENT_LINE"]}; }}
QFrame[banner="true"][tone="warning"] {{ background-color: {g["WARNING_BG"]}; border-color: {rgba(g["WARNING"], 0.35)}; }}
QFrame[banner="true"][tone="error"] {{ background-color: {g["ERROR_BG"]}; border-color: {rgba(g["ERROR"], 0.35)}; }}
QFrame[banner="true"][tone="info"] {{ background-color: {g["INFO_BG"]}; border-color: {rgba(g["INFO"], 0.35)}; }}

/* ── Navegación lateral (compatibilidad con botones QSS) ───────── */
QPushButton[variant="nav"] {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 8px 12px;
    text-align: left;
    font-weight: 550;
    color: {g["MUTED"]};
}}
QPushButton[variant="nav"]:hover {{ background-color: {g["SURFACE_3"]}; color: {g["TEXT"]}; }}
QPushButton[variant="nav"]:checked {{
    background-color: {g["ACCENT_SUBTLE"]}; color: {g["LINK"]}; font-weight: 650;
}}
QPushButton[variant="nav"]:disabled {{ color: {g["FAINT"]}; background: transparent; border-color: transparent; }}

/* ── Inputs ────────────────────────────────────────────────────── */
QLineEdit, QComboBox, QDateEdit, QSpinBox {{
    background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 9px;
    padding: 8px 12px;
    min-height: 20px;
    selection-background-color: {g["ACCENT"]};
    selection-color: {g["ON_ACCENT"]};
    placeholder-text-color: {g["FAINT"]};
}}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover, QSpinBox:hover {{ border-color: {g["FAINT"]}; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{
    border: 2px solid {g["LINK"]};
    padding: 7px 11px;
    background-color: {g["SURFACE"]};
}}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled {{
    color: {g["FAINT"]};
    background-color: {g["SURFACE"]};
    border-color: {g["BORDER"]};
}}
QLineEdit[variant="search"] {{
    border-radius: 10px; padding-left: 34px; background-color: {g["SURFACE"]};
}}
QLineEdit[variant="search"]:focus {{ padding-left: 33px; }}
QLineEdit[variant="ghost"] {{ border: none; background: transparent; padding: 6px 4px; font-size: 15px; }}
QLineEdit[variant="ghost"]:focus {{ border: none; padding: 6px 4px; background: transparent; }}
QComboBox, QDateEdit, QSpinBox {{ padding-right: 34px; }}
QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{ padding-right: 33px; }}
QComboBox::drop-down, QDateEdit::drop-down {{
    subcontrol-origin: padding; subcontrol-position: top right;
    border: none; width: 30px;
}}
QComboBox::down-arrow, QDateEdit::down-arrow, QSpinBox::down-arrow {{
    image: url("{g["DOWN_ICON"]}"); width: 14px; height: 14px;
}}
QSpinBox::up-arrow {{ image: url("{g["UP_ICON"]}"); width: 14px; height: 14px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 30px;
    border: none; border-left: 1px solid {g["BORDER"]};
    background: transparent;
}}
QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 2px; margin-right: 2px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 2px; margin-right: 2px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {g["SURFACE_3"]}; }}
QSpinBox::up-button:off, QSpinBox::down-button:off {{ background: {g["SURFACE_2"]}; }}
QComboBox QAbstractItemView {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {g["ACCENT_SUBTLE"]};
    selection-color: {g["TEXT"]};
}}
QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 2px 8px; border-radius: 6px; }}
QPlainTextEdit, QTextEdit {{
    background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 10px;
    padding: 8px 10px;
    selection-background-color: {g["ACCENT"]};
    selection-color: {g["ON_ACCENT"]};
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 9px; }}
QTextBrowser {{
    background-color: transparent; border: none; padding: 0;
    selection-background-color: {g["ACCENT"]};
    selection-color: {g["ON_ACCENT"]};
}}
QTextBrowser:focus {{ border: none; padding: 0; }}
QTextBrowser#reader {{
    background-color: {g["SURFACE"]}; border: 1px solid {g["BORDER"]};
    border-radius: {RADIUS}px; padding: 18px 22px;
}}
QPlainTextEdit#composer {{
    background-color: transparent; border: none; padding: 4px 2px; font-size: 14.5px;
}}
QPlainTextEdit#composer:focus {{ border: none; padding: 4px 2px; }}
QFrame#composerBox {{
    background-color: {g["SURFACE_2"]}; border: 1px solid {g["BORDER_HI"]}; border-radius: 14px;
}}
QFrame#composerBox[focused="true"] {{ border: 1px solid {g["LINK"]}; }}

/* ── Botones ───────────────────────────────────────────────────── */
QPushButton {{
    background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 9px;
    padding: 8px 15px;
    font-weight: 600;
}}
QPushButton:hover {{ background-color: {g["SURFACE_3"]}; border-color: {g["FAINT"]}; }}
QPushButton:pressed {{ background-color: {g["SURFACE"]}; }}
QPushButton:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 14px; }}
QPushButton:disabled {{
    color: {g["FAINT"]};
    background-color: {g["SURFACE"]};
    border-color: {g["BORDER"]};
}}
QPushButton::menu-indicator {{ image: url("{g["DOWN_ICON"]}"); width: 12px; height: 12px; subcontrol-position: right center; right: 10px; }}
QPushButton[variant="primary"] {{
    background-color: {g["ACCENT"]};
    border: 1px solid {g["ACCENT"]};
    color: {g["ON_ACCENT"]};
    font-weight: 700;
    padding: 9px 18px;
}}
QPushButton[variant="primary"]:hover {{ background-color: {g["ACCENT_HI"]}; border-color: {g["ACCENT_HI"]}; }}
QPushButton[variant="primary"]:pressed {{ background-color: {g["ACCENT_DN"]}; border-color: {g["ACCENT_DN"]}; }}
QPushButton[variant="primary"]:disabled {{
    background-color: {g["SURFACE_3"]}; border-color: {g["BORDER"]}; color: {g["FAINT"]};
}}
QPushButton[variant="secondary"] {{
    background-color: {g["SURFACE_2"]}; border: 1px solid {g["BORDER"]}; padding: 7px 13px;
}}
QPushButton[variant="secondary"]:hover {{ background-color: {g["SURFACE_3"]}; border-color: {g["BORDER_HI"]}; }}
QPushButton[variant="ghost"] {{
    background-color: transparent; border: 1px solid transparent; color: {g["MUTED"]};
    padding: 7px 10px; font-weight: 600;
}}
QPushButton[variant="ghost"]:hover {{ background-color: {g["SURFACE_3"]}; color: {g["TEXT"]}; }}
QPushButton[variant="ghost"]:checked {{ background-color: {g["ACCENT_SUBTLE"]}; color: {g["LINK"]}; }}
QPushButton[variant="ghost"]:disabled {{ background: transparent; border-color: transparent; color: {g["FAINT"]}; }}
QPushButton[variant="link"] {{
    background: transparent; border: none; color: {g["LINK"]}; padding: 2px 2px; font-weight: 600; text-align: left;
}}
QPushButton[variant="link"]:hover {{ color: {g["TEXT"]}; background: transparent; }}
QPushButton[variant="icon"] {{
    background-color: transparent; border: 1px solid transparent; border-radius: 8px;
    padding: 6px; min-width: 20px;
}}
QPushButton[variant="icon"]:hover {{ background-color: {g["SURFACE_3"]}; }}
QPushButton[variant="icon"]:checked {{ background-color: {g["ACCENT_SUBTLE"]}; }}
QPushButton[variant="icon"]:disabled {{ background: transparent; border-color: transparent; }}
QPushButton[variant="danger"] {{
    background-color: transparent; border: 1px solid {rgba(g["ERROR"], 0.55)}; color: {g["ERROR"]};
}}
QPushButton[variant="danger"]:hover {{ background-color: {g["ERROR_BG"]}; color: {g["ERROR"]}; border-color: {g["ERROR"]}; }}
QPushButton[variant="week"], QPushButton[variant="chip"] {{
    background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER"]};
    border-radius: 12px;
    padding: 4px 11px;
    min-height: 16px;
    font-weight: 600; font-size: 12.5px;
    color: {g["MUTED"]};
}}
QPushButton[variant="week"] {{ border-radius: 8px; padding: 3px 6px; font-size: 13px; min-height: 0px; }}
QPushButton[variant="week"]:hover, QPushButton[variant="chip"]:hover {{
    border-color: {g["ACCENT"]}; color: {g["TEXT"]}; background-color: {g["SURFACE_3"]};
}}
QPushButton[variant="week"]:checked, QPushButton[variant="chip"]:checked {{
    background-color: {g["ACCENT"]}; border-color: {g["ACCENT"]}; color: {g["ON_ACCENT"]};
}}
QPushButton[variant="week"]:disabled, QPushButton[variant="chip"]:disabled {{ color: {g["FAINT"]}; background-color: {g["SURFACE"]}; }}
/* Después de las variantes: ningún borde de reposo tapa el foco. */
QPushButton[variant="primary"]:focus {{ border: 2px solid {g["TEXT"]}; padding: 8px 17px; }}
QPushButton[variant="week"]:focus {{ border: 2px solid {g["LINK"]}; padding: 2px 5px; }}
QPushButton[variant="chip"]:focus {{ border: 2px solid {g["LINK"]}; padding: 3px 10px; }}
QPushButton[variant="week"]:checked:focus, QPushButton[variant="chip"]:checked:focus {{ border-color: {g["TEXT"]}; }}
QPushButton[variant="danger"]:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 14px; }}
QPushButton[variant="nav"]:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 11px; }}
QPushButton[variant="ghost"]:focus {{ border: 2px solid {g["LINK"]}; padding: 6px 9px; }}
QPushButton[variant="icon"]:focus {{ border: 2px solid {g["LINK"]}; padding: 5px; }}
QPushButton[variant="secondary"]:focus {{ border: 2px solid {g["LINK"]}; padding: 6px 12px; }}
QPushButton[variant="link"]:focus {{ border: none; padding: 2px 2px; color: {g["TEXT"]}; }}
QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton:hover {{ background-color: {g["SURFACE_3"]}; }}

/* ── Menús ─────────────────────────────────────────────────────── */
QMenu {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; margin: 1px 0; }}
QMenu::item:selected {{ background-color: {g["ACCENT_SUBTLE"]}; color: {g["TEXT"]}; }}
QMenu::item:disabled {{ color: {g["FAINT"]}; }}
QMenu::separator {{ height: 1px; background: {g["BORDER"]}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 8px; }}

/* ── Calendario ────────────────────────────────────────────────── */
QCalendarWidget QWidget {{ background-color: {g["SURFACE"]}; color: {g["TEXT"]}; }}
QCalendarWidget QToolButton {{
    background-color: transparent; color: {g["TEXT"]};
    font-weight: 600; border-radius: 6px; padding: 4px 8px;
}}
QCalendarWidget QToolButton:hover {{ background-color: {g["SURFACE_3"]}; }}
QCalendarWidget QAbstractItemView {{
    selection-background-color: {g["ACCENT"]};
    selection-color: {g["ON_ACCENT"]};
}}

/* ── Tablas, listas y árboles ──────────────────────────────────── */
QTableWidget, QTableView {{
    background-color: {g["SURFACE"]};
    alternate-background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER"]};
    border-radius: {RADIUS}px;
    gridline-color: transparent;
    selection-background-color: {g["ACCENT_SUBTLE"]};
    selection-color: {g["TEXT"]};
}}
QTableWidget::item, QTableView::item {{
    padding: 6px 10px;
    border-bottom: 1px solid {g["BORDER"]};
}}
QTableWidget::item:hover, QTableView::item:hover {{ background-color: {g["SURFACE_2"]}; }}
QTableWidget::item:selected, QTableView::item:selected {{ background-color: {g["ACCENT_SUBTLE"]}; color: {g["TEXT"]}; }}
QTableWidget::item:focus {{ border: 1px solid {g["LINK"]}; padding: 5px 9px; }}
QHeaderView {{ background-color: transparent; border: none; }}
QHeaderView::section {{
    background-color: {g["SURFACE"]};
    color: {g["FAINT"]};
    border: none;
    border-bottom: 1px solid {g["BORDER"]};
    padding: 8px 10px;
    font-weight: 650;
    font-size: 11.5px;
}}
QTableCornerButton::section {{ background-color: {g["SURFACE"]}; border: none; }}
QTreeWidget, QTreeView, QListWidget, QListView {{
    background-color: transparent;
    border: none;
    selection-background-color: {g["ACCENT_SUBTLE"]};
    selection-color: {g["TEXT"]};
}}
QTreeWidget::item, QTreeView::item {{ padding: 5px 4px; border-radius: 6px; min-height: 22px; }}
QListWidget::item, QListView::item {{ padding: 7px 8px; border-radius: 8px; }}
QTreeWidget::item:hover, QTreeView::item:hover, QListWidget::item:hover, QListView::item:hover {{
    background-color: {g["SURFACE_2"]};
}}
QTreeWidget::item:selected, QTreeView::item:selected, QListWidget::item:selected, QListView::item:selected {{
    background-color: {g["ACCENT_SUBTLE"]}; color: {g["TEXT"]};
}}
QTreeView::branch {{ background: transparent; }}
QListWidget#palette {{ background-color: transparent; }}
QListWidget#palette::item {{ padding: 0px; border-radius: 9px; margin: 1px 0; }}

/* ── Consola ───────────────────────────────────────────────────── */
QPlainTextEdit#console {{
    background-color: {g["CONSOLE_BG"]};
    border: 1px solid {g["BORDER"]};
    border-radius: 10px;
    padding: 12px;
    font-family: {FONT_MONO};
    font-size: 12.5px;
    color: {g["CONSOLE_FG"]};
}}
QPlainTextEdit#console:focus {{ border: 1px solid {g["BORDER_HI"]}; padding: 12px; }}

/* ── Misceláneos ───────────────────────────────────────────────── */
QProgressBar {{
    background-color: {g["SURFACE_3"]};
    border: none; border-radius: 4px; max-height: 8px; min-height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background-color: {g["ACCENT"]}; border-radius: 4px; }}
QProgressBar[thin="true"] {{ max-height: 4px; min-height: 4px; border-radius: 2px; }}
QProgressBar[thin="true"]::chunk {{ border-radius: 2px; }}
QToolTip {{
    background-color: {g["SURFACE_3"]};
    color: {g["TEXT"]};
    border: 1px solid {g["BORDER_HI"]};
    padding: 6px 9px;
    border-radius: 7px;
    font-size: 12.5px;
}}
QMessageBox, QWizard QFrame {{ background-color: {g["SURFACE"]}; }}
QMessageBox QLabel {{ font-size: 13.5px; }}
QCheckBox, QRadioButton {{ spacing: 9px; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {g["MUTED"]}; }}
QCheckBox::indicator, QAbstractItemView::indicator {{
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid {g["BORDER_HI"]};
    background-color: {g["SURFACE_2"]};
}}
QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {{ border-color: {g["LINK"]}; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background-color: {g["ACCENT"]};
    border-color: {g["ACCENT"]};
    image: url("{g["CHECK_ICON"]}");
}}
QCheckBox::indicator:focus {{ border: 2px solid {g["LINK"]}; width: 16px; height: 16px; }}
QCheckBox::indicator:checked:focus {{ border-color: {g["TEXT"]}; }}
QCheckBox::indicator:disabled, QAbstractItemView::indicator:disabled {{ border-color: {g["BORDER"]}; }}
QRadioButton::indicator {{
    width: 16px; height: 16px; border-radius: 9px;
    border: 1px solid {g["BORDER_HI"]}; background-color: {g["SURFACE_2"]};
}}
QRadioButton::indicator:checked {{ border: 5px solid {g["ACCENT"]}; background-color: {g["ON_ACCENT"]}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {g["BORDER_HI"]}; border-radius: 3px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {g["FAINT"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {g["BORDER_HI"]}; border-radius: 3px; min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {g["FAINT"]}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}
QTabWidget::pane {{ border: 1px solid {g["BORDER"]}; border-radius: {RADIUS}px; top: -1px; background: {g["SURFACE"]}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {g["MUTED"]}; padding: 9px 16px; font-weight: 600;
    border-bottom: 2px solid transparent; margin-right: 2px;
}}
QTabBar::tab:selected {{ color: {g["LINK"]}; border-bottom: 2px solid {g["LINK"]}; }}
QTabBar::tab:hover {{ color: {g["TEXT"]}; }}
QTabBar::tab:focus {{ color: {g["TEXT"]}; border-bottom: 2px solid {g["LINK"]}; }}
QGroupBox {{
    border: 1px solid {g["BORDER"]}; border-radius: {RADIUS}px; margin-top: 14px; padding: 14px 12px 10px 12px;
    font-weight: 650;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {g["MUTED"]}; }}
QDialogButtonBox QPushButton {{ min-width: 84px; }}
QStatusBar {{ background: {g["BG"]}; }}
"""


def configure_table(table) -> None:
    """Ritmo compartido; no cambia edición, selección, datos ni señales."""
    table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
    table.verticalHeader().setMinimumSectionSize(ROW_HEIGHT)
    table.horizontalHeader().setFixedHeight(HEADER_HEIGHT)
    table.horizontalHeader().setHighlightSections(False)
    from PySide6.QtCore import Qt

    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


def apply_theme(app, mode: str = "dark") -> None:
    global current_mode
    current_mode = mode if mode in PALETTES else "dark"
    globals().update(PALETTES[current_mode])
    import sys
    from pathlib import Path

    from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

    asset_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    globals()["CHECK_ICON"] = (
        asset_root / "assets" / "ui" / f"check-{current_mode}.svg"
    ).as_posix()
    if not Path(globals()["CHECK_ICON"]).is_file():
        globals()["CHECK_ICON"] = (asset_root / "assets" / "check.svg").as_posix()
    for direction in ("down", "up"):
        globals()[direction.upper() + "_ICON"] = (
            asset_root / "assets" / "ui" / f"chevron-{direction}-{current_mode}.svg"
        ).as_posix()
    if "Inter" not in QFontDatabase.families():
        QFontDatabase.addApplicationFont(str(asset_root / "assets" / "fonts" / "Inter.ttf"))
    available = set(QFontDatabase.families())
    family = next(
        (f for f in ("Inter", "Segoe UI", "Noto Sans", "DejaVu Sans") if f in available),
        "Sans Serif",
    )
    font = QFont(family, 10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    app.setStyle("Fusion")
    palette = QPalette()
    for role, key in (
        (QPalette.ColorRole.Window, "BG"),
        (QPalette.ColorRole.Base, "SURFACE"),
        (QPalette.ColorRole.AlternateBase, "SURFACE_2"),
        (QPalette.ColorRole.Text, "TEXT"),
        (QPalette.ColorRole.WindowText, "TEXT"),
        (QPalette.ColorRole.Button, "SURFACE_2"),
        (QPalette.ColorRole.ButtonText, "TEXT"),
        (QPalette.ColorRole.Highlight, "ACCENT"),
        (QPalette.ColorRole.HighlightedText, "ON_ACCENT"),
        (QPalette.ColorRole.PlaceholderText, "FAINT"),
        (QPalette.ColorRole.Link, "LINK"),
        (QPalette.ColorRole.LinkVisited, "LINK"),
        (QPalette.ColorRole.ToolTipBase, "SURFACE_3"),
        (QPalette.ColorRole.ToolTipText, "TEXT"),
    ):
        palette.setColor(role, QColor(globals()[key]))
    app.setPalette(palette)
    app.setStyleSheet(_build_qss())
    _notify_theme_change()
