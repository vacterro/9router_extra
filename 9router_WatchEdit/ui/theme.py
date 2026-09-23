"""
9router_WatchEdit - Golden Default (Wintage) Theme Engine
Implements UI.md specifications: 21 exact Golden Default tokens, non-antialiased Verdana,
2px Win95 bevels, zero animations, high-contrast operator aesthetic.
"""
from PySide6.QtGui import QFont, QPalette, QColor
from PySide6.QtWidgets import QApplication

# 21 Exact Golden Default Tokens
COLOR_BACKGROUND = "#1A1810"
COLOR_BACKGROUND_SOFT = "#232018"
COLOR_SURFACE = "#332E22"
COLOR_SURFACE_RAISED = "#3D372A"
COLOR_SURFACE_ALT = "#453D30"

COLOR_BORDER_DARK = "#100E08"
COLOR_BORDER_HIGHLIGHT = "#F0D060"
COLOR_BEVEL_LIGHT = "#75663D"
COLOR_BORDER_MUTED = "#5A5040"

COLOR_TEXT_PRIMARY = "#D4C89A"
COLOR_TEXT_SECONDARY = "#9C9371"
COLOR_TEXT_MUTED = "#6E674E"

COLOR_ACCENT_TEAL = "#008080"
COLOR_ACCENT_TEAL_DEEP = "#004C4C"

COLOR_SUCCESS = "#4A7A20"
COLOR_WARNING = "#7A7A20"
COLOR_DANGER = "#7A2020"
COLOR_DANGER_TEXT = "#D66464"

COLOR_SELECTION = "#3D372A"
COLOR_COMPARE_BACK = "#14120C"
COLOR_LINK = "#F0D060"

# Badges Background & Text Mapping
# CORE-003: keys are the CANONICAL persisted state values produced by
# get_ui_badge / ModelHealthRecord.state. Legacy display labels are kept as
# fallback keys so any older string still renders.
STATE_COLORS = {
    "FREE/USE": {"bg": COLOR_SUCCESS, "fg": "#FFFFFF"},
    "PAID": {"bg": COLOR_SURFACE_RAISED, "fg": COLOR_BORDER_HIGHLIGHT, "border": COLOR_BEVEL_LIGHT},
    "BALANCE_REQUIRED": {"bg": COLOR_WARNING, "fg": "#FFFFFF"},
    "AUTH_REJECTED": {"bg": COLOR_DANGER, "fg": "#FFFFFF"},
    "ACCESS_FORBIDDEN": {"bg": COLOR_DANGER, "fg": "#FFFFFF"},
    "RATE_LIMITED": {"bg": "#8A6D1C", "fg": "#FFFFFF"},
    "PENDING": {"bg": COLOR_ACCENT_TEAL, "fg": "#FFFFFF"},
    "CONNECT_TIMEOUT": {"bg": "#6B4226", "fg": "#FFFFFF"},
    "PROVIDER_ERROR": {"bg": "#5A3434", "fg": "#E0A0A0"},
    "ENDPOINT_OR_MODEL_INVALID": {"bg": "#4A324A", "fg": "#DDA0DD"},
    "MODEL_INVALID": {"bg": "#4A324A", "fg": "#DDA0DD"},
    "MODEL_MISSING": {"bg": "#4A324A", "fg": "#DDA0DD"},
    "ROUTE_ERROR": {"bg": "#4A324A", "fg": "#DDA0DD"},
    "MODEL_GONE": {"bg": "#4A324A", "fg": "#DDA0DD"},
    "ROUTER_DEGRADED": {"bg": "#5A3434", "fg": "#E0A0A0"},
    "DNS_FAILURE": {"bg": "#5A3434", "fg": "#E0A0A0"},
    "NON_API_HTML_RESPONSE": {"bg": "#4A4A2A", "fg": "#D8D890"},
    "WAF_BLOCKED": {"bg": "#4A4A2A", "fg": "#D8D890"},
    "BROWSER_CHALLENGE": {"bg": "#4A4A2A", "fg": "#D8D890"},
    "MODEL_DISCOVERY_UNAVAILABLE": {"bg": COLOR_SURFACE_ALT, "fg": COLOR_TEXT_SECONDARY},
    "DEAD": {"bg": COLOR_DANGER, "fg": "#FFFFFF"},
    "UNKNOWN": {"bg": COLOR_SURFACE_ALT, "fg": COLOR_TEXT_SECONDARY},
    # Legacy display-label fallbacks.
    "BALANCE": {"bg": COLOR_WARNING, "fg": "#FFFFFF"},
    "AUTH": {"bg": COLOR_DANGER, "fg": "#FFFFFF"},
    "RATE LIMIT": {"bg": "#8A6D1C", "fg": "#FFFFFF"},
    "TIMEOUT": {"bg": "#6B4226", "fg": "#FFFFFF"},
    "TEMP ERROR": {"bg": "#5A3434", "fg": "#E0A0A0"},
    "MODEL MISSING": {"bg": "#4A324A", "fg": "#DDA0DD"},
}

