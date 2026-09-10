"""Tema nativo de Zoom2TXT: neutros, verde profundo y acento ámbar.
Modo claro y oscuro desde los mismos tokens de esta fuente de verdad.

apply_theme(app, mode) fija la paleta activa como variables de módulo
(BG, SURFACE, …) para que el resto de la GUI las lea, y aplica el QSS.
"""

PALETTES = {
    "dark": dict(
        BG="#141a18",
        SURFACE="#1b2420",
        SURFACE_2="#202b26",
        SURFACE_3="#293730",
        BORDER="#34453c",
        BORDER_HI="#70877b",
        TEXT="#eef4f0",
        MUTED="#b4c6bc",
        FAINT="#9cb2a5",
        ACCENT="#176b52",
        ACCENT_HI="#125740",
        ACCENT_DN="#104b38",
        ACCENT_SUBTLE="#233e31",
        ON_ACCENT="#ffffff",
        LINK="#82dfb6",
        SUCCESS="#82dfb6",
        ERROR="#ffa69e",
        WARNING="#efc374",
        CONSOLE_BG="#141a18",
        CONSOLE_FG="#c5d6cd",
    ),
    "light": dict(
        BG="#f6f8f7",
        SURFACE="#ffffff",
        SURFACE_2="#f0f4f2",
        SURFACE_3="#e7efe9",
        BORDER="#d6e1da",
        BORDER_HI="#71877b",
        TEXT="#18241f",
        MUTED="#405c4d",
        FAINT="#526f5f",
        ACCENT="#176b52",
        ACCENT_HI="#125740",
        ACCENT_DN="#104b38",
        ACCENT_SUBTLE="#e3f2e9",
        ON_ACCENT="#ffffff",
        LINK="#176b52",
        SUCCESS="#176b52",
        ERROR="#b42318",
        WARNING="#935300",
        CONSOLE_BG="#f0f4f2",
        CONSOLE_FG="#243c2d",
    ),
}

FONT_UI = '"Inter", "Segoe UI", "Noto Sans", sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'

# Paleta activa (defaults; apply_theme la reemplaza)
current_mode = "dark"
CHECK_ICON = ""
DOWN_ICON = ""
UP_ICON = ""
ROW_HEIGHT = 36
HEADER_HEIGHT = 36
globals().update(PALETTES["dark"])


