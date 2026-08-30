"""
深度研究產出 skill：把研究結果轉成「研究報告」與「簡報」兩種可下載檔案。

每個 skill 由三件事組成：
1. 一段 skill prompt（規定寫作風格與資訊密度）
2. 一個結構化輸出 schema（讓模型只負責內容，不負責排版）
3. 一個決定性的 HTML renderer（`templates/`）

排版交給樣板而非模型，是為了讓輸出穩定好看 —— 模型只要漏一個結束標籤，
整份檔案就毀了；改成填 JSON 之後，最差情況也只是內容平庸而非版面破碎。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Tuple

from agents import Agent, ModelSettings, Runner
from pydantic import BaseModel, Field

from .config import resolve_length, supports_reasoning_effort
from .templates import Theme, render_deck, render_report, resolve_theme

MAX_SKILL_TURNS = 4


# ─────────────────────────────────────────────────────────────
# 研究報告
# ─────────────────────────────────────────────────────────────
class Reference(BaseModel):
    title: str = Field(description="來源標題")
    url: str = Field(description="來源網址；若為使用者上傳的文件則填檔名")


class KeyFigure(BaseModel):
    value: str = Field(
        description=(
            "**單一**數字，含單位，例如「38.2%」「NT$1,240 億」「+12%」。"
            "最多 14 個字元；不要放整組數列或區間說明，那些寫進 label 或內文"
        )
    )
    label: str = Field(description="這個數字代表什麼，一句話說完，二十字內")


class ReportSection(BaseModel):
    heading: str = Field(description="小節標題")
    body_markdown: str = Field(
        description=(
            "小節內文，Markdown 格式。可用清單、表格、粗體與 [標題](網址) 連結。"
            "每段至少三句，論斷後面要接依據。"
        )
    )
    callout: str = Field(
        description="這一節最該被記住的一句話；沒有就填空字串"
    )


class ReportDoc(BaseModel):
    title: str = Field(description="報告標題，具體點出研究對象與結論方向")
    subtitle: str = Field(description="副標，一句話說明這份報告回答了什麼")
    executive_summary: List[str] = Field(description="三到五條摘要，每條一句話講結論")
    key_figures: List[KeyFigure] = Field(description="二到四個關鍵數字；沒有就給空陣列")
    sections: List[ReportSection] = Field(
        description="報告小節；數量以 instructions 指定的為準"
    )
    open_questions: List[str] = Field(description="尚待釐清的問題；沒有就給空陣列")
    references: List[Reference] = Field(description="所有引用來源")


REPORT_SKILL_PROMPT = """你是一位替決策者撰寫研究報告的分析師。輸入是一份研究筆記，
你的工作是把它重組成一份結構完整、可以直接交出去的報告。

要求：
- 標題要具體。「台積電 2025 資本支出的三個變數」勝過「台積電研究報告」。
- 摘要每一條都是結論句，不是主題句。「毛利率壓力主要來自 N2 折舊而非匯率」
  勝過「本報告分析毛利率變化」。
- 內文保留研究筆記裡的所有數字、時間點與來源連結，不要為了精簡而丟掉證據。
- 研究筆記中互相矛盾的說法要保留，寫成獨立小節說明分歧與各自依據。
- 只使用研究筆記裡出現過的事實，不要補充你自己記憶中的資訊。
- 全文繁體中文（台灣用語）。
- key_figures 只放研究筆記中確實出現的數字；沒有明確數字就給空陣列，不要湊。
  每個 value 是**一個**數字（像儀表板上的大字），不是一整列數據。
  想呈現時間序列請在對應小節用 Markdown 表格。
- **sections 不要重複 executive_summary、open_questions、references 的內容**。
  報告已經有獨立的「摘要」「尚待釐清」「參考來源」區塊，
  再開一個同名小節會在成品裡出現兩次。
"""


# ─────────────────────────────────────────────────────────────
# 簡報
# ─────────────────────────────────────────────────────────────
class SlideStat(BaseModel):
    value: str = Field(description="數字本身，含單位")
    label: str = Field(description="這個數字的說明，十五字內")


class SlideColumn(BaseModel):
    heading: str = Field(description="這一欄的標題，例如被比較的對象名稱")
    bullets: List[str] = Field(description="這一欄的重點，二到四條")


class Slide(BaseModel):
    layout: Literal[
        "cover", "agenda", "section", "bullets", "stats", "compare", "quote", "closing"
    ] = Field(
        description=(
            "版面。第一頁必為 cover、最後一頁必為 closing；第二頁建議 agenda。"
            "stats=數字密集頁（填 stats）；compare=兩個對象並排比較（填 columns）；"
            "quote=只放一句話（放進 lead）；section=章節分隔頁，用來切開大段落"
        )
    )
    kicker: str = Field(description="頁面左上角的小標籤，四到八字")
    title: str = Field(description="頁面主標，越具體越好")
    lead: str = Field(description="標題底下的一句話補充；不需要就填空字串")
    bullets: List[str] = Field(
        description=(
            "條列重點，三到五條，每條 15–30 字且自成完整句子。"
            "stats 與 quote 版面請給空陣列"
        )
    )
    stats: List[SlideStat] = Field(
        description="僅 layout=stats 時填二到四個；其他版面給空陣列"
    )
    columns: List[SlideColumn] = Field(
        description="僅 layout=compare 時填**剛好兩欄**；其他版面給空陣列"
    )
    note: str = Field(description="這一頁的口說講稿，兩到四句，講的是投影片上沒寫的脈絡")


class DeckDoc(BaseModel):
    title: str = Field(description="簡報標題")
    subtitle: str = Field(description="副標，一句話")
    slides: List[Slide] = Field(
        description="投影片；頁數以 instructions 指定的為準，含封面與結語"
    )


DECK_SKILL_PROMPT = """你是一位替高階簡報設計內容的顧問。輸入是一份研究筆記，
你的工作是把它變成一份現場報告用的投影片。

