"""
Project API (專案管理接口)
========================
使用 asyncpg 原生連線操作 PostgreSQL。

asyncpg 語法注意事項：
- 參數佔位符：$1, $2, $3...（位置參數）
- 取單列：await db.fetchrow(sql, *args)
- 取單值：await db.fetchval(sql, *args)
- 寫入/更新/刪除：await db.execute(sql, *args)

安全設計：
- 所有端點均需通過 JWT Access Token 驗證（get_current_user）
- user_id 從驗證後的 JWT 中取得，前端不需也不應傳遞
- project name 經白名單正則過濾，防止 XSS / Injection
"""

import re
import asyncpg
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from datetime import datetime, timezone
from asyncpg.exceptions import ForeignKeyViolationError

from app.backend.database.postgresql import get_db
from app.backend.module.jwt import get_current_user

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
#   < > ' " ` ; = / \ & % $ # @ ! ^ * + | ~ 等危險字符
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
    # user_id 由後端從 JWT 取得，前端無需傳遞


# --- API Endpoint ---

@router.post("/api/project", status_code=status.HTTP_201_CREATED)
async def create_project(
    request: CreateProjectRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    建立新專案。（需登入）

    前端必要欄位：
    - name (str) : 專案名稱，由後端正則過濾非法字串

    後端自動帶入：
    - user_id    : 從 JWT Access Token 解析，前端無需傳遞
    - created_at : UTC+0 標準時間

    HTTP 回應：
    - 201 Created  : 建立成功，回傳專案資料
    - 401          : JWT 驗證失敗或 Token 已過期
    - 403          : 帳號已停用
    - 422          : name 包含非法字符、過長或為空
    - 500          : 其他未預期的資料庫錯誤
    """

    # 1. 正則過濾 name
    clean_name = _validate_name(request.name)

    # 2. user_id 從已驗證的 JWT 取得（無需信任前端傳入）
    user_id = current_user["id"]

    # 3. 由後端注入 UTC+0 建立時間
    now_utc = datetime.now(timezone.utc)

    try:
        row = await db.fetchrow(
            """
            INSERT INTO projects (name, user_id, created_at)
            VALUES ($1, $2, $3)
            RETURNING id, name, user_id, created_at
            """,
            clean_name,
            user_id,
            now_utc,
        )

    except ForeignKeyViolationError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected referential integrity error. Please contact support."
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


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/project/all
# ─────────────────────────────────────────────────────────────────────────────
# 注意：必須註冊在 GET /api/project 之前，避免 FastAPI 路由解析歧義
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/project/all")
async def list_all_projects(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    讀取目前登入使用者建立的所有專案。

    安全設計：
    - user_id 由 JWT 取得，前端不需也不應傳遞
    - SQL 以 user_id 過濾，確保只回傳本人擁有的資料

    HTTP 回應：
    - 200 OK   : 回傳專案陣列（可能為空）
    - 401      : JWT 驗證失敗或 Token 已過期
    - 403      : 帳號已停用
    - 500      : 其他未預期的資料庫錯誤
    """
    user_id = current_user["id"]

    try:
        rows = await db.fetch(
            """
            SELECT id, name, created_at
            FROM projects
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}"
        )

    return {
        "status": "success",
        "data": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/project
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/project")
async def get_project_detail(
    project_id: UUID = Query(..., description="要查詢的 project id"),
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    讀取指定專案的詳細資訊，包含其底下的 chats 與 files 列表。

    回傳結構：
    - project: id / name / created_at
    - chats[]: id / title
    - files[]: id / file_name / s3_url / file_type / status / created_at

    安全設計：
    - 同時以 (id, user_id) 過濾，確保使用者無法讀取他人的 project
    - 找不到（不存在或不屬於本人）一律回 404，不洩漏資源是否存在

    HTTP 回應：
    - 200 OK   : 回傳專案詳細資料
    - 401      : JWT 驗證失敗或 Token 已過期
    - 403      : 帳號已停用
    - 404      : 找不到專案或無權存取
    - 500      : 其他未預期的資料庫錯誤
    """
    user_id = current_user["id"]

    try:
        # 1. 查專案本身（同時驗證 ownership）
        project_row = await db.fetchrow(
            """
            SELECT id, name, created_at
            FROM projects
            WHERE id = $1 AND user_id = $2
            """,
            project_id,
            user_id,
        )

        if project_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found or you don't have permission to access it."
            )

        # 2. 查關聯的 chats
        chat_rows = await db.fetch(
            """
            SELECT id, title
            FROM chats
            WHERE project_id = $1
            ORDER BY created_at DESC
            """,
            project_id,
        )

        # 3. 查關聯的 files
        file_rows = await db.fetch(
            """
            SELECT id, file_name, s3_url, file_type, status, created_at
            FROM files
            WHERE project_id = $1
            ORDER BY created_at DESC
            """,
            project_id,
        )

    except HTTPException:
        raise
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}"
        )

    return {
        "status": "success",
        "data": {
            "id": str(project_row["id"]),
            "name": project_row["name"],
            "created_at": project_row["created_at"].isoformat(),
            "chats": [
                {
                    "id": str(c["id"]),
                    "title": c["title"],
                }
                for c in chat_rows
            ],
            "files": [
                {
                    "id": str(f["id"]),
                    "file_name": f["file_name"],
                    "s3_url": f["s3_url"],
                    "file_type": f["file_type"],
                    "status": f["status"],
                    "created_at": f["created_at"].isoformat(),
                }
                for f in file_rows
            ],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/project
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/api/project")
async def delete_project(
    project_id: UUID = Query(..., description="要刪除的 project id"),
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    刪除指定專案。

    Cascade 行為（由 PostgreSQL schema 保證）：
    - chats.project_id  ON DELETE CASCADE  → 刪 project 會連帶刪所有 chats
    - messages.chat_id  ON DELETE CASCADE  → 刪 chats 會連帶刪所有 messages
    - files.project_id  ON DELETE CASCADE  → 刪 project 會連帶刪所有 files
    因此一句 DELETE FROM projects 即可清掉整個 project 子樹。

    安全設計：
    - 以 (id, user_id) 同時過濾，確保使用者無法刪除他人的 project
    - asyncpg.execute 對 DELETE 會回傳 'DELETE n'，n=0 表示沒匹配到

    HTTP 回應：
    - 200 OK   : 刪除成功
    - 401      : JWT 驗證失敗或 Token 已過期
    - 403      : 帳號已停用
    - 404      : 找不到專案或無權刪除
    - 500      : 其他未預期的資料庫錯誤
    """
    user_id = current_user["id"]

    try:
        result = await db.execute(
            """
            DELETE FROM projects
            WHERE id = $1 AND user_id = $2
            """,
            project_id,
            user_id,
        )
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}"
        )

    if result == "DELETE 0":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or you don't have permission to delete it."
        )

    return {
        "status": "success",
        "message": "Project and all related chats / messages / files have been deleted.",
        "data": {
            "id": str(project_id),
        },
    }