def _build_qss() -> str:
    g = globals()
    return f"""
* {{
    font-family: {FONT_UI};
    font-size: 14px;
    color: {g["TEXT"]};
}}

QMainWindow, QDialog, QWizard, QWidget#root {{
    background-color: {g["BG"]};
}}
QWidget#sidebar {{
    background-color: {g["SURFACE"]};
    border-right: 1px solid {g["BORDER"]};
}}

/* ── Tipografía ────────────────────────────────────────────────── */
QLabel#h1 {{ font-size: 24px; font-weight: 700; letter-spacing: -0.3px; }}
QLabel#h2 {{ font-size: 16px; font-weight: 600; }}
QLabel#muted, QLabel#hint {{ color: {g["MUTED"]}; font-size: 13px; }}
QLabel#faint {{ color: {g["FAINT"]}; font-size: 12px; }}
QLabel#accent {{ color: {g["LINK"]}; font-weight: 600; }}

/* ── Tarjetas ──────────────────────────────────────────────────── */
QFrame#card {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER"]};
    border-radius: 8px;
}}
QFrame#workspace, QFrame#metric {{ background: transparent; border: none; }}
QFrame#healthSummary {{
    background-color: {g["ACCENT_SUBTLE"]};
    border: 1px solid {g["BORDER"]};
    border-radius: 10px;
}}
QLabel#healthHeadline {{ font-size: 19px; font-weight: 700; letter-spacing: -0.2px; }}

/* ── Navegación lateral ────────────────────────────────────────── */
QPushButton[variant="nav"] {{
    background-color: transparent;
    border: 2px solid transparent;
    border-radius: 8px;
    padding: 7px 10px;
    text-align: left;
    font-weight: 500;
    color: {g["MUTED"]};
}}
QPushButton[variant="nav"]:hover {{
    background-color: {g["SURFACE_3"]};
    color: {g["TEXT"]};
}}
QPushButton[variant="nav"]:checked {{
    background-color: {g["ACCENT_SUBTLE"]};
    color: {g["LINK"]};
    font-weight: 600;
}}

/* ── Inputs ────────────────────────────────────────────────────── */
QLineEdit, QComboBox, QDateEdit, QSpinBox {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 8px;
    padding: 9px 12px;
    selection-background-color: {g["ACCENT"]};
    selection-color: {g["ON_ACCENT"]};
    placeholder-text-color: {g["FAINT"]};
}}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover, QSpinBox:hover {{
    border-color: {g["FAINT"]};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{
    border: 2px solid {g["LINK"]};
    padding: 8px 11px;
}}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled {{
    color: {g["FAINT"]};
    background-color: {g["SURFACE_2"]};
}}
QComboBox, QDateEdit, QSpinBox {{ padding-right: 36px; }}
QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{ padding-right: 35px; }}
QComboBox::drop-down, QDateEdit::drop-down {{
    subcontrol-origin: padding; subcontrol-position: top right;
    border: none; width: 32px;
}}
QComboBox::down-arrow, QDateEdit::down-arrow, QSpinBox::down-arrow {{
    image: url("{g["DOWN_ICON"]}"); width: 14px; height: 14px;
}}
QSpinBox::up-arrow {{ image: url("{g["UP_ICON"]}"); width: 14px; height: 14px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 32px;
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
    selection-background-color: {g["ACCENT"]};
    selection-color: {g["ON_ACCENT"]};
    border-radius: 8px;
}}

/* ── Botones ───────────────────────────────────────────────────── */
QPushButton {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {g["SURFACE_3"]};
}}
QPushButton:pressed {{
    background-color: {g["SURFACE_2"]};
    padding-top: 9px; padding-bottom: 7px;
}}
QPushButton:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 15px; }}
QPushButton:disabled {{
    color: {g["FAINT"]};
    background-color: {g["SURFACE_2"]};
    border-color: {g["BORDER"]};
}}
QPushButton[variant="primary"] {{
    background-color: {g["ACCENT"]};
    border: 1px solid {g["ACCENT"]};
    color: {g["ON_ACCENT"]};
    padding: 9px 20px;
}}
QPushButton[variant="primary"]:hover {{
    background-color: {g["ACCENT_HI"]};
    border-color: {g["ACCENT_HI"]};
}}
QPushButton[variant="primary"]:pressed {{
    background-color: {g["ACCENT_DN"]};
    border-color: {g["ACCENT_DN"]};
}}
QPushButton[variant="primary"]:disabled {{
    background-color: {g["SURFACE_2"]};
    border-color: {g["BORDER"]};
    color: {g["FAINT"]};
}}
QPushButton[variant="danger"] {{
    background-color: transparent;
    border: 1px solid {g["ERROR"]};
    color: {g["ERROR"]};
}}
QPushButton[variant="danger"]:hover {{
    background-color: {g["SURFACE_3"]};
    color: {g["ERROR"]};
}}
QPushButton[variant="week"] {{
    background-color: {g["SURFACE"]};
    border: 1px solid {g["BORDER_HI"]};
    border-radius: 8px;
    padding: 3px 6px;
    font-weight: 600;
    font-size: 13px;
    color: {g["MUTED"]};
}}
QPushButton[variant="week"]:hover {{
    border-color: {g["ACCENT"]};
    color: {g["TEXT"]};
}}
QPushButton[variant="week"]:checked {{
    background-color: {g["ACCENT"]};
    border-color: {g["ACCENT"]};
    color: {g["ON_ACCENT"]};
}}
/* Después de las variantes: ningún borde de reposo tapa el foco. */
QPushButton[variant="primary"]:focus {{
    border: 2px solid {g["ON_ACCENT"]}; padding: 8px 19px;
}}
QPushButton[variant="week"]:focus {{
    border: 2px solid {g["LINK"]}; padding: 2px 5px;
}}
QPushButton[variant="week"]:checked:focus {{ border-color: {g["ON_ACCENT"]}; }}
QPushButton[variant="danger"]:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 15px; }}
QPushButton[variant="nav"]:focus {{ border: 2px solid {g["LINK"]}; padding: 7px 10px; }}

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

/* ── Tablas ────────────────────────────────────────────────────── */
QTableWidget {{
    background-color: {g["SURFACE"]};
    alternate-background-color: {g["SURFACE_2"]};
    border: 1px solid {g["BORDER"]};
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: {g["ACCENT_SUBTLE"]};
    selection-color: {g["TEXT"]};
}}
QTableWidget::item {{
    padding: 6px 10px;
    border-bottom: 1px solid {g["BORDER"]};
}}
QTableWidget::item:focus {{ border: 1px solid {g["LINK"]}; padding: 5px 9px; }}
QHeaderView::section {{
    background-color: {g["SURFACE"]};
    color: {g["FAINT"]};
    border: none;
    border-bottom: 1px solid {g["BORDER_HI"]};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 12px;
}}
QTableCornerButton::section {{ background-color: {g["SURFACE"]}; border: none; }}

/* ── Consola ───────────────────────────────────────────────────── */
QPlainTextEdit#console {{
    background-color: {g["CONSOLE_BG"]};
    border: 1px solid {g["BORDER"]};
    border-radius: 8px;
    padding: 12px;
    font-family: {FONT_MONO};
    font-size: 12.5px;
    color: {g["CONSOLE_FG"]};
}}

/* ── Misceláneos ───────────────────────────────────────────────── */
QProgressBar {{
    background-color: {g["SURFACE_3"]};
    border: none; border-radius: 5px; height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background-color: {g["ACCENT"]}; border-radius: 5px; }}
QToolTip {{
    background-color: {g["SURFACE"]};
    color: {g["TEXT"]};
    border: 1px solid {g["BORDER_HI"]};
    padding: 6px 8px;
    border-radius: 6px;
}}
QMessageBox, QWizard QFrame {{ background-color: {g["SURFACE"]}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox:disabled {{ color: {g["MUTED"]}; }}
QCheckBox::indicator, QAbstractItemView::indicator {{
    width: 18px; height: 18px; border-radius: 4px;
    border: 1px solid {g["BORDER_HI"]};
    background-color: {g["SURFACE"]};
}}
QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {{ border-color: {g["LINK"]}; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background-color: {g["ACCENT"]};
    border-color: {g["ACCENT"]};
    image: url("{g["CHECK_ICON"]}");
}}
QCheckBox::indicator:focus {{ border: 2px solid {g["LINK"]}; width: 16px; height: 16px; }}
QCheckBox::indicator:checked:focus {{ border-color: {g["ON_ACCENT"]}; }}
QCheckBox::indicator:disabled, QAbstractItemView::indicator:disabled {{ border-color: {g["BORDER"]}; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {g["BORDER_HI"]}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {g["FAINT"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {g["BORDER_HI"]}; border-radius: 4px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QTabWidget::pane {{ border: 1px solid {g["BORDER"]}; border-radius: 8px; }}
QTabBar::tab {{ background: {g["SURFACE_2"]}; color: {g["MUTED"]}; padding: 10px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {g["LINK"]}; border-bottom: 2px solid {g["LINK"]}; background: {g["SURFACE"]}; }}
QTabBar::tab:hover {{ background: {g["SURFACE_3"]}; }}
QTabBar::tab:focus {{ border-bottom: 2px solid {g["LINK"]}; color: {g["TEXT"]}; }}
"""


