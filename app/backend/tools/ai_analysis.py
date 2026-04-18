"""
AI 分析搜尋工具 v2
===================
善用 v2 遷移腳本產生的新 metadata 欄位進行精準過濾：
- chunk_type: summary / key_news / stock_insight (按語意角色過濾)
- sentiment_label: positive / negative / neutral
- industry_list: 產業標籤
- search_groups: 按 mongo_id 聚合，避免同一篇分析的不同 chunks 重複出現
"""

import os
import time
from typing import List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from dotenv import load_dotenv

load_dotenv()

# 初始化連線
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

mongo_client = AsyncIOMotorClient(MONGODB_URL)
db = mongo_client["stock_insight"]
# 全面改用 AsyncQdrantClient
qdrant_client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


async def search_ai_analysis(
    query: str,
    query_embedding: List[float],
    chat_id: str,
    top_k: int = 10,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    chunk_type: Optional[str] = None,       # "summary" | "key_news" | "stock_insight"
    sentiment: Optional[str] = None,        # "positive" | "negative" | "neutral"
    industry: Optional[str] = None,         # 按產業標籤過濾
    score_threshold: float = 0.3,           # 🆕 P1: 最低相似度門檻
) -> Dict[str, Any]:
    """
    AI 分析工具：混合搜尋 (向量 + 時間/語意角色過濾)
    使用 search_groups 按 mongo_id 聚合，確保同一篇分析不會回傳多個 chunks。
    """
    must_conditions = []

    if start_date or end_date:
        must_conditions.append(models.FieldCondition(
            key="publishAt",
            range=models.DatetimeRange(gte=start_date, lte=end_date)
        ))

    # 按 chunk 類型過濾 (精準搜尋某種語意角色)
    if chunk_type:
        must_conditions.append(models.FieldCondition(
            key="chunk_type",
            match=models.MatchValue(value=chunk_type)
        ))

    # 按情緒過濾
    if sentiment:
        must_conditions.append(models.FieldCondition(
            key="sentiment_label",
            match=models.MatchValue(value=sentiment)
        ))

    # 按產業過濾
    if industry:
        must_conditions.append(models.FieldCondition(
            key="industry_list",
            match=models.MatchValue(value=industry)
        ))

    search_filter = models.Filter(must=must_conditions) if must_conditions else None

    try:
        # 使用 search_groups 按 mongo_id 聚合
        result = await qdrant_client.search_groups(
            collection_name="ai_analysis",
            query_vector=query_embedding,
            group_by="mongo_id",
            group_size=2,           # 每篇分析取最相關的 2 個 chunks (可能有 summary + key_news)
            query_filter=search_filter,
            limit=top_k,
            with_payload=True,
        )

        context = []
        for group in result.groups:
            top_hit = group.hits[0]

            # 🆕 P1: 過濾低品質結果
            if top_hit.score < score_threshold:
                continue

            # 將同一篇分析的多個 chunks 合併
            combined_content = ""
            first_payload = top_hit.payload

            for hit in group.hits:
                combined_content += hit.payload.get("content", "") + "\n\n"

            context.append({
                "title": first_payload.get("title", "無標題"),
                "content": combined_content.strip(),
                "mongo_id": first_payload.get("mongo_id"),
                "publishAt": first_payload.get("publishAt"),
                # 新增欄位
                "sentiment": first_payload.get("sentiment"),
                "sentiment_label": first_payload.get("sentiment_label"),
                "stock_list": first_payload.get("stock_list", []),
                "industry_list": first_payload.get("industry_list", []),
                "source_news_titles": first_payload.get("source_news_titles", []),
                "chunk_types": [h.payload.get("chunk_type") for h in group.hits],
                "score": top_hit.score,
            })

        return {"context": context}
    except Exception as e:
        print(f"❌ Error searching AI analysis: {e}")
        return {"context": []}


async def search_recommendations(
    query_embedding: List[float],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    top_k: int = 10
) -> Dict[str, Any]:
    """
    推薦專用工具：從 AI 分析報告中提取結構化的推薦股票與產業標籤。
    🆕 只搜尋 chunk_type=stock_insight 的向量，精準命中「潛力標的分析」。
    """
    must_conditions = []

    if start_date or end_date:
        must_conditions.append(models.FieldCondition(
            key="publishAt",
            range=models.DatetimeRange(gte=start_date, lte=end_date)
        ))

    # 🆕 只搜尋「潛力標的」類型的 chunk
    must_conditions.append(models.FieldCondition(
        key="chunk_type",
        match=models.MatchValue(value="stock_insight")
    ))

    search_filter = models.Filter(must=must_conditions)

    try:
        search_result = await qdrant_client.search(
            collection_name="ai_analysis",
            query_vector=query_embedding,
            query_filter=search_filter,
            limit=top_k,
            with_payload=True
        )

        recommended_stocks = set()
        recommended_industries = set()
        details = []

        for hit in search_result:
            payload = hit.payload
            raw_stocks = payload.get("stock_list", [])
            formatted_stocks = []
            if raw_stocks:
                for s in raw_stocks:
                    if isinstance(s, list) and len(s) >= 3:
                        stock_name = f"{s[2]}({s[1]})"
                        recommended_stocks.add(stock_name)
                        formatted_stocks.append(stock_name)
                    elif isinstance(s, str):
                        recommended_stocks.add(s)
                        formatted_stocks.append(s)

            industries = payload.get("industry_list", [])
            recommended_industries.update(industries)

            details.append({
                "title": payload.get("title"),
                "mongo_id": payload.get("mongo_id"),
                "content": payload.get("content", ""),
                "stocks": formatted_stocks,
                "industries": industries,
                "publishAt": payload.get("publishAt"),
                # 🆕 新增溯源欄位
                "source_news_titles": payload.get("source_news_titles", []),
                "sentiment": payload.get("sentiment"),
                "sentiment_label": payload.get("sentiment_label"),
            })

        return {
            "stocks": list(recommended_stocks),
            "industries": list(recommended_industries),
            "sources": details
        }
    except Exception as e:
        print(f"❌ Error searching recommendations: {e}")
        return {"stocks": [], "industries": [], "sources": []}


async def get_full_ai_analysis(mongo_ids: List[str]) -> List[Dict[str, Any]]:
    from bson import ObjectId
    try:
        object_ids = [ObjectId(mid) for mid in mongo_ids if mid]
        cursor = db["AI_news_analysis"].find({"_id": {"$in": object_ids}})
        docs = await cursor.to_list(length=len(mongo_ids))

        results = []
        for doc in docs:
            results.append({
                "id": str(doc["_id"]),
                "summary": doc.get("summary", ""),
                "title": doc.get("article_title", ""),
                "important_news": doc.get("important_news", ""),
                "potential_stocks_and_industries": doc.get("potential_stocks_and_industries", ""),
                "stock_list": doc.get("stock_list", []),
                "industry_list": doc.get("industry_list", []),
            })
        return results
    except Exception as e:
        print(f"❌ Error getting full AI analysis: {e}")
        return []
