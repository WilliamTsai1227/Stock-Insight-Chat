"""
深度研究產出的樣板。

同一份結構化物件（`ReportDoc` / `DeckDoc`）可以走三條 renderer：
HTML（自帶樣式、離線可開、可預覽）、docx（報告）、pptx（簡報）。
三者都吃同一個 `Theme`，所以換風格會同時反映在所有格式上。
"""

from .deck import render_deck
from .deck_pptx import render_deck_pptx
from .report import render_report
from .report_docx import render_report_docx
from .themes import DEFAULT_THEME, THEMES, Theme, public_themes, resolve_theme

__all__ = [
    "render_deck",
    "render_deck_pptx",
    "render_report",
    "render_report_docx",
    "THEMES",
    "Theme",
    "DEFAULT_THEME",
    "public_themes",
    "resolve_theme",
]
