"""
Office 產出共用層：字型、色票、Markdown 解析。

docx / pptx 與 HTML 樣板吃的是同一個結構化物件（`ReportDoc` / `DeckDoc`），
差別只在輸出的容器 —— 所以這裡不做任何「HTML 轉檔」，只提供兩種 Office
renderer 都要用的基礎工具。

python-docx / python-pptx 一律在 renderer 函式內部才 import：它們會連帶拉進
lxml 與 Pillow，放在模組頂層等於每個 worker 常駐多吃十幾 MB，而這兩個套件
只有在使用者真的按下載時才會用到（backend 的 mem_limit 只有 640m）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .themes import Theme

# ─────────────────────────────────────────────────────────────
# 字型
# ─────────────────────────────────────────────────────────────
# Office 檔案不吃 CSS font stack：docx/pptx 只能指定單一字型名，而且中文與
# 英文要分開設（latin / eastAsia）—— 只設 latin 的話 Word 會拿英文字型去湊
# 中文字，字重與字寬都會跑掉。
#
# 選的是 Windows Office 內建、macOS Office 缺了也能優雅退回的字。
OFFICE_FONTS: Dict[str, Tuple[str, str]] = {
    # 類別 → (latin, eastAsia)
    "sans": ("Segoe UI", "Microsoft JhengHei"),
    "serif": ("Georgia", "PMingLiU"),
}

# themes.py 的襯線堆疊以 Georgia 開頭；用開頭字串判斷即可分辨兩種字體配對
_SERIF_HEAD = "Georgia"


def office_font(theme: Theme, role: str = "body") -> Tuple[str, str]:
    """`role="display"` 取標題字、`"body"` 取內文字，回傳 `(latin, eastAsia)`。"""
    stack = theme.tokens.get(f"font-{role}", "")
    return OFFICE_FONTS["serif" if stack.startswith(_SERIF_HEAD) else "sans"]


# ─────────────────────────────────────────────────────────────
# 色票
# ─────────────────────────────────────────────────────────────
def hex_rgb(value: str) -> Tuple[int, int, int]:
    """`#1b4fd8` → `(27, 79, 216)`；解析不出來就當黑色，不要讓產出直接爆掉。"""
    raw = (value or "").lstrip("#").strip()
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) != 6:
        return (0, 0, 0)
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _luminance(rgb: Tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def darken(color: str, target: float = 0.42) -> str:
    """
    把顏色壓到在白紙上讀得清楚的亮度。

    「暗夜」主題的強調色是螢光綠（#3ddc97），在深色投影片上很好看，
    直接放到 Word 的白紙上則幾乎看不見。
    """
    rgb = list(hex_rgb(color))
    for _ in range(12):
        if _luminance((rgb[0], rgb[1], rgb[2])) <= target:
            break
        rgb = [max(0, int(c * 0.82)) for c in rgb]
    return "#%02x%02x%02x" % (rgb[0], rgb[1], rgb[2])


# Word 的紙張一律淺色：文件會被列印，而 Word 預設不印背景色 ——
# 深色主題若照搬過去，結果是白紙配一片幾乎看不見的淺色字。
_PAPER = {
    "bg": "#ffffff",
    "bg-soft": "#f5f6f8",
    "ink": "#14171c",
    "ink2": "#2b303a",
    "body": "#3f4753",
    "muted": "#7b8492",
    "line": "#e2e5ea",
    "line-strong": "#c9cfd8",
}


def paper_palette(theme: Theme) -> Dict[str, str]:
    """docx 用的色票：暗色主題換成淺色紙張，只留下它的強調色（壓暗到可讀）。"""
    palette = dict(theme.tokens)
    if not theme.is_dark:
        return palette
    palette.update(_PAPER)
    accent = darken(theme.tokens.get("accent", "#1b4fd8"))
    palette["accent"] = accent
    palette["accent-soft"] = "#f2f5ff"
    palette["accent-line"] = "#d8e2ff"
    return palette


# ─────────────────────────────────────────────────────────────
# Markdown → 區塊
# ─────────────────────────────────────────────────────────────
@dataclass
class Span:
    """一段格式一致的文字。"""

    text: str
    bold: bool = False
    code: bool = False
    link: str = ""


@dataclass
class Block:
    """一個段落層級的區塊。"""

    kind: str                                     # para | bullet | ordered | heading | table
    spans: List[Span] = field(default_factory=list)
    level: int = 0                                # heading 階層，或清單縮排層級
    rows: List[List[List[Span]]] = field(default_factory=list)   # 僅 table 使用


_INLINE_RE = re.compile(
    r"\*\*(?P<bold>.+?)\*\*"
    r"|`(?P<code>[^`]+)`"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^)\s]+)\)"
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
# 表格分隔列：|---|:--:|---|
_TABLE_SEP_RE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")


def parse_inline(text: str) -> List[Span]:
    """
    解析行內語法：`**粗體**`、`` `行內碼` ``、`[文字](網址)`。

    刻意不支援巢狀（連結文字裡的粗體會原樣保留）—— 巢狀要處理就得寫真正的
    parser，而 skill prompt 產出的內文不會用到。
    """
    source = text or ""
    spans: List[Span] = []
    pos = 0

    for match in _INLINE_RE.finditer(source):
        if match.start() > pos:
            spans.append(Span(source[pos:match.start()]))
        if match.group("bold") is not None:
            spans.append(Span(match.group("bold"), bold=True))
        elif match.group("code") is not None:
            spans.append(Span(match.group("code"), code=True))
        else:
            spans.append(Span(match.group("link_text"), link=match.group("link_url")))
        pos = match.end()

    if pos < len(source):
        spans.append(Span(source[pos:]))

    kept = [s for s in spans if s.text]
    return kept or [Span("")]


def _split_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _consume_table(lines: List[str], start: int) -> Tuple[List[List[List[Span]]], int]:
    """從標頭列開始吃掉整張表，回傳 `(rows, 下一行的 index)`。"""
    rows = [[parse_inline(cell) for cell in _split_row(lines[start])]]
    index = start + 2                              # 跳過標頭列與分隔列
    while index < len(lines) and lines[index].strip() and "|" in lines[index]:
        rows.append([parse_inline(cell) for cell in _split_row(lines[index])])
        index += 1

    # 模型偶爾會漏掉某一列的最後一個分隔線，補齊才不會在建表時 index 爆掉
    width = max(len(row) for row in rows)
    for row in rows:
        while len(row) < width:
            row.append([Span("")])
    return rows, index


def parse_markdown(text: str) -> List[Block]:
    """
    把小節內文的 Markdown 拆成 Office renderer 排得出來的區塊。

    支援段落、標題、有序／無序清單、表格，以及行內的粗體／行內碼／連結 ——
    也就是 skill prompt 實際會要求模型產出的東西。程式碼區塊、引言、巢狀清單
    會退化成普通段落：寧可少一點格式，也不要為了涵蓋全部語法把這裡養成
    半個 Markdown 實作（HTML 那條路已經有 `markdown` 套件負責了）。
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    blocks: List[Block] = []
    buffer: List[str] = []

    def flush() -> None:
        if buffer:
            blocks.append(Block("para", parse_inline(" ".join(buffer).strip())))
            buffer.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        # 表格必須是「標頭列 + 分隔列」才算，否則一句夾了 | 的普通句子會被誤判
        if "|" in stripped and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1]):
            flush()
            rows, index = _consume_table(lines, index)
            blocks.append(Block("table", rows=rows))
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush()
            blocks.append(
                Block("heading", parse_inline(heading.group(2)), level=len(heading.group(1)))
            )
            index += 1
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush()
            blocks.append(
                Block("bullet", parse_inline(bullet.group(2)), level=len(bullet.group(1)) // 2)
            )
            index += 1
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            flush()
            blocks.append(
                Block("ordered", parse_inline(ordered.group(2)), level=len(ordered.group(1)) // 2)
            )
            index += 1
            continue

        buffer.append(stripped)
        index += 1

    flush()
    return blocks


def plain_text(spans: List[Span]) -> str:
    """把一串 span 攤平成純文字（pptx 的講稿、頁尾等不需要格式的地方用）。"""
    return "".join(s.text for s in spans)
