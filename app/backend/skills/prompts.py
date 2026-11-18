"""
把 skill 的兩個小節包成可直接併入系統提示的區塊。

skill 正文是我們自己維護的檔案（不是使用者輸入），但仍用明確的標頭框起來，
讓模型分得清「平台規則」與「這一輪的分析方法」——兩者衝突時以平台規則
（安全規則、引用規範、不得編造數字）為準。
"""

from __future__ import annotations

from .registry import Skill


# 每個區塊都帶這句：skill 只能改「查什麼、怎麼寫」，不能放寬平台規則
PRIORITY_NOTE = (
    "此分析方法只規範資料範圍與報告結構；平台的安全規則、引用規範與「不得編造數字」"
    "仍然優先，兩者衝突時以平台規則為準。"
)


def _header(skill: Skill) -> str:
    return f"使用者以 {skill.token} 指定本輪採用「{skill.label}」分析方法。{PRIORITY_NOTE}"


def gather_block(skill: Skill) -> str:
    """給「決定要查什麼」的角色：股市 Agent 的 Router、深度研究的查證步驟。"""
    return (
        f"[本輪分析方法 - {skill.label}：資料蒐集清單]\n"
        f"{_header(skill)}以下是完成這個分析必須取得的資料；"
        "請依清單逐項檢索，清單以外的資料只在與題目直接相關時才查。\n\n"
        f"{skill.gather}"
    )


def output_block(skill: Skill) -> str:
    """給「決定怎麼寫」的角色：股市 Agent 的 Analyst、深度研究的報告輸出。"""
    return (
        f"[本輪分析方法 - {skill.label}：分析步驟與輸出格式]\n"
        f"{_header(skill)}請依下列結構撰寫回覆（以 `##`／`###` 作為小節標題），"
        "取代你原本的預設報告格式。\n"
        "資料不足以完成某一節時，保留該節標題並寫明缺了什麼資料，不要編造數字或略過不提。\n\n"
        f"{skill.output}"
    )


def full_block(skill: Skill) -> str:
    """深度研究用：同一個 Agent 既查資料又寫報告，兩段都要。"""
    return gather_block(skill) + "\n\n" + output_block(skill)


__all__ = ["gather_block", "output_block", "full_block"]
