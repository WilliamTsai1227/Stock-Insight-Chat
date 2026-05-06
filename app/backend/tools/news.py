"""
新聞搜尋工具 v2
================
善用 v2 遷移腳本產生的新 metadata 欄位進行精準過濾：
- stock_codes: 按股票代碼過濾
- keywords: 按新聞關鍵字過濾
- type: 按新聞類型 (台股/國際) 過濾
- hybrid / RRF：依 mongo_id 聚合，避免同一篇文章的不同 chunks 重複洗版
"""

import asyncio
import time
import os
from typing import List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from dotenv import load_dotenv

from app.backend.tools.qdrant_hybrid import embed_sparse_query, hybrid_rrf_grouped

# 載入 .env 環境變數
load_dotenv()

# --- 資料庫連線配置 ---
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "stock_insight")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

# 初始化客戶端
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB]
qdrant_client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


async def search_news(
    query: str,
    query_embedding: List[float],
    chat_id: str,
    top_k: int = 10,
    start_date: Optional[str] = None,  # ISO 格式字串
    end_date: Optional[str] = None,
    stock_code: Optional[str] = None,  # 按股票代碼過濾 (e.g. "2330")
    news_type: Optional[str] = None,   # 按新聞類型過濾 (e.g. "台股新聞")
    keyword: Optional[str] = None,     # 按 keywords 欄位過濾 (e.g. "國巨")
    stock_name: Optional[str] = None,  # 按 stock_names 欄位過濾 (e.g. "勤誠")
    score_threshold: Optional[float] = None,  # RRF 融合分數尺度與純 cosine 不同；None 表示不裁切
) -> Dict[str, Any]:
    """
    新聞工具 #1：Hybrid 檢索（dense 語意 + BM25 sparse，Qdrant RRF 融合）+ payload 過濾。
    依 mongo_id 聚合、每組最多 2 chunks（與先前 search_groups 行為一致）。
    stock_code、keyword、stock_name 使用 should (OR) 邏輯，只要其中一個匹配即可。
    """
    start_time = time.time()

    # 組建過濾條件
    must_conditions = []
    should_conditions = []

    if start_date or end_date:
        must_conditions.append(models.FieldCondition(
            key="publishAt",
            range=models.DatetimeRange(
                gte=start_date,
                lte=end_date
            )
        ))

    # stock_code、keyword、stock_name 用 should (OR) 邏輯
    # 只要其中任一欄位匹配，該文件就會被保留
    if stock_code:
        should_conditions.append(models.FieldCondition(
            key="stock_codes",
            match=models.MatchValue(value=stock_code)
        ))

    if keyword:
        should_conditions.append(models.FieldCondition(
            key="keywords",
            match=models.MatchValue(value=keyword)
        ))

    if stock_name:
        should_conditions.append(models.FieldCondition(
            key="stock_names",
            match=models.MatchValue(value=stock_name)
        ))

    # 新聞類型過濾 (必須滿足)
    if news_type:
        must_conditions.append(models.FieldCondition(
            key="type",
            match=models.MatchValue(value=news_type)
        ))

    # 組建 filter：must 全部滿足 且 should 至少一個滿足
    filter_args = {}
    if must_conditions:
        filter_args["must"] = must_conditions
    if should_conditions:
        filter_args["should"] = should_conditions
    search_filter = models.Filter(**filter_args) if filter_args else None

    try:
        sparse_vec = await asyncio.to_thread(embed_sparse_query, query)
        grouped = await hybrid_rrf_grouped(
            qdrant_client,
            "news",
            query_embedding,
            sparse_vec,
            search_filter,
            group_by_payload_key="mongo_id",
            group_size=2,
            top_k=top_k,
            score_threshold=score_threshold,
        )

        context = []
        for _gid, hits in grouped:
            top_hit = hits[0]
            first_payload = top_hit.payload or {}

            combined_content = "\n".join(
                (h.payload or {}).get("content", "") for h in hits
            )

            context.append({
                "title": first_payload.get("title", "未知標題"),
                "mongo_id": first_payload.get("mongo_id"),
                "publishAt": first_payload.get("publishAt"),
                "url": first_payload.get("url"),
                "total_chunks": first_payload.get("total_chunks"),
                "chunks_retrieved": len(hits),
                "content": combined_content,
                "source": first_payload.get("source"),
                "stock_codes": first_payload.get("stock_codes", []),
                "stock_names": first_payload.get("stock_names", []),
                "keywords": first_payload.get("keywords", []),
                "score": top_hit.score,
            })

    except Exception as e:
        context = [{"error": str(e)}]

    execution_time = time.time() - start_time

    return {
        "query": query,
        "top_k": top_k,
        "execution_time": round(execution_time, 4),
        "context": context,
        "chat_id": chat_id
    }


async def get_full_news(
    news_ids: List[str],
    chat_id: str,
    query: str,
    query_embedding: List[float],
    top_k: int = 10
) -> Dict[str, Any]:
    """
    新聞工具 #2：獲取完整新聞內容 (MongoDB)
    這通常是要給一個完整的 news_id 列表，然後去 MongoDB 把列表內的所有新聞內容完整拿回。
    """
    start_time = time.time()

    try:
        # 到 MongoDB 根據 _id 列表拿取全文
        from bson import ObjectId
        object_ids = [ObjectId(nid) for nid in news_ids]
        cursor = db.news.find({"_id": {"$in": object_ids}})
        documents = await cursor.to_list(length=len(news_ids))

        context = []
        for doc in documents:
            context.append({
                "title": doc.get("title"),
                "mongo_id": str(doc.get("_id")),
                "content": doc.get("content", ""),  # 這是全文
                "publishAt": str(doc.get("publishAt", "")),
                "url": doc.get("url"),
            })

        if not context:
            context = [{"error": f"找不到指定的新聞 IDs: {news_ids}"}]

    except Exception as e:
        context = [{"error": str(e)}]

    execution_time = time.time() - start_time

    return {
        "query": query,
        "query_embedding": query_embedding,
        "top_k": top_k,
        "execution_time": round(execution_time, 4),
        "context": context,
        "chat_id": chat_id
    }