def configure_table(table) -> None:
    """Ritmo compartido; no cambia edición, selección, datos ni señales."""
    table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
    table.verticalHeader().setMinimumSectionSize(ROW_HEIGHT)
    table.horizontalHeader().setFixedHeight(HEADER_HEIGHT)


def apply_theme(app, mode: str = "dark") -> None:
    global current_mode
    current_mode = mode if mode in PALETTES else "dark"
    globals().update(PALETTES[current_mode])
    import sys
    from pathlib import Path

    from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

    asset_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
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
    app.setFont(QFont(family, 10))
    app.setStyle("Fusion")
    palette = QPalette()
    for role, key in (
        (QPalette.ColorRole.Window, "BG"),
        (QPalette.ColorRole.Base, "SURFACE"),
        (QPalette.ColorRole.Text, "TEXT"),
        (QPalette.ColorRole.WindowText, "TEXT"),
        (QPalette.ColorRole.Button, "SURFACE_2"),
        (QPalette.ColorRole.ButtonText, "TEXT"),
        (QPalette.ColorRole.Highlight, "ACCENT"),
        (QPalette.ColorRole.HighlightedText, "ON_ACCENT"),
        (QPalette.ColorRole.PlaceholderText, "FAINT"),
        (QPalette.ColorRole.Link, "LINK"),
        (QPalette.ColorRole.LinkVisited, "LINK"),
    ):
        palette.setColor(role, QColor(globals()[key]))
    app.setPalette(palette)
    app.setStyleSheet(_build_qss())