要求：
- 一頁一個論點。頁標題就是那個論點本身，不是主題標籤：
  「N2 折舊壓縮毛利率 3 個百分點」勝過「毛利率分析」。
- 條列句要能單獨讀懂，不要只有名詞片語；但不要整段搬進投影片。
- 有數字的地方優先用 stats 版面，讓數字自己說話。
- 要比較兩個對象（公司、方案、時期）時用 compare，把兩邊各自的重點放進 columns，
  不要擠在同一份條列裡。
- 超過八頁時，用 section 分隔頁把簡報切成二到三個段落，讓聽眾知道走到哪了；
  八頁以內不需要分隔頁，把版位留給論點。
- 八頁以上的簡報至少要有一頁 stats、一頁 quote，讓節奏有變化；
  頁數更少時以論點優先，這兩種節奏頁可以省略。
- 每頁條列**最多五條**，每條 15–30 字。超過就拆頁 —— 塞太多字版面會擠。
- 講稿（note）寫投影片上「沒有寫」的東西：脈絡、佐證、可能被追問的點。
- 只使用研究筆記裡出現過的事實與數字。
- 全文繁體中文（台灣用語）。
"""


# ─────────────────────────────────────────────────────────────
# Skill 註冊表
# ─────────────────────────────────────────────────────────────
SKILLS: Dict[str, Dict[str, Any]] = {
    "report": {
        "label": "研究報告",
        "agent_name": "Report Writer",
        "prompt": REPORT_SKILL_PROMPT,
        "output_type": ReportDoc,
        "renderer": render_report,
        "extension": "html",
    },
    "deck": {
        "label": "簡報",
        "agent_name": "Deck Designer",
        "prompt": DECK_SKILL_PROMPT,
        "output_type": DeckDoc,
        "renderer": render_deck,
        "extension": "html",
    },
}


def _length_rule(kind: str, length: int) -> str:
    """
    把使用者指定的篇幅寫成一條 instructions 規則，接在 skill prompt 後面。

    只用 prompt 要求、不在後端裁切：截斷會砍掉有內容的頁，而結構化輸出對
    「剛好 N 個」的命中率夠好，偏一頁的成本遠低於硬砍一頁。
    """
    if kind == "deck":
        return (
            f"\n- 整份簡報**剛好 {length} 頁**，含封面與結語頁，不多也不少。"
            "\n  素材不夠時寧可把單頁講深，也不要為了湊頁數拆出只有一條重點的頁；"
            "\n  素材太多時砍掉次要論點，不要把兩個論點擠進同一頁。"
        )
    return (
        f"\n- 正文**剛好 {length} 個小節**，不多也不少"
        "（摘要、關鍵數字、尚待釐清、參考來源是固定區塊，不算在內）。"
        "\n  素材不夠時寧可把小節寫深，也不要拆出只有兩三句的空節。"
    )


def _safe_filename(kind: str, title: str, extension: str) -> str:
    """`<標題>_<產出類型>.<副檔名>`；報告與簡報同時下載時不會撞檔名。"""
    label = SKILLS[kind]["label"]
    # 不安全的字元換成空白（而非直接刪除），否則「A：B」會變成黏在一起的「AB」
    keep = "".join(
        ch if (ch.isalnum() or ch in "-_（）()[]、．.") else " "
        for ch in (title or "")
    )
    stem = "_".join(keep.split())[:48] or "深度研究"
    return f"{stem}_{label}.{extension}"


def _skill_input(query: str, research_markdown: str, citations: List[Dict[str, str]]) -> str:
    sources = (
        "\n".join(f"- [{c.get('title') or c['url']}]({c['url']})" for c in citations)
        or "（研究筆記內文已含引用連結）"
    )
    return (
        f"# 使用者的研究題目\n{query}\n\n"
        f"# 研究筆記\n{research_markdown}\n\n"
        f"# 已蒐集到的來源\n{sources}\n"
    )


async def generate_artifact(
    *,
    kind: str,
    model: str,
    query: str,
    research_markdown: str,
    citations: List[Dict[str, str]],
    theme: str | Theme | None = None,
    length: int | None = None,
) -> Tuple[str, str, str]:
    """
    跑一個 skill，回傳 `(filename, media_type, html)`。

    模型只產生結構化 JSON，HTML 一律由樣板組出來；`theme` 決定色票與字體配對，
    `length` 決定簡報頁數／報告小節數（`None` 用預設值，超出範圍會被 clamp）。
    """
    skill = SKILLS[kind]
    size = resolve_length(kind, length)

    agent = Agent(
        name=skill["agent_name"],
        instructions=skill["prompt"] + _length_rule(kind, size),
        model=model,
        output_type=skill["output_type"],
        model_settings=(
            ModelSettings(reasoning={"effort": "low"})
            if supports_reasoning_effort(model)
            else ModelSettings()
        ),
    )

    result = await Runner.run(
        agent,
        input=_skill_input(query, research_markdown, citations),
        max_turns=MAX_SKILL_TURNS,
    )

    doc = result.final_output
    if isinstance(doc, str):
        # 極少數情況模型會回純文字；試著當成 JSON 救回來
        doc = skill["output_type"].model_validate(json.loads(doc))

    chosen = theme if isinstance(theme, Theme) else resolve_theme(theme)
    html = skill["renderer"](doc, model=model, query=query, theme=chosen)
    filename = _safe_filename(kind, getattr(doc, "title", ""), skill["extension"])
    return filename, "text/html; charset=utf-8", html
