"""
Market Feed API（新聞瀏覽／AI 分析瀏覽）
=========================================
從原 stock_insight 專案移植過來的兩個瀏覽功能，直接讀 MongoDB：

- GET /api/feed/news               新聞列表（關鍵字、時間區間、分頁）
- GET /api/feed/news/{news_id}     單篇新聞的原始連結（AI 分析卡片的「相關新聞」點擊用）
- GET /api/feed/ai-analysis        AI 新聞分析列表（關鍵字、產業、逐條／統整、時間區間、分頁）

連線設定預設沿用 MONGO_URI / MONGO_DB（與 tools/news.py 同一個資料庫）。
若要指向不同的叢集或改用唯讀帳號，可另外設定（選用）：

- FEED_MONGO_URI   覆寫連線字串；未設定時用 MONGO_URI（兩者都沒有時本模組回 503，不影響其他功能）
- FEED_MONGO_DB    覆寫資料庫名稱；未設定時用 MONGO_DB，再沒有則為 stock_insight

安全設計：
- 所有端點需登入（純 JWT 驗證，不佔 PostgreSQL 連線）
- 關鍵字一律 re.escape 後才進 $regex，避免使用者輸入的正則造成 ReDoS 或語意錯誤
- 分頁、字串長度都有上限
"""

import os
import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure, PyMongoError

from app.backend.module.jwt import get_current_user_id

router = APIRouter(tags=["Market Feed"])

_NEWS_COLLECTION = "news"
_AI_COLLECTION = "AI_news_analysis"

_NEWS_PAGE_SIZE = 20
_AI_PAGE_SIZE = 15
_NEWS_CONTENT_PREVIEW = 150
_MAX_PAGE = 500
_KEYWORD_MAX = 100

# 列表不需要的大欄位，與原專案 projection 一致
_NEWS_PROJECTION = {
    "_id": 0,
    "news_id": 0,
    "summary": 0,
    "keyword": 0,
    "market": 0,
    "type": 0,
    "stock": 0,
}

_client: Optional[AsyncIOMotorClient] = None


def _get_db() -> AsyncIOMotorDatabase:
    """延遲建立 client：沒設連線字串時不影響後端啟動，只讓本模組回 503。"""
    global _client
    uri = os.getenv("FEED_MONGO_URI") or os.getenv("MONGO_URI")
    if not uri:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="新聞資料庫尚未設定（MONGO_URI）",
        )
    if _client is None:
        _client = AsyncIOMotorClient(
            uri,
            maxPoolSize=10,
            serverSelectionTimeoutMS=5000,
        )
    db_name = os.getenv("FEED_MONGO_DB") or os.getenv("MONGO_DB") or "stock_insight"
    return _client[db_name]


def _jsonable(value: Any) -> Any:
    """ObjectId 等 BSON 型別轉成 JSON 可序列化的值（巢狀的 source_news._id 也要轉）。"""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _time_filter(start_time: Optional[int], end_time: Optional[int]) -> Optional[Dict[str, int]]:
    cond: Dict[str, int] = {}
    if start_time is not None:
        cond["$gte"] = start_time
    if end_time is not None:
        cond["$lte"] = end_time
    return cond or None


def _clean_keyword(keyword: Optional[str]) -> str:
    return (keyword or "").strip()[:_KEYWORD_MAX]


def _db_error(err: Exception) -> HTTPException:
    print(f"[FEED] MongoDB error: {type(err).__name__}: {err}", flush=True)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="新聞資料庫暫時無法連線，請稍後再試",
    )


@router.get("/api/feed/news")
async def list_news(
    keyword: Optional[str] = Query(None),
    start_time: Optional[int] = Query(None, ge=0),
    end_time: Optional[int] = Query(None, ge=0),
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    _user_id: UUID = Depends(get_current_user_id),
):
    collection = _get_db()[_NEWS_COLLECTION]
    kw = _clean_keyword(keyword)

    base: Dict[str, Any] = {}
    time_cond = _time_filter(start_time, end_time)
    if time_cond:
        base["publishAt"] = time_cond

    skip = (page - 1) * _NEWS_PAGE_SIZE

    async def run(query: Dict[str, Any]) -> List[Dict[str, Any]]:
        cursor = (
            collection.find(query, _NEWS_PROJECTION)
            .sort("publishAt", -1)
            .skip(skip)
            .limit(_NEWS_PAGE_SIZE + 1)
        )
        return await cursor.to_list(length=_NEWS_PAGE_SIZE + 1)

    try:
        if kw:
            try:
                # 原專案 news collection 建有 text index，優先走 $text
                results = await run({**base, "$text": {"$search": kw}})
            except OperationFailure:
                # 沒有 text index 時退回標題／內文 regex
                pattern = {"$regex": re.escape(kw), "$options": "i"}
                results = await run({**base, "$or": [{"title": pattern}, {"content": pattern}]})
        else:
            results = await run(base)
    except PyMongoError as err:
        raise _db_error(err)

    has_next = len(results) > _NEWS_PAGE_SIZE
    results = results[:_NEWS_PAGE_SIZE]
    for r in results:
        content = r.get("content")
        if isinstance(content, str):
            r["content"] = content[:_NEWS_CONTENT_PREVIEW]

    return {
        "page": page,
        "nextPage": page + 1 if has_next else None,
        "data": _jsonable(results),
    }


@router.get("/api/feed/news/{news_id}")
async def get_news_url(
    news_id: str,
    _user_id: UUID = Depends(get_current_user_id),
):
    if not ObjectId.is_valid(news_id):
        raise HTTPException(status_code=400, detail="Invalid news id")
    collection = _get_db()[_NEWS_COLLECTION]
    try:
        doc = await collection.find_one(
            {"_id": ObjectId(news_id)}, {"_id": 0, "url": 1, "title": 1}
        )
    except PyMongoError as err:
        raise _db_error(err)
    if not doc:
        raise HTTPException(status_code=404, detail="News not found")
    return {"data": _jsonable(doc)}


@router.get("/api/feed/ai-analysis")
async def list_ai_analysis(
    keyword: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    is_summary: Optional[bool] = Query(None),
    start_time: Optional[int] = Query(None, ge=0),
    end_time: Optional[int] = Query(None, ge=0),
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    _user_id: UUID = Depends(get_current_user_id),
):
    collection = _get_db()[_AI_COLLECTION]
    query: Dict[str, Any] = {}

    kw = _clean_keyword(keyword)
    if kw:
        pattern = {"$regex": re.escape(kw), "$options": "i"}
        query["$or"] = [
            {"summary": pattern},
            {"important_news": pattern},
            {"potential_stocks_and_industries": pattern},
            {"source_news.title": pattern},
        ]

    ind = _clean_keyword(industry)
    if ind:
        query["industry_list"] = {
            "$elemMatch": {"$regex": re.escape(ind), "$options": "i"}
        }

    if is_summary is not None:
        query["is_summary"] = is_summary

    time_cond = _time_filter(start_time, end_time)
    if time_cond:
        query["publishAt"] = time_cond

    skip = (page - 1) * _AI_PAGE_SIZE
    try:
        cursor = (
            collection.find(query)
            .sort("publishAt", -1)
            .skip(skip)
            .limit(_AI_PAGE_SIZE + 1)
        )
        results = await cursor.to_list(length=_AI_PAGE_SIZE + 1)
    except PyMongoError as err:
        raise _db_error(err)

    has_next = len(results) > _AI_PAGE_SIZE
    results = results[:_AI_PAGE_SIZE]

    return {
        "page": page,
        "nextPage": page + 1 if has_next else None,
        "data": _jsonable(results),
    }
