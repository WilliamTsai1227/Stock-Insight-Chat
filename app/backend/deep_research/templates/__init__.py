"""深度研究產出的 HTML 樣板（自帶樣式、可離線開啟）。"""

from .deck import render_deck
from .report import render_report
from .themes import DEFAULT_THEME, THEMES, Theme, public_themes, resolve_theme

__all__ = [
    "render_deck",
    "render_report",
    "THEMES",
    "Theme",
    "DEFAULT_THEME",
    "public_themes",
    "resolve_theme",
]
