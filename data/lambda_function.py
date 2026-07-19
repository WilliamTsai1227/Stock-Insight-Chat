"""
MongoDB → Qdrant 向量同步 (AWS Lambda 版)
==========================================
改寫自 app/backend/scripts/migrate_to_qdrant.py，供 EventBridge 排程觸發，
自動將 MongoDB 新增文件向量化後寫入 EC2 上的 Qdrant。

與原腳本的差異：
  - 增量同步：預設只處理「最近 SYNC_SINCE_HOURS 小時」內新增的文件
    （以 ObjectId 內含的時間戳過濾，無需額外狀態存儲；
     搭配確定性 UUID，重跑同一區間不會產生重複向量）
  - 移除 tqdm / argparse / dotenv，改由 Lambda 環境變數與 event 參數控制
  - Embedding 重試耗盡後直接 raise（讓 Lambda 執行失敗、可觸發告警），
    不再填零向量；upsert 冪等，重跑即可補齊
  - FastEmbed BM25 模型快取指向唯讀映像內預載路徑（Dockerfile 烘入），
    找不到時退回 /tmp 下載

Event 參數（皆可省略）：
  {
    "collection": "all" | "news" | "ai_analysis",   # 預設 all
    "since_hours": 24,        # 只處理最近 N 小時的新文件；0 = 不過濾時間
    "limit": 500,             # 每個 collection 最多處理幾篇
    "dry_run": false          # true 時只切分預覽，不呼叫 Embedding / 不寫入
  }

必要環境變數：
  MONGO_URI          MongoDB 連線字串
  QDRANT_URL         例如 http://<EC2 私有 IP 或域名>:6333
  OPENAI_API_KEY     OpenAI API key
選用環境變數：
  MONGO_DB           預設 stock_insight
  QDRANT_API_KEY     Qdrant 對外開放時務必設定
  SYNC_SINCE_HOURS   預設 24
  SYNC_LIMIT         預設 500
  FASTEMBED_CACHE_PATH  預設 /opt/fastembed_cache（Dockerfile 預載位置）
"""

import os
import time
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from openai import AsyncOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ─── 環境配置 ──────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "stock_insight")
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
DEFAULT_SINCE_HOURS = int(os.getenv("SYNC_SINCE_HOURS", "24"))
DEFAULT_LIMIT = int(os.getenv("SYNC_LIMIT", "500"))
FASTEMBED_CACHE_PATH = os.getenv("FASTEMBED_CACHE_PATH", "/opt/fastembed_cache")

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# BM25 模型跨 warm invocation 重用（與 event loop 無關，可放模組層級）
_SPARSE_MODEL = None


def _get_sparse_embedder():
    global _SPARSE_MODEL
    if _SPARSE_MODEL is None:
        from fastembed import SparseTextEmbedding

        cache_dir = FASTEMBED_CACHE_PATH
        if not os.path.isdir(cache_dir):
            # 映像內未預載時退回 /tmp（Lambda 唯一可寫路徑），冷啟動時會下載模型
            cache_dir = "/tmp/fastembed_cache"
            os.makedirs(cache_dir, exist_ok=True)
        _SPARSE_MODEL = SparseTextEmbedding(model_name="Qdrant/bm25", cache_dir=cache_dir)
    return _SPARSE_MODEL


def batch_sparse_vectors(texts: List[str], batch_size: int = 48) -> List[models.SparseVector]:
    """以 FastEmbed BM25 產生與 Qdrant sparse IDF index 相容的 passage 向量。"""
    model = _get_sparse_embedder()
    out: List[models.SparseVector] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        for emb in model.passage_embed(batch):
            out.append(
                models.SparseVector(
                    indices=emb.indices.tolist(),
                    values=emb.values.tolist(),
                )
            )
    if len(out) != len(texts):
        raise RuntimeError(f"sparse 向量筆數不符：{len(out)} vs {len(texts)}")
    return out


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
    return datetime.fromtimestamp(unix_ts, tz=TAIPEI_TZ).isoformat()


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


