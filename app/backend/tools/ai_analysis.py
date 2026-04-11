import time
import os
from typing import List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

# --- 資料庫連線配置 ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "stock_insight")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

# 初始化客戶端
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB]
qdrant_client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

async def search_ai_analysis(
    query: str,
    query_embedding: List[float],
    chat_id: str,
    top_k: int = 10
) -> Dict[str, Any]:
    """
    AI 分析工具 #3：混合搜尋 (向量 + 關鍵字)
    """
    start_time = time.time()
    
    try:
        # 在 Qdrant 執行混合搜尋 (Hybrid Search)
        search_result = await qdrant_client.search(
            collection_name="ai_analysis",
            query_vector=query_embedding,
            limit=top_k,
            with_payload=True
        )
        
        context = []
        for hit in search_result:
            payload = hit.payload
            context.append({
                "title": payload.get("title", "未知標題"),
                "ai_analysis_id": payload.get("ai_analysis_id"),
                "chunk_id": payload.get("chunk_id"),
                "content": payload.get("content", "") # 檢索到的片段
            })
            
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

async def get_full_ai_analysis(
    ai_analysis_ids: List[str],
    chat_id: str,
    query: str,
    query_embedding: List[float],
    top_k: int = 10
) -> Dict[str, Any]:
    """
    AI 分析工具 #4：獲取完整分析報告 (MongoDB)
    這通常是要給一個完整的 ai_analysis_id 列表，然後去 MongoDB 把列表內的所有分析內容完整拿回。
    """
    start_time = time.time()
    
    try:
        # 到 MongoDB 根據 _id 列表拿取全文
        from bson import ObjectId
        object_ids = [ObjectId(aid) for aid in ai_analysis_ids]
        cursor = db.ai_analysis.find({"_id": {"$in": object_ids}})
        documents = await cursor.to_list(length=len(ai_analysis_ids))
        
        context = []
        for doc in documents:
            context.append({
                "title": doc.get("title"),
                "ai_analysis_id": str(doc.get("_id")),
                "content": doc.get("content", ""), # 全文報告
                "created_at": str(doc.get("created_at", ""))
            })
            
        if not context:
            context = [{"error": f"找不到指定的 AI 分析 IDs: {ai_analysis_ids}"}]
            
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
