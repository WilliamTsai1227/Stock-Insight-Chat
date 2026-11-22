"""樣板共用：轉義、Markdown 轉換、色票。"""

from __future__ import annotations

import nh3
from html import escape as _html_escape
from typing import Any

# 兩份樣板共用的設計 token，改這裡就能同時換色
PALETTE = """
    --bg:#ffffff;
    --bg-soft:#f6f7f9;
    --ink:#14171c;
    --ink2:#2b303a;
    --body:#565f6d;
    --muted:#8a93a1;
    --line:#e7e9ee;
    --accent:#2f6bff;
    --accent-soft:#eef3ff;
    --accent-line:#dbe6ff;
    --green:#12924f;
    --amber:#b0730e;
    --mono:"SFMono-Regular",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;
"""

# Parse HTML with an allowlist: regex cannot handle entities or malformed tags.
_MARKDOWN_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b",
    "em", "i", "s", "del", "blockquote", "pre", "code", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td", "a", "sup", "sub",
}


def esc(value: Any) -> str:
    """所有進入樣板的 LLM 產出都必須經過這裡。"""
    return _html_escape(str(value or ""), quote=True)


def scrub_html(html: str) -> str:
    return nh3.clean(html, tags=_MARKDOWN_TAGS,
                     attributes={"a": {"href", "title"}},
                     url_schemes={"https", "http", "mailto"},
                     link_rel="noopener noreferrer")


def markdown_to_html(text: str) -> str:
    """Markdown → HTML（表格、清單、換行），並移除可執行內容。"""
    import markdown as md

    rendered = md.markdown(
        text or "",
        extensions=["tables", "sane_lists", "nl2br"],
        output_format="html",
    )
    return scrub_html(rendered)