async def batch_embed(
    ai_client: AsyncOpenAI,
    texts: List[str],
    batch_size: int = 256,
    max_retries: int = 3,
) -> List[List[float]]:
    """批次產生 embeddings，帶指數退避重試；重試耗盡直接 raise 讓 Lambda 失敗。"""
    all_embeddings: List[List[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        for attempt in range(max_retries):
            try:
                response = await ai_client.embeddings.create(
                    input=batch,
                    model=EMBEDDING_MODEL,
                )
                all_embeddings.extend([item.embedding for item in response.data])
                break
            except Exception as e:
                wait_time = 2 ** attempt
                print(f"Embedding batch {i // batch_size} failed (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    raise RuntimeError(
                        f"Embedding batch {i // batch_size} 重試 {max_retries} 次仍失敗"
                    ) from e

    return all_embeddings


# ═══════════════════════════════════════════════════════════
# News Collection 切分邏輯（與 migrate_to_qdrant.py 一致）
# ═══════════════════════════════════════════════════════════

def chunk_news_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    對單篇新聞文件做語意切分。
    短文 (< 800 字) 直接當單一 chunk，長文使用 RecursiveCharacterTextSplitter。
    回傳格式：[{"text": ..., "payload": {...}}, ...]
    """
    title = doc.get("title") or "無標題"
    content = doc.get("content") or ""
    mongo_id = str(doc["_id"])

    if not content.strip():
        return []

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
# AI_news_analysis Collection 切分邏輯（與 migrate_to_qdrant.py 一致）
# ═══════════════════════════════════════════════════════════

def chunk_ai_analysis_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    對單篇 AI 分析報告按欄位角色拆分。
    產出最多 3 個 chunk：summary / key_news / stock_insight
    """
    mongo_id = str(doc["_id"])
    title = doc.get("article_title") or "無標題"
    publish_at = transform_timestamp(doc.get("publishAt", int(time.time())))
    sentiment_raw = doc.get("sentiment") or ""

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
        # 部分文件的 source_news 欄位為 bool 或非 list（資料髒亂），需先正規化
        "source_news_titles": [s.get("title", "") for s in
                                (doc.get("source_news") if isinstance(doc.get("source_news"), list) else [])
                                if isinstance(s, dict)],
        "source_news_ids":    [str(s.get("_id", "")) for s in
                                (doc.get("source_news") if isinstance(doc.get("source_news"), list) else [])
                                if isinstance(s, dict)],
        "collection_type": "ai_analysis",
    }

    results = []
    chunk_idx = 0

    summary = doc.get("summary") or ""
    if summary.strip():
        text = f"[分析摘要] {title}：{summary}"
        results.append({
            "text": text,
            "payload": {**base_payload, "content": text, "chunk_type": "summary", "chunk_idx": chunk_idx},
        })
        chunk_idx += 1

    important_news = doc.get("important_news") or ""
    if important_news.strip():
        text = f"[重要新聞] {title}：{important_news}"
        results.append({
            "text": text,
            "payload": {**base_payload, "content": text, "chunk_type": "key_news", "chunk_idx": chunk_idx},
        })
        chunk_idx += 1

    potential = doc.get("potential_stocks_and_industries") or ""
    if potential.strip():
        text = f"[潛力標的分析] {title}：{potential}"
        results.append({
            "text": text,
            "payload": {**base_payload, "content": text, "chunk_type": "stock_insight", "chunk_idx": chunk_idx},
        })

    return results


# ═══════════════════════════════════════════════════════════
# 同步主程序
# ═══════════════════════════════════════════════════════════

async def sync_collection(
    db,
    qdrant_client: AsyncQdrantClient,
    ai_client: AsyncOpenAI,
    mongo_col_name: str,
    qdrant_col_name: str,
    chunk_fn,
    since_hours: int,
    limit: int,
    embedding_batch_size: int = 256,
    upsert_batch_size: int = 100,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    1. 從 MongoDB 讀取指定時間區間內的新文件
    2. 切分 → 批次 Embedding → 批次 Upsert 到 Qdrant
    冪等：point id 為確定性 UUID，重跑同一區間只會覆寫相同向量。
    """
    print(f"=== 同步 {mongo_col_name} → {qdrant_col_name} "
          f"(since_hours={since_hours}, limit={limit}, dry_run={dry_run}) ===")

    if not dry_run and not await qdrant_client.collection_exists(qdrant_col_name):
        raise RuntimeError(
            f"Qdrant collection '{qdrant_col_name}' 不存在，"
            f"請先在 EC2 上執行 setup_qdrant.py 建立 collection 與索引"
        )

    # Step 1: 以 ObjectId 時間戳做增量過濾
    query: Dict[str, Any] = {}
    if since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        query["_id"] = {"$gt": ObjectId.from_datetime(cutoff)}

    cursor = db[mongo_col_name].find(query).sort("_id", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    print(f"從 MongoDB 讀取到 {len(docs)} 筆文件")

    if not docs:
        return {"docs": 0, "chunks": 0, "points": 0}

    # Step 2: 切分
    all_chunks: List[Dict[str, Any]] = []
    for doc in docs:
        all_chunks.extend(chunk_fn(doc))
    print(f"總共產生 {len(all_chunks)} 個 chunks")

    if not all_chunks:
        return {"docs": len(docs), "chunks": 0, "points": 0}

    if dry_run:
        for i, chunk in enumerate(all_chunks[:3]):
            print(f"--- Chunk {i} | {chunk['payload'].get('chunk_type')} | "
                  f"{chunk['payload']['title']} ---")
            print(f"  text (前200字): {chunk['text'][:200]}")
        return {"docs": len(docs), "chunks": len(all_chunks), "points": 0, "dry_run": True}

    # Step 3: 批次 Embedding (dense + sparse)
    texts_to_embed = [c["text"] for c in all_chunks]
    embeddings = await batch_embed(ai_client, texts_to_embed, batch_size=embedding_batch_size)
    print("Dense embedding 完成")
    sparse_vectors = batch_sparse_vectors(texts_to_embed, batch_size=48)
    print("Sparse (BM25) 完成")

    # Step 4: 組裝 Points 並批次 Upsert
    points: List[models.PointStruct] = []
    for chunk, embedding, sparse_vec in zip(all_chunks, embeddings, sparse_vectors):
        payload = chunk["payload"]
        point_id = generate_deterministic_uuid(
            payload["mongo_id"], payload.get("chunk_type", "unknown"), payload.get("chunk_idx", 0)
        )
        points.append(models.PointStruct(
            id=point_id,
            vector={"dense": embedding, "text": sparse_vec},
            payload=payload,
        ))

    for i in range(0, len(points), upsert_batch_size):
        batch = points[i:i + upsert_batch_size]
        await qdrant_client.upsert(collection_name=qdrant_col_name, points=batch, wait=True)
        print(f"Upsert batch {i // upsert_batch_size + 1}: {len(batch)} points")

    print(f"完成: docs={len(docs)} chunks={len(all_chunks)} points={len(points)}")
    return {"docs": len(docs), "chunks": len(all_chunks), "points": len(points)}


async def _run(event: Dict[str, Any]) -> Dict[str, Any]:
    collection = event.get("collection", "all")
    since_hours = int(event.get("since_hours", DEFAULT_SINCE_HOURS))
    limit = int(event.get("limit", DEFAULT_LIMIT))
    dry_run = bool(event.get("dry_run", False))

    # 客戶端必須在當前 event loop 內建立（motor/httpx 綁定 loop，
    # 放模組層級會在 warm invocation 的新 loop 上炸 "attached to a different loop"）
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[MONGO_DB]
    qdrant_client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=60)
    ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    targets = []
    if collection in ("all", "news"):
        targets.append(("news", "news", chunk_news_document))
    if collection in ("all", "ai_analysis"):
        targets.append(("AI_news_analysis", "ai_analysis", chunk_ai_analysis_document))
    if not targets:
        raise ValueError(f"未知的 collection: {collection}")

    results: Dict[str, Any] = {}
    try:
        for mongo_col, qdrant_col, chunk_fn in targets:
            results[qdrant_col] = await sync_collection(
                db=db,
                qdrant_client=qdrant_client,
                ai_client=ai_client,
                mongo_col_name=mongo_col,
                qdrant_col_name=qdrant_col,
                chunk_fn=chunk_fn,
                since_hours=since_hours,
                limit=limit,
                dry_run=dry_run,
            )
    finally:
        mongo_client.close()
        await qdrant_client.close()
        await ai_client.close()

    return results


def lambda_handler(event, context):
    missing = [name for name, val in
               [("MONGO_URI", MONGO_URI), ("QDRANT_URL", QDRANT_URL), ("OPENAI_API_KEY", OPENAI_API_KEY)]
               if not val]
    if missing:
        raise RuntimeError(f"缺少必要環境變數: {', '.join(missing)}")

    start = time.time()
    results = asyncio.run(_run(event or {}))
    elapsed = round(time.time() - start, 1)
    print(f"全部同步完成，耗時 {elapsed}s: {results}")
    return {"ok": True, "elapsed_seconds": elapsed, "results": results}


if __name__ == "__main__":
    # 本地測試：python lambda_function.py（讀取當前環境變數）
    print(lambda_handler({"dry_run": True, "since_hours": 24, "limit": 10}, None))
