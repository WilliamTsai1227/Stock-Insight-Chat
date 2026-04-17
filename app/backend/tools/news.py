import time
import os
from typing import List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from dotenv import load_dotenv

# 載入 .env 環境變數
load_dotenv()

# --- 資料庫連線配置 ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
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
    top_k: int = 10
) -> Dict[str, Any]:
    """
    新聞工具 #1：混合搜尋 (向量 + 關鍵字)
    """
    start_time = time.time()
    
    try:
        # 在 Qdrant 執行混合搜尋 (Hybrid Search)
        # 這裡示範基本的向量搜尋，若要支援關鍵字過濾可加模型過濾器
        search_result = await qdrant_client.search(
            collection_name="news",
            query_vector=query_embedding,
            limit=top_k,
            with_payload=True
        )
        
        context = []
        for hit in search_result:
            payload = hit.payload
            context.append({
                "title": payload.get("title", "未知標題"),
                "mongo_id": payload.get("mongo_id"), # 與遷移指令同步
                "chunk_idx": payload.get("chunk_idx"),
                "content": payload.get("content", "") 
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
                "content": doc.get("content", ""), # 這是全文
                "publishAt": str(doc.get("publishAt", ""))
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
