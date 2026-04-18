"""
MongoDB → Qdrant 向量遷移工具 v2
=================================
切分策略：
  - news collection:          RecursiveCharacterTextSplitter (語意段落切分)
  - AI_news_analysis collection: 按欄位角色拆分 (summary / key_news / stock_insight)

特性：
  - Batch Embedding (OpenAI 支援一次最多 2048 筆)
  - 完整 Metadata 保留 (keywords, stock_names, source_news, etc.)
  - 確定性 UUID 防止重複入庫
  - tqdm 進度條
  - Exponential backoff 重試機制
"""

import os
import sys
import time
import uuid
import pytz
import asyncio
import argparse
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from openai import AsyncOpenAI
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from tqdm import tqdm

# ─── 環境配置 ──────────────────────────────────────────────
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "stock_insight")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# ─── 初始化客戶端 ───────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB]
qdrant_client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ─── 新聞 Text Splitter (語意段落切分) ───────────────────────
news_splitter = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=150,
    separators=["\n\n", "\n", "。", "，", "；", " ", ""],
    keep_separator=True,
    is_separator_regex=False,
)


# ═══════════════════════════════════════════════════════════
# 工具函式
# ═══════════════════════════════════════════════════════════

def transform_timestamp(unix_ts: int) -> str:
    """Unix timestamp → ISO 8601 (Asia/Taipei)"""
    tz = pytz.timezone("Asia/Taipei")
    dt = datetime.fromtimestamp(unix_ts, tz=tz)
    return dt.isoformat()


def refine_sentiment(sentiment_text: str) -> str:
    """將中文情緒描述轉為分類標籤 (供 Qdrant filter 使用)"""
    if not sentiment_text:
        return "neutral"
    neg = ["負面", "風險", "惡化", "衝擊", "下行", "衰退", "疲軟", "緊張", "危機", "威脅", "利空"]
    pos = ["正面", "看好", "成長", "利多", "亮眼", "樂觀", "上揚", "強勁", "擴張", "回升", "受惠", "復甦"]
    neu = ["中性", "觀望", "盤整", "震盪", "持平", "穩定", "互見", "有限"]

    n = sum(1 for k in neg if k in sentiment_text)
    p = sum(1 for k in pos if k in sentiment_text)
    u = sum(1 for k in neu if k in sentiment_text)

    if n > p and n >= u:
        return "negative"
    if p > n and p >= u:
        return "positive"
    return "neutral"


def generate_deterministic_uuid(mongo_id: str, chunk_type: str, chunk_idx: int) -> str:
    """基於 mongo_id + chunk_type + idx 產生固定 UUID，防止重複向量入庫"""
    namespace = uuid.NAMESPACE_DNS
    return str(uuid.uuid5(namespace, f"{mongo_id}_{chunk_type}_{chunk_idx}"))