GOLDEN_DEFAULT_QSS = f"""
/* -------------------------------------------------------------
   Golden Default (Wintage) - Win95 Dark Golden Style
------------------------------------------------------------- */
* {{
    border-radius: 0px;
    outline: none;
}}

QMainWindow, QDialog, QWidget#centralWidget {{
    background-color: {COLOR_BACKGROUND};
    color: {COLOR_TEXT_PRIMARY};
}}

QWidget {{
    background-color: {COLOR_BACKGROUND};
    color: {COLOR_TEXT_PRIMARY};
}}

/* Panels and GroupBoxes */
QGroupBox {{
    background-color: {COLOR_SURFACE};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    margin-top: 10px;
    padding-top: 6px;
    font-weight: bold;
    color: {COLOR_TEXT_PRIMARY};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 4px;
    background-color: {COLOR_SURFACE};
    color: {COLOR_BORDER_HIGHLIGHT};
}}

QFrame#beveledFrameRaised {{
    background-color: {COLOR_SURFACE};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
}}

QFrame#beveledFrameSunken {{
    background-color: {COLOR_BACKGROUND_SOFT};
    border: 2px solid;
    border-top-color: {COLOR_BORDER_DARK};
    border-left-color: {COLOR_BORDER_DARK};
    border-bottom-color: {COLOR_BEVEL_LIGHT};
    border-right-color: {COLOR_BEVEL_LIGHT};
}}

/* Buttons: Classic 2px Win95 Bevel */
QPushButton {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT_PRIMARY};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    padding: 2px 6px;
    min-height: 18px;
    font-weight: bold;
}}

QPushButton:hover {{
    background-color: {COLOR_SURFACE_RAISED};
    color: {COLOR_BORDER_HIGHLIGHT};
}}

QPushButton:pressed {{
    background-color: {COLOR_SURFACE_ALT};
    border-top-color: {COLOR_BORDER_DARK};
    border-left-color: {COLOR_BORDER_DARK};
    border-bottom-color: {COLOR_BEVEL_LIGHT};
    border-right-color: {COLOR_BEVEL_LIGHT};
    padding-top: 5px;
    padding-left: 11px;
}}

QPushButton:disabled {{
    background-color: {COLOR_BACKGROUND_SOFT};
    color: {COLOR_TEXT_MUTED};
    border-color: {COLOR_BORDER_MUTED};
}}

QPushButton#primaryAction {{
    background-color: {COLOR_ACCENT_TEAL_DEEP};
    color: {COLOR_BORDER_HIGHLIGHT};
    border-top-color: {COLOR_ACCENT_TEAL};
    border-left-color: {COLOR_ACCENT_TEAL};
}}

QPushButton#primaryAction:hover {{
    background-color: {COLOR_ACCENT_TEAL};
    color: #FFFFFF;
}}

QPushButton#dangerAction {{
    background-color: {COLOR_DANGER};
    color: #FFFFFF;
}}

/* Sunken Input Controls */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background-color: {COLOR_BACKGROUND_SOFT};
    color: {COLOR_TEXT_PRIMARY};
    border: 2px solid;
    border-top-color: {COLOR_BORDER_DARK};
    border-left-color: {COLOR_BORDER_DARK};
    border-bottom-color: {COLOR_BEVEL_LIGHT};
    border-right-color: {COLOR_BEVEL_LIGHT};
    padding: 3px 5px;
    selection-background-color: {COLOR_SELECTION};
    selection-color: {COLOR_BORDER_HIGHLIGHT};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border-color: {COLOR_BORDER_HIGHLIGHT};
}}

/* ComboBox */
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid {COLOR_BORDER_MUTED};
    background-color: {COLOR_SURFACE};
}}

QComboBox QAbstractItemView {{
    background-color: {COLOR_BACKGROUND_SOFT};
    color: {COLOR_TEXT_PRIMARY};
    border: 2px solid {COLOR_BORDER_DARK};
    selection-background-color: {COLOR_SELECTION};
    selection-color: {COLOR_BORDER_HIGHLIGHT};
}}

/* Tables and Lists */
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget, QListView {{
    background-color: {COLOR_BACKGROUND_SOFT};
    color: {COLOR_TEXT_PRIMARY};
    border: 2px solid;
    border-top-color: {COLOR_BORDER_DARK};
    border-left-color: {COLOR_BORDER_DARK};
    border-bottom-color: {COLOR_BEVEL_LIGHT};
    border-right-color: {COLOR_BEVEL_LIGHT};
    gridline-color: {COLOR_SURFACE};
    selection-background-color: {COLOR_SELECTION};
    selection-color: {COLOR_BORDER_HIGHLIGHT};
}}

QHeaderView::section {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT_PRIMARY};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    padding: 2px 4px;
    font-weight: bold;
}}

/* Tabs */
QTabWidget::pane {{
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    background-color: {COLOR_SURFACE};
}}

QTabBar::tab {{
    background-color: {COLOR_BACKGROUND_SOFT};
    color: {COLOR_TEXT_SECONDARY};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    padding: 3px 8px;
    margin-right: 2px;
    font-weight: bold;
}}

QTabBar::tab:selected {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_BORDER_HIGHLIGHT};
    border-bottom-color: {COLOR_SURFACE};
}}

QTabBar::tab:hover:!selected {{
    color: {COLOR_TEXT_PRIMARY};
}}

/* Scrollbars: Classic Win95 Beveled */
QScrollBar:vertical {{
    background-color: {COLOR_BACKGROUND_SOFT};
    width: 16px;
    border: 1px solid {COLOR_BORDER_DARK};
}}

QScrollBar::handle:vertical {{
    background-color: {COLOR_SURFACE};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    min-height: 20px;
}}

QScrollBar::handle:vertical:pressed {{
    background-color: {COLOR_SURFACE_ALT};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    background-color: {COLOR_SURFACE};
    height: 14px;
    border: 1px solid {COLOR_BORDER_DARK};
}}

QScrollBar:horizontal {{
    background-color: {COLOR_BACKGROUND_SOFT};
    height: 16px;
    border: 1px solid {COLOR_BORDER_DARK};
}}

QScrollBar::handle:horizontal {{
    background-color: {COLOR_SURFACE};
    border: 2px solid;
    border-top-color: {COLOR_BEVEL_LIGHT};
    border-left-color: {COLOR_BEVEL_LIGHT};
    border-bottom-color: {COLOR_BORDER_DARK};
    border-right-color: {COLOR_BORDER_DARK};
    min-width: 20px;
}}

/* Progress Bar */
QProgressBar {{
    background-color: {COLOR_BACKGROUND_SOFT};
    border: 2px solid;
    border-top-color: {COLOR_BORDER_DARK};
    border-left-color: {COLOR_BORDER_DARK};
    border-bottom-color: {COLOR_BEVEL_LIGHT};
    border-right-color: {COLOR_BEVEL_LIGHT};
    text-align: center;
    color: {COLOR_TEXT_PRIMARY};
    font-weight: bold;
}}

QProgressBar::chunk {{
    background-color: {COLOR_ACCENT_TEAL};
}}

/* Splitters */
QSplitter::handle {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER_DARK};
}}

/* Labels and Status Bar */
QLabel {{
    color: {COLOR_TEXT_PRIMARY};
}}

QStatusBar {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT_SECONDARY};
    border-top: 2px solid {COLOR_BEVEL_LIGHT};
}}

QStatusBar::item {{
    border: 1px solid {COLOR_BORDER_DARK};
}}
"""

