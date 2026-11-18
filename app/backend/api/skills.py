"""
金融分析 skill 目錄（`GET /api/skills`）
=======================================
前端的 @ 選單以這份清單顯示可點名的分析方法（@dcf、@財報解讀…）。
只回傳顯示用欄位；skill 的 prompt 正文留在後端。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.backend.module.jwt import get_current_user_id
from app.backend.skills.registry import list_skills

router = APIRouter(tags=["Skills"])

# 前端用來判斷哪些模式可以 @ skill（與 api/chat.py、api/chat_deep_research.py 的驗證一致）
SUPPORTED_MODES = ["stock_agent", "deep_research"]


@router.get("/api/skills")
async def get_skills(_: UUID = Depends(get_current_user_id)):
    return {
        "status": "success",
        "data": {
            "skills": [s.public() for s in list_skills()],
            "supported_modes": SUPPORTED_MODES,
        },
    }


__all__ = ["router"]