async def batch_embed(texts: List[str], batch_size: int = 512, max_retries: int = 3) -> List[List[float]]:
    """
    批次產生 embeddings，帶指數退避重試。
    OpenAI API 一次最多接受 2048 筆 input，這裡預設 512 為安全批次大小。
    """
    all_embeddings: List[List[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        for attempt in range(max_retries):
            try:
                response = await ai_client.embeddings.create(
                    input=batch,
                    model=EMBEDDING_MODEL
                )
                all_embeddings.extend([item.embedding for item in response.data])
                break
            except Exception as e:
                wait_time = 2 ** attempt
                print(f"  ⚠️  Embedding batch {i // batch_size} failed (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    print(f"     Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"  ❌ Batch {i // batch_size} 永久失敗，填入零向量")
                    all_embeddings.extend([[0.0] * EMBEDDING_DIM] * len(batch))

    return all_embeddings


# ═══════════════════════════════════════════════════════════
# News Collection 切分邏輯
# ═══════════════════════════════════════════════════════════

def chunk_news_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    對單篇新聞文件做語意切分。
    短文 (< 800 字) 直接當單一 chunk，長文使用 RecursiveCharacterTextSplitter。
    回傳格式：[{"text": ..., "payload": {...}}, ...]
    """
    title = doc.get("title", "無標題")
    content = doc.get("content", "")
    mongo_id = str(doc["_id"])

    if not content.strip():
        return []

    # 準備共用 metadata
    publish_at = transform_timestamp(doc.get("publishAt", int(time.time())))
    stock_codes = doc.get("stock", [])
    stock_names = [m["name"] for m in doc.get("market", [])] if doc.get("market") else []

    base_payload = {
        "mongo_id": mongo_id,
        "title": title,
        "publishAt": publish_at,
        "url": doc.get("url"),
        "source": doc.get("source"),
        "category": doc.get("category"),
        "type": doc.get("type"),
        "keywords": doc.get("keyword", []),
        "stock_codes": stock_codes,
        "stock_names": stock_names,
        "collection_type": "news",
    }

    # 切分策略：短文不切，長文語意切分
    if len(content) <= 800:
        text_for_embedding = f"[{title}] {content}"
        return [{
            "text": text_for_embedding,
            "payload": {
                **base_payload,
                "content": text_for_embedding,
                "chunk_idx": 0,
                "total_chunks": 1,
                "chunk_type": "full",
            }
        }]

    # 長文：使用 RecursiveCharacterTextSplitter
    chunks = news_splitter.split_text(content)
    total = len(chunks)
    results = []

    for idx, chunk_text in enumerate(chunks):
        text_for_embedding = f"[{title}] {chunk_text}"
        results.append({
            "text": text_for_embedding,
            "payload": {
                **base_payload,
                "content": text_for_embedding,
                "chunk_idx": idx,
                "total_chunks": total,
                "chunk_type": "partial",
            }
        })

    return results


# ═══════════════════════════════════════════════════════════
# AI_news_analysis Collection 切分邏輯
# ═══════════════════════════════════════════════════════════

def chunk_ai_analysis_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    對單篇 AI 分析報告按欄位角色拆分。
    每個欄位本身就是完整語意，不做二次切割。
    產出最多 3 個 chunk：summary / key_news / stock_insight
    """
    mongo_id = str(doc["_id"])
    title = doc.get("article_title", "無標題")
    publish_at = transform_timestamp(doc.get("publishAt", int(time.time())))
    sentiment_raw = doc.get("sentiment", "")

    # 共用 metadata
    base_payload = {
        "mongo_id": mongo_id,
        "title": title,
        "publishAt": publish_at,
        "sentiment": sentiment_raw,
        "sentiment_label": refine_sentiment(sentiment_raw),
        "stock_list": doc.get("stock_list", []),
        "industry_list": doc.get("industry_list", []),
        "category": doc.get("category"),
        "is_summary": doc.get("is_summary", False),
        "analysis_batch": doc.get("analysis_batch"),
        "source_news_titles": [s.get("title", "") for s in doc.get("source_news", [])],
        "source_news_ids": [str(s.get("_id", "")) for s in doc.get("source_news", [])],
        "collection_type": "ai_analysis",
    }

    results = []
    chunk_idx = 0

    # ── Chunk 1: 摘要向量 (summary + article_title) ──
    summary = doc.get("summary", "")
    if summary and summary.strip():
        text = f"[分析摘要] {title}：{summary}"
        results.append({
            "text": text,
            "payload": {
                **base_payload,
                "content": text,
                "chunk_type": "summary",
                "chunk_idx": chunk_idx,
            }
        })
        chunk_idx += 1

    # ── Chunk 2: 重要新聞向量 (important_news) ──
    important_news = doc.get("important_news", "")
    if important_news and important_news.strip():
        text = f"[重要新聞] {title}：{important_news}"
        results.append({
            "text": text,
            "payload": {
                **base_payload,
                "content": text,
                "chunk_type": "key_news",
                "chunk_idx": chunk_idx,
            }
        })
        chunk_idx += 1

    # ── Chunk 3: 潛力標的向量 (potential_stocks_and_industries) ──
    potential = doc.get("potential_stocks_and_industries", "")
    if potential and potential.strip():
        text = f"[潛力標的分析] {title}：{potential}"
        results.append({
            "text": text,
            "payload": {
                **base_payload,
                "content": text,
                "chunk_type": "stock_insight",
                "chunk_idx": chunk_idx,
            }
        })

    return results


# ═══════════════════════════════════════════════════════════
# 遷移主程序
# ═══════════════════════════════════════════════════════════

async def migrate_collection(
    mongo_col_name: str,
    qdrant_col_name: str,
    chunk_fn,
    limit: int,
    embedding_batch_size: int = 256,
    upsert_batch_size: int = 100,
    dry_run: bool = False,
):
    """
    通用遷移邏輯：
    1. 從 MongoDB 讀取文件
    2. 使用指定的 chunk_fn 切分
    3. 批次 Embedding
    4. 批次 Upsert 到 Qdrant
    """
    print(f"\n{'='*60}")
    print(f"🚀 開始遷移: {mongo_col_name} → {qdrant_col_name}")
    print(f"   最大文件數: {limit} | Embedding batch: {embedding_batch_size}")
    print(f"   模式: {'🧪 DRY RUN (不寫入 Qdrant)' if dry_run else '📝 正式寫入'}")
    print(f"{'='*60}")

    # Step 1: 從 MongoDB 讀取
    cursor = db[mongo_col_name].find().sort("_id", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    print(f"📦 從 MongoDB 讀取到 {len(docs)} 筆文件")

    if not docs:
        print("⚠️  沒有文件可處理，跳過")
        return

    # Step 2: 切分所有文件
    all_chunks: List[Dict[str, Any]] = []
    for doc in tqdm(docs, desc="✂️  切分文件", unit="doc"):
        chunks = chunk_fn(doc)
        all_chunks.extend(chunks)

    print(f"📐 總共產生 {len(all_chunks)} 個 chunks (平均每篇 {len(all_chunks)/len(docs):.1f} 個)")

    if not all_chunks:
        print("⚠️  切分後無有效 chunk，跳過")
        return

    # Dry run 模式：顯示前幾筆範例
    if dry_run:
        print(f"\n📋 前 3 筆 chunk 範例：")
        for i, chunk in enumerate(all_chunks[:3]):
            print(f"\n--- Chunk {i} ---")
            print(f"  chunk_type: {chunk['payload'].get('chunk_type')}")
            print(f"  title: {chunk['payload']['title']}")
            print(f"  text (前200字): {chunk['text'][:200]}...")
            print(f"  payload keys: {list(chunk['payload'].keys())}")
        print(f"\n🧪 Dry run 完成，不寫入 Qdrant")
        return

    # Step 3: 批次 Embedding
    print(f"\n🧠 開始產生 Embeddings ({len(all_chunks)} 筆)...")
    texts_to_embed = [c["text"] for c in all_chunks]
    embeddings = await batch_embed(texts_to_embed, batch_size=embedding_batch_size)
    print(f"✅ Embedding 完成")

    # Step 4: 組裝 Points 並批次 Upsert
    print(f"📤 開始寫入 Qdrant...")
    points: List[models.PointStruct] = []
    for chunk, embedding in zip(all_chunks, embeddings):
        mongo_id = chunk["payload"]["mongo_id"]
        chunk_type = chunk["payload"].get("chunk_type", "unknown")
        chunk_idx = chunk["payload"].get("chunk_idx", 0)
        point_id = generate_deterministic_uuid(mongo_id, chunk_type, chunk_idx)

        points.append(models.PointStruct(
            id=point_id,
            vector=embedding,
            payload=chunk["payload"]
        ))

    # 批次 upsert
    for i in tqdm(range(0, len(points), upsert_batch_size), desc="📤 Upsert", unit="batch"):
        batch = points[i:i + upsert_batch_size]
        await qdrant_client.upsert(collection_name=qdrant_col_name, points=batch)

    print(f"✨ 遷移完成: {mongo_col_name} → {qdrant_col_name}")
    print(f"   文件數: {len(docs)} | Chunks: {len(all_chunks)} | Points: {len(points)}")


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(
        description="MongoDB → Qdrant 向量資料遷移工具 v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  # 遷移全部 collection (各取最新 100 篇)
  python migrate_to_qdrant.py --limit 100

  # 只遷移新聞
  python migrate_to_qdrant.py --collection news --limit 500

  # Dry run 模式 (只看切分結果，不寫入)
  python migrate_to_qdrant.py --collection ai_analysis --limit 10 --dry-run
        """
    )
    parser.add_argument("--collection", type=str, default="all",
                        choices=["all", "news", "ai_analysis"],
                        help="要遷移的 collection (預設: all)")
    parser.add_argument("--limit", type=int, default=100,
                        help="每個 collection 最多處理幾篇文件 (預設: 100)")
    parser.add_argument("--embedding-batch-size", type=int, default=256,
                        help="Embedding API 批次大小 (預設: 256)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry run 模式：只執行切分預覽，不呼叫 Embedding API 也不寫入 Qdrant")
    args = parser.parse_args()

    start_time = time.time()

    if args.collection in ("all", "news"):
        await migrate_collection(
            mongo_col_name="news",
            qdrant_col_name="news",
            chunk_fn=chunk_news_document,
            limit=args.limit,
            embedding_batch_size=args.embedding_batch_size,
            dry_run=args.dry_run,
        )

    if args.collection in ("all", "ai_analysis"):
        await migrate_collection(
            mongo_col_name="AI_news_analysis",
            qdrant_col_name="ai_analysis",
            chunk_fn=chunk_ai_analysis_document,
            limit=args.limit,
            embedding_batch_size=args.embedding_batch_size,
            dry_run=args.dry_run,
        )

    total_time = time.time() - start_time
    print(f"\n🏁 全部遷移完成！總耗時: {total_time:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
