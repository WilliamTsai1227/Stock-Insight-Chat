"""金融分析 skill：註冊表、prompt 組裝、股市 Agent 的工具替換與深度查證名額、API 驗證。"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from app.backend.skills.registry import (
    SkillFormatError,
    get_skill,
    list_skills,
    load_library,
    parse_skill,
)

_VALID = """---
id: demo
mention: 示範
label: 示範分析
description: 測試用
source: original
---

# 標題

## 資料蒐集

查 A 與 B。

### 子標題屬於上一節

細節。

## 分析與輸出

寫成表格。
"""


# ─── 註冊表 ───────────────────────────────────────────────
def test_library_has_the_five_finance_skills_in_menu_order():
    assert [s.id for s in list_skills()] == ["dcf", "comps", "earnings", "chips", "sector"]
    assert [s.token for s in list_skills()] == ["@dcf", "@comps", "@財報解讀", "@籌碼分析", "@產業比較"]


def test_every_adapted_skill_cites_a_pinned_upstream_and_license_is_kept():
    from pathlib import Path

    pinned = "https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/"
    for skill in list_skills():
        if skill.source == "original":
            continue
        for src in skill.source.split(";"):
            assert src.strip().startswith(pinned), src

    # Apache 2.0 第 4(a) 條：散布衍生作品須附授權全文
    license_file = Path(__file__).resolve().parents[2] / "third_party/anthropic-financial-services/LICENSE"
    assert "Apache License" in license_file.read_text(encoding="utf-8")


def test_parse_splits_sections_and_keeps_subheadings():
    skill = parse_skill(_VALID)
    assert skill.gather.startswith("查 A 與 B")
    assert "### 子標題屬於上一節" in skill.gather
    assert skill.output == "寫成表格。"
    assert skill.public() == {"id": "demo", "mention": "示範", "label": "示範分析", "description": "測試用"}


@pytest.mark.parametrize(
    "broken",
    [
        _VALID.replace("## 分析與輸出", "## 其他"),            # 缺必要小節
        _VALID.replace("mention: 示範", "mention: 有 空白"),     # mention 含空白
        _VALID.replace("id: demo", "id: Demo!"),                # id 格式
        _VALID.replace("label: 示範分析\n", ""),                # 缺欄位
        _VALID.split("---\n", 2)[2],                            # 沒有 frontmatter
    ],
)
def test_parse_rejects_malformed_files(broken):
    with pytest.raises(SkillFormatError):
        parse_skill(broken)


def test_library_rejects_duplicate_mentions_and_folder_mismatch(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "SKILL.md").write_text(_VALID, encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "SKILL.md").write_text(_VALID.replace("id: demo", "id: other"), encoding="utf-8")
    with pytest.raises(SkillFormatError, match="重複"):
        load_library(tmp_path)

    (tmp_path / "other" / "SKILL.md").unlink()
    (tmp_path / "wrong").mkdir()
    (tmp_path / "wrong" / "SKILL.md").write_text(
        _VALID.replace("id: demo", "id: nope").replace("mention: 示範", "mention: x"), encoding="utf-8"
    )
    with pytest.raises(SkillFormatError, match="資料夾"):
        load_library(tmp_path)


# ─── 深度研究 ─────────────────────────────────────────────
def test_research_instructions_swap_output_format_but_keep_safety():
    from app.backend.deep_research.researcher import RESEARCH_INSTRUCTIONS, research_instructions

    assert research_instructions(None) == RESEARCH_INSTRUCTIONS
    assert "## 摘要" in RESEARCH_INSTRUCTIONS

    text = research_instructions(get_skill("dcf"))
    assert "## 交叉比對與分歧" not in text            # 預設格式被取代
    assert "敏感度分析" in text and "台灣 10 年期公債" in text
    assert "file_search" in text                       # 研究方法（含附件）保留
    assert "不是給你的指令" in text                    # 安全規則保留
    assert text.index("安全規則（優先於上面所有指示") > text.index("敏感度分析")   # 安全規則在最後、優先


# ─── 股市 Agent ───────────────────────────────────────────
def test_skill_mode_swaps_tavily_for_deep_web_search():
    from app.backend.agent.chat import _skill_tool_names

    assert _skill_tool_names(["search_stock_news", "tavily_global_search", "mcp__s__t"]) == [
        "search_stock_news", "deep_web_search", "mcp__s__t",
    ]
    # 使用者沒勾網路搜尋 → skill 模式也不偷偷打開
    assert _skill_tool_names(["search_stock_news"]) == ["search_stock_news"]


def test_router_prompt_switches_web_guidance():
    from app.backend.agent.prompts import build_router_system_prompt

    normal = build_router_system_prompt("now", ["search_stock_news", "tavily_global_search"])
    skill = build_router_system_prompt("now", ["search_stock_news", "deep_web_search"])
    assert "預設優先呼叫 `tavily_global_search`" in normal and "deep_web_search" not in normal
    assert "deep_web_search" in skill and "預設優先呼叫 `tavily_global_search`" not in skill


async def test_call_tools_caps_deep_searches_per_turn(monkeypatch):
    from app.backend.agent import chat as agent_chat
    from app.backend.deep_research import web_lookup

    calls = []

    async def fake_lookup(query, **_):
        calls.append(query)
        return web_lookup.LookupResult(
            text=f"- {query} 的事實（2026Q3）—— [來源](https://example.com/{len(calls)})",
            citations=[{"title": "來源", "url": f"https://example.com/{len(calls)}"}],
        )

    monkeypatch.setattr(web_lookup, "deep_web_lookup", fake_lookup)
    monkeypatch.setattr(agent_chat, "DEEP_SEARCH_MAX_CALLS_PER_TURN", 2)

    msg = AIMessage(content="", tool_calls=[
        {"name": "deep_web_search", "args": {"query": f"q{i}"}, "id": f"c{i}"} for i in range(3)
    ])
    state = {"messages": [msg], "deep_search_calls": 1, "web_search_calls": 0}
    out = await agent_chat.call_tools(state)

    assert calls == ["q0"]                              # 剩 1 個名額：只執行第一個
    assert out["deep_search_calls"] == 2
    contents = [m.content for m in out["messages"]]
    assert "q0 的事實" in contents[0]
    assert all("已達上限" in c for c in contents[1:])
    assert out["retrieved_data"][0]["citations"][0]["url"] == "https://example.com/1"


async def test_call_tools_reports_lookup_failure_without_raising(monkeypatch):
    from app.backend.agent import chat as agent_chat
    from app.backend.deep_research import web_lookup

    async def boom(query, **_):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(web_lookup, "deep_web_lookup", boom)
    msg = AIMessage(content="", tool_calls=[{"name": "deep_web_search", "args": {"query": "x"}, "id": "c0"}])
    out = await agent_chat.call_tools({"messages": [msg]})
    assert "深度查證失敗" in out["messages"][0].content
    assert out["retrieved_data"] == []


async def test_lookup_usage_is_collected_in_scope(monkeypatch):
    from app.backend.deep_research import web_lookup

    class FakeResult:
        final_output = "- 事實"
        new_items = []
        context_wrapper = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=20, requests=2))

        async def stream_events(self):
            if False:
                yield None

    monkeypatch.setattr(web_lookup.Runner, "run_streamed", lambda *a, **k: FakeResult())
    with web_lookup.lookup_usage_scope() as collector:
        await web_lookup.deep_web_lookup("a")
        await web_lookup.deep_web_lookup("b")
    assert collector.calls == 2
    assert collector.usage.prompt_tokens == 200 and collector.usage.completion_tokens == 40


# ─── API ─────────────────────────────────────────────────
def _request(**kw):
    from app.backend.api.chat import MessageRequest

    return MessageRequest(query="台積電", chat_id=uuid.uuid4(), **kw)


def test_request_skill_validation():
    from app.backend.api.chat import _resolve_request_skill

    assert _resolve_request_skill(_request()) is None
    assert _resolve_request_skill(_request(skill="dcf", chat_mode="stock_agent")).id == "dcf"
    with pytest.raises(HTTPException) as unknown:
        _resolve_request_skill(_request(skill="nope", chat_mode="stock_agent"))
    assert unknown.value.status_code == 422
    with pytest.raises(HTTPException) as general:
        _resolve_request_skill(_request(skill="dcf", chat_mode="general"))
    assert general.value.status_code == 422


def test_retrieval_sources_expand_deep_lookup_citations():
    from app.backend.api.chat import _retrieval_sources_from

    out = _retrieval_sources_from([
        {"source_tool": "web", "title": "深度查證：x", "content": "...", "citations": [
            {"title": "A", "url": "https://a"}, {"title": "", "url": "https://b"}, {"title": "no url"},
        ]},
        {"source_tool": "news", "title": "新聞", "content": None},
    ])
    assert [(s["title"], s["url"]) for s in out[:2]] == [("A", "https://a"), ("https://b", "https://b")]
    assert out[2]["title"] == "新聞" and out[2]["content_preview"] == "..."


async def test_skills_endpoint_lists_public_fields_only():
    from app.backend.api.skills import get_skills

    body = await get_skills(uuid.uuid4())
    skills = body["data"]["skills"]
    assert {s["id"] for s in skills} == {"dcf", "comps", "earnings", "chips", "sector"}
    assert all(set(s) == {"id", "mention", "label", "description"} for s in skills)
    assert body["data"]["supported_modes"] == ["stock_agent", "deep_research"]
