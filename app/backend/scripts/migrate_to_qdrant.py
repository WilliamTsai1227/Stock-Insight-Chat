import os
import time
import uuid
import pytz
import argparse
from datetime import datetime
from typing import List, Dict, Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from openai import AsyncOpenAI

# 1. 環境配置
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "stock_insight")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 2. 初始化客戶端
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB]
qdrant_client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# --- 核心邏輯 A：時間轉換 ---
def transform_timestamp(unix_ts: int) -> str:
    tz = pytz.timezone("Asia/Taipei")
    dt = datetime.fromtimestamp(unix_ts, tz=tz)
    return dt.isoformat()

# --- 核心邏輯 A.2：情緒文字處理 ---
def refine_sentiment(sentiment_text: str) -> str:
    if not sentiment_text: return "neutral"
    neg_keywords = ["負面", "風險", "惡化", "影響經濟", "衝擊", "下行", "衰退", "疲軟", "緊張", "危機", "封鎖", "威脅"]
    pos_keywords = ["正面", "看好", "成長", "利多", "亮眼", "樂觀", "上揚", "強勁", "擴張", "回升", "受惠", "復甦"]
    neu_keywords = ["中性", "觀望", "盤整", "震盪", "持平", "穩定", "互見", "有限"]

    neg_hits = sum(1 for k in neg_keywords if k in sentiment_text)
    pos_hits = sum(1 for k in pos_keywords if k in sentiment_text)
    neu_hits = sum(1 for k in neu_keywords if k in sentiment_text)

    if neg_hits > pos_hits and neg_hits >= neu_hits: return "negative"
    if pos_hits > neg_hits and pos_hits >= neu_hits: return "positive"
    return "neutral"

# --- 核心邏輯 B：分段器 ---
def split_text_into_chunks(text: str, title: str, chunk_size: int = 1000) -> List[str]:
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunks.append(f"[{title}]: {text[i:i + chunk_size]}")
    return chunks

async def get_embedding(text: str) -> List[float]:
    try:
        response = await ai_client.embeddings.create(input=text, model="text-embedding-3-small")
        return response.data[0].embedding
    except Exception as e:
        print(f"Embedding error: {e}")
        return [0.0] * 1536

# --- 核心邏輯 C：確定性 ID 生成器 ---
def generate_deterministic_uuid(mongo_id: str, chunk_idx: int) -> str:
    """基於原始 ID 與序號產生固定 UUID，防止重複向量入庫"""
    # 使用 NAMESPACE_DNS 確保這兩個字串組合出的 UUID 是唯一的且固定的
    namespace = uuid.NAMESPACE_DNS
    return str(uuid.uuid5(namespace, f"{mongo_id}_{chunk_idx}"))

# --- 核心邏輯 D：遷移主程序 ---
async def migrate_collection(mongo_col_name: str, qdrant_col_name: str, mapping_config: Dict[str, Any], limit: int, batch_size: int):
    print(f"🚀 Starting migration legacy: {mongo_col_name} -> {qdrant_col_name} (Limit: {limit})")
    
    cursor = db[mongo_col_name].find().limit(limit)
    processed_count = 0
    
    async for doc in cursor:
        mongo_id = str(doc.get("_id"))
        title = doc.get(mapping_config["title_key"], "無標題")
        
        text_to_chunk = ""
        for key in mapping_config["content_keys"]:
            val = doc.get(key, "")
            if val: text_to_chunk += f"\n{val}"
            
        chunks = split_text_into_chunks(text_to_chunk, title)
        points = []
        
        for idx, chunk_text in enumerate(chunks):
            vector = await get_embedding(chunk_text)
            
            payload = {
                "mongo_id": mongo_id,
                "title": title,
                "publishAt": transform_timestamp(doc.get("publishAt", int(time.time()))),
                "chunk_idx": idx,
                "content": chunk_text
            }
            
            for extra_key in mapping_config.get("extra_payload_keys", []):
                 val = doc.get(extra_key)
                 if extra_key == "sentiment": val = refine_sentiment(val)
                 
                 # 統一命名為 stock_list (針對新聞 collection 的差異處理)
                 store_key = "stock_list" if extra_key == "stock" else extra_key
                 payload[store_key] = val

            # 使用確定性 ID：如果 mongo_id 和 chunk_idx 一樣，Qdrant 會執行 Overwrite
            point_id = generate_deterministic_uuid(mongo_id, idx)
            
            points.append(models.PointStruct(id=point_id, vector=vector, payload=payload))
            
        if points:
            await qdrant_client.upsert(collection_name=qdrant_col_name, points=points)
        
        processed_count += 1
        if processed_count % 10 == 0:
            print(f"Processed {processed_count}/{limit} documents in {mongo_col_name}...")
            
    print(f"✨ Finished {mongo_col_name}. Total documents processed: {processed_count}")

async def main():
    parser = argparse.ArgumentParser(description="MongoDB to Qdrant Data Migration Tool")
    parser.add_argument("--limit", type=int, default=100, help="Maximum documents to process per collection (Default: 100)")
    parser.add_argument("--batch_size", type=int, default=50, help="Batch size for processing (Default: 50)")
    args = parser.parse_args()

    # 1. 新聞遷移
    await migrate_collection(
        mongo_col_name="news",
        qdrant_col_name="news",
        mapping_config={
            "title_key": "title",
            "content_keys": ["content"],
            # 將 MongoDB 的 'stock' 欄位名稱對應至 Qdrant 的 'stock_list'
            "extra_payload_keys": ["source", "category", "url", "stock"] 
        },
        limit=args.limit,
        batch_size=args.batch_size
    )
    
    # 2. AI 分析遷移
    await migrate_collection(
        mongo_col_name="ai_analysis",
        qdrant_col_name="ai_analysis",
        mapping_config={
            "title_key": "article_title",
            "content_keys": ["summary", "important_news"],
            "extra_payload_keys": ["sentiment", "industry_list", "stock_list"]
        },
        limit=args.limit,
        batch_size=args.batch_size
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
