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
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    AI 分析工具：混合搜尋 (向量 + 時間過濾)
    """
    search_filter = None
    if start_date or end_date:
        search_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="publishAt",
                    range=models.DatetimeRange(gte=start_date, lte=end_date)
                )
            ]
        )

    try:
        search_result = await qdrant_client.search(
            collection_name="ai_analysis",
            query_vector=query_embedding,
            query_filter=search_filter,
            limit=top_k,
            with_payload=True
        )
        
        context = []
        for hit in search_result:
            payload = hit.payload
            context.append({
                "title": payload.get("title", "無標題"),
                "content": payload.get("content", ""),
                "mongo_id": payload.get("mongo_id"),
                "publishAt": payload.get("publishAt")
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
    """
    search_filter = None
    if start_date or end_date:
        search_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="publishAt",
                    range=models.DatetimeRange(gte=start_date, lte=end_date)
                )
            ]
        )

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
                "publishAt": payload.get("publishAt")
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
                "title": doc.get("title", "")
            })
        return results
    except Exception as e:
        print(f"❌ Error getting full AI analysis: {e}")
        return []
