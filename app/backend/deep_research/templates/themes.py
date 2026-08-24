"""
產出檔案的視覺主題。

一個主題 = 色票 + 字體配對 + 幾個排版取向（參考 Anthropic theme-factory 的作法：
把「風格」收斂成少數具名預設，而不是讓模型每次自由發揮 —— 自由發揮的結果
是每份檔案長得都不一樣，而且通常都不好看）。

字體只用系統堆疊。產出的 HTML 會被下載到本機離線開啟，
連 Google Fonts 都不能拉，否則使用者在沒網路的會議室裡就是一份沒有字體的檔案。

襯線／無襯線在中文環境是真實的視覺差異（明體 vs 黑體），
所以字體配對是有意義的主題維度，不是裝飾。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

# 中文字體堆疊：先系統原生，再退回思源，最後 generic
_SANS = (
    '-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang TC",'
    '"Noto Sans TC","Microsoft JhengHei",sans-serif'
)
_SERIF = (
    'Georgia,"Times New Roman","Songti TC","Noto Serif TC",'
    '"PMingLiU",serif'
)
_MONO = '"SFMono-Regular",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace'


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    description: str
    tokens: Dict[str, str]
    # 供樣板做細部分支（不是每個主題都適合同一種裝飾）
    traits: Dict[str, Any] = field(default_factory=dict)

    def css_vars(self) -> str:
        base = dict(self.tokens)
        base.setdefault("font-mono", _MONO)
        return "".join(f"--{k}:{v};" for k, v in base.items())

    @property
    def is_dark(self) -> bool:
        return bool(self.traits.get("dark"))


THEMES: Dict[str, Theme] = {
    # ── 1. 編輯部：襯線標題 + 紙感底 ────────────────────────────
    "editorial": Theme(
        id="editorial",
        label="編輯部",
        description="襯線標題配紙感米白底，像一份被仔細編過的長篇報導",
        tokens={
            "bg": "#fdfcfa",
            "bg-soft": "#f5f2ec",
            "ink": "#1a1714",
            "ink2": "#3d372f",
            "body": "#5c554b",
            "muted": "#948b7e",
            "line": "#e4ded3",
            "line-strong": "#cfc6b6",
            "accent": "#9c3d24",
            "accent-soft": "#fbf1ed",
            "accent-line": "#f0d9d0",
            "font-display": _SERIF,
            "font-body": _SANS,
            "radius": "4px",
        },
        traits={"heading_weight": "700", "rule": "double", "kicker": "underline"},
    ),
    # ── 2. 顧問簡報：深藍、資訊密度高 ───────────────────────────
    "consulting": Theme(
        id="consulting",
        label="顧問簡報",
        description="深藍配無襯線，資訊密度高，適合對主管或客戶報告",
        tokens={
            "bg": "#ffffff",
            "bg-soft": "#f4f6fa",
            "ink": "#0d1b33",
            "ink2": "#22314d",
            "body": "#4e5a70",
            "muted": "#8590a5",
            "line": "#dfe4ed",
            "line-strong": "#c3ccdb",
            "accent": "#1b4fd8",
            "accent-soft": "#eaf0ff",
            "accent-line": "#cfdcff",
            "font-display": _SANS,
            "font-body": _SANS,
            "radius": "8px",
        },
        traits={"heading_weight": "800", "rule": "bar", "kicker": "pill"},
    ),
    # ── 3. 暗夜：深色底、螢幕優先 ───────────────────────────────
    "midnight": Theme(
        id="midnight",
        label="暗夜",
        description="深色底配青綠強調，投影機與螢幕上最不刺眼",
        tokens={
            "bg": "#0e1116",
            "bg-soft": "#161b23",
            "ink": "#f2f5f9",
            "ink2": "#dce3ec",
            "body": "#a8b3c2",
            "muted": "#6f7c8d",
            "line": "#242c37",
            "line-strong": "#39434f",
            "accent": "#3ddc97",
            "accent-soft": "#13251f",
            "accent-line": "#1f4436",
            "font-display": _SANS,
            "font-body": _SANS,
            "radius": "10px",
        },
        traits={"dark": True, "heading_weight": "700", "rule": "bar", "kicker": "pill"},
    ),
    # ── 4. 極簡：留白與細線 ─────────────────────────────────────
    "minimal": Theme(
        id="minimal",
        label="極簡",
        description="大量留白、細線與單一墨色，讓內容自己說話",
        tokens={
            "bg": "#ffffff",
            "bg-soft": "#fafafa",
            "ink": "#111111",
            "ink2": "#2e2e2e",
            "body": "#5f5f5f",
            "muted": "#9a9a9a",
            "line": "#ebebeb",
            "line-strong": "#d4d4d4",
            "accent": "#111111",
            "accent-soft": "#f4f4f4",
            "accent-line": "#e0e0e0",
            "font-display": _SANS,
            "font-body": _SANS,
            "radius": "0px",
        },
        traits={"heading_weight": "600", "rule": "hairline", "kicker": "plain"},
    ),
    # ── 5. 暖刊：雜誌感 ─────────────────────────────────────────
    "warm": Theme(
        id="warm",
        label="暖刊",
        description="暖橘與襯線標題，雜誌內頁的節奏，適合對外分享",
        tokens={
            "bg": "#fffdf9",
            "bg-soft": "#fbf4e9",
            "ink": "#231a12",
            "ink2": "#453425",
            "body": "#65543f",
            "muted": "#9e8b73",
            "line": "#ece0cd",
            "line-strong": "#d8c6a9",
            "accent": "#c4661a",
            "accent-soft": "#fdf0e2",
            "accent-line": "#f3ddc2",
            "font-display": _SERIF,
            "font-body": _SANS,
            "radius": "12px",
        },
        traits={"heading_weight": "700", "rule": "bar", "kicker": "pill"},
    ),
}

DEFAULT_THEME = "consulting"


def resolve_theme(theme_id: str | None) -> Theme:
    """前端送來的 theme 一律經過這裡；不認得就退回預設。"""
    return THEMES.get((theme_id or "").strip(), THEMES[DEFAULT_THEME])


def public_themes() -> List[Dict[str, str]]:
    """給前端渲染風格選單。"""
    return [
        {
            "id": t.id,
            "label": t.label,
            "description": t.description,
            "swatch": t.tokens["accent"],
            "surface": t.tokens["bg"],
            "ink": t.tokens["ink"],
        }
        for t in THEMES.values()
    ]
