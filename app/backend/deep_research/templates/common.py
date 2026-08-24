"""樣板共用：轉義、Markdown 轉換、色票。"""

from __future__ import annotations

import re
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

# LLM 產出的 Markdown 允許夾帶 HTML，這裡把可執行的部分拔掉。
# 產出的檔案會被使用者下載後用瀏覽器開啟（file:// 同源），不能留下可執行入口。
_DANGEROUS_TAGS = re.compile(
    r"</?\s*(script|iframe|object|embed|form|link|meta|base|style)\b[^>]*>",
    re.IGNORECASE,
)
_EVENT_ATTRS = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URLS = re.compile(r"(href|src)\s*=\s*([\"']?)\s*javascript:", re.IGNORECASE)


def esc(value: Any) -> str:
    """所有進入樣板的 LLM 產出都必須經過這裡。"""
    return _html_escape(str(value or ""), quote=True)


def scrub_html(html: str) -> str:
    html = _DANGEROUS_TAGS.sub("", html)
    html = _EVENT_ATTRS.sub("", html)
    html = _JS_URLS.sub(r"\1=\2#", html)
    return html


def markdown_to_html(text: str) -> str:
    """Markdown → HTML（表格、清單、換行），並移除可執行內容。"""
    import markdown as md

    rendered = md.markdown(
        text or "",
        extensions=["tables", "sane_lists", "nl2br"],
        output_format="html",
    )
    return scrub_html(rendered)
