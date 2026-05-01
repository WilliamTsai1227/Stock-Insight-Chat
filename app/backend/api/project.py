"""
Project API (專案管理接口)
========================
使用 asyncpg 原生連線操作 PostgreSQL。

asyncpg 語法注意事項：
- 參數佔位符：$1, $2, $3...（位置參數）
- 取單列：await db.fetchrow(sql, *args)
- 取單值：await db.fetchval(sql, *args)
- 寫入/更新/刪除：await db.execute(sql, *args)

外鍵約束說明：
- projects.user_id 參考 users(id)
- 若 user_id 不存在，PostgreSQL 會拋出 ForeignKeyViolationError
- 後端統一捕獲並回傳 HTTP 404，附帶明確錯誤訊息給前端
"""

import re
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime, timezone
from asyncpg.exceptions import ForeignKeyViolationError

from app.backend.database.postgresql import get_db

router = APIRouter(tags=["Project"])


# ── Project Name 白名單正則 ────────────────────────────────────────────────────
# 允許：
#   \w              → ASCII 字母、數字、底線
#   \u4e00-\u9fff  → 基本 CJK 漢字
#   \u3400-\u4dbf  → CJK 擴展 A（罕用漢字）
#   \u3040-\u309f  → 平假名
#   \u30a0-\u30ff  → 片假名
#   \uff00-\uffef  → 全形字符（全形英數、括號等）
#   \u00c0-\u024f  → 拉丁擴展（含重音字母，如 é à ü）
#   \s             → 空白（含半形空格、Tab）
#   \-_.           → 常用分隔符
#   ()（）【】「」  → 括號（ASCII + 全形）
#
# 禁止（不在白名單內）：
#   < > ' " ` ; = / \ & % $ # @ ! ^ * + | ~ , ？ 等危險字符
#   → 防止 XSS、HTML Injection、SQL 符號注入、Shell 注入
# ─────────────────────────────────────────────────────────────────────────────
_VALID_NAME_PATTERN = re.compile(
    r'^[\w'
    r'\u4e00-\u9fff'
    r'\u3400-\u4dbf'
    r'\u3040-\u309f'
    r'\u30a0-\u30ff'
    r'\uff00-\uffef'
    r'\u00c0-\u024f'
    r'\s\-_.()（）【】「」『』·'
    r']+$',
    re.UNICODE
)

_NAME_MIN_LEN = 1
_NAME_MAX_LEN = 100


def _validate_name(name: str) -> str:
    """
    驗證 project name 合法性，回傳去頭尾空白後的乾淨字串。
    驗證失敗時拋出 HTTPException 422。
    """
    stripped = name.strip()

    if len(stripped) < _NAME_MIN_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Project name cannot be empty or contain only whitespace."
        )

    if len(stripped) > _NAME_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Project name must not exceed {_NAME_MAX_LEN} characters (current: {len(stripped)})."
        )

    if not _VALID_NAME_PATTERN.match(stripped):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Project name contains illegal characters. "
                "Only letters, digits, CJK characters, spaces, "
                "hyphens (-), underscores (_), dots (.), and common brackets are allowed. "
                "Characters such as < > ' \" ; / \\ & $ ` are forbidden."
            )
        )

    return stripped


# --- Request Schema ---

class CreateProjectRequest(BaseModel):
    name: str
    user_id: UUID


# --- API Endpoint ---

@router.post("/api/project", status_code=status.HTTP_201_CREATED)
async def create_project(
    request: CreateProjectRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    建立新專案。

    前端必要欄位：
    - name     (str)  : 專案名稱，由後端正則過濾非法字串
    - user_id  (UUID) : 所屬使用者 ID

    後端自動帶入：
    - created_at : UTC+0 標準時間，前端不需傳入

    錯誤回傳：
    - 422 : name 包含非法字符、過長或為空
    - 404 : user_id 不存在於 users 表（PostgreSQL FK 違反）
    - 500 : 其他未預期的資料庫錯誤
    """

    # 1. 正則過濾 name
    clean_name = _validate_name(request.name)

    # 2. 由後端注入 UTC+0 建立時間
    now_utc = datetime.now(timezone.utc)

    try:
        row = await db.fetchrow(
            """
            INSERT INTO projects (name, user_id, created_at)
            VALUES ($1, $2, $3)
            RETURNING id, name, user_id, created_at
            """,
            clean_name,
            request.user_id,
            now_utc
        )

    except ForeignKeyViolationError:
        # PostgreSQL FK 約束：user_id 不存在於 users 表時觸發
        # asyncpg.exceptions.ForeignKeyViolationError
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with id '{request.user_id}' does not exist."
        )

    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}"
        )

    return {
        "status": "success",
        "data": {
            "id": str(row["id"]),
            "name": row["name"],
            "user_id": str(row["user_id"]),
            "created_at": row["created_at"].isoformat()
        }
    }