def get_app_font(size: int = 11, bold: bool = False) -> QFont:
    """Returns a Verdana font strictly configured with 100% aliased bitmap-style rendering."""
    if size not in (10, 11, 12, 14, 16):
        size = 11
    font = QFont("Verdana", size)
    font.setBold(bold)
    strat = QFont.StyleStrategy(
        QFont.StyleStrategy.NoAntialias.value
        | QFont.StyleStrategy.NoSubpixelAntialias.value
        | QFont.StyleStrategy.PreferBitmap.value
    )
    font.setStyleStrategy(strat)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font

def apply_theme(app: QApplication):
    """Applies the Golden Default 100% aliased Verdana theme to the PySide6 application."""
    # Apply stylesheet first so CSS parsing does not reset application-level font flags
    app.setStyleSheet(GOLDEN_DEFAULT_QSS)

    # Apply strict 100% aliased font globally
    font = get_app_font(11)
    app.setFont(font)

    # Explicitly bind to all widget classes
    for widget_class in (
        "QWidget",
        "QDialog",
        "QMainWindow",
        "QTableWidget",
        "QTableView",
        "QHeaderView",
        "QListWidget",
        "QListView",
        "QTreeWidget",
        "QTreeView",
        "QLabel",
        "QPushButton",
        "QLineEdit",
        "QTextEdit",
        "QPlainTextEdit",
        "QComboBox",
        "QTabBar",
        "QTabWidget",
        "QGroupBox",
        "QStatusBar",
        "QAbstractItemView",
    ):
        app.setFont(font, widget_class)


