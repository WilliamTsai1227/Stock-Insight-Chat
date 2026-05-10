"""
test_news.py
=============
診斷腳本：排查「瀚荃」、「奇鋐」等股票在新聞搜尋中找不到的問題。

測試三個層面：
  1. MongoDB 原始資料是否存在
  2. Qdrant payload filter (stock_names / keywords) 能否命中
  3. 不帶 filter 的純向量搜尋，驗證資料是否已向量化

執行方式：
  cd /Users/william/Documents/project/Stock-Insight-Chat
  python -m test.backend.test_news
  # 或
  python test/backend/test_news.py
"""

import asyncio
import os
import sys

# ── 讓 import app.backend.* 可以找到模組 ───────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models
from openai import AsyncOpenAI

# ── 環境變數 ───────────────────────────────────────────────
MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB", "stock_insight")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 要診斷的關鍵字，可以改成其他股票名稱
TARGET_KEYWORD = "瀚荃"

# ── 初始化客戶端 ───────────────────────────────────────────
mongo_client  = AsyncIOMotorClient(MONGO_URI)
db            = mongo_client[MONGO_DB]
qdrant_client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ═══════════════════════════════════════════════════════════
# Test 1：MongoDB 原始資料查詢
# ═══════════════════════════════════════════════════════════

async def test_mongodb_exists(keyword: str = TARGET_KEYWORD):
    """
    直接查 MongoDB 原始 news collection，
    確認是否有標題、keyword 欄位或 market.name 包含目標關鍵字的文件。
    """
    print(f"\n{'='*60}")
    print(f"[Test 1] MongoDB 原始資料查詢 (keyword='{keyword}')")
    print(f"{'='*60}")

    query = {
        "$or": [
            {"market.name": keyword},
            {"keyword":     keyword},
            {"title":       {"$regex": keyword}},
            {"content":     {"$regex": keyword}},
        ]
    }

    # findOne：只取一筆看欄位結構
    doc = await db.news.find_one(query)

    if doc is None:
        print(f"  ❌ MongoDB 中找不到任何包含「{keyword}」的文件！")
        print(f"     → 問題根源：資料本身不存在，需要先確認爬蟲是否有抓到此股票的新聞。")
        return

    print(f"  ✅ 找到文件！以下是該文件的關鍵欄位：")
    print(f"     _id      : {doc.get('_id')}")
    print(f"     title    : {doc.get('title', '(無)')}")
    print(f"     type     : {doc.get('type', '(無)')}")
    print(f"     keyword  : {doc.get('keyword', '(無)')}")
    print(f"     market   : {doc.get('market', '(無)')}")
    print(f"     stock    : {doc.get('stock', '(無)')}")
    print(f"     source   : {doc.get('source', '(無)')}")
    print(f"     publishAt: {doc.get('publishAt', '(無)')}")
    print(f"     content  : {doc.get('content', '(無)')}")

    # 計算總共有幾筆
    count = await db.news.count_documents(query)
    print(f"\n  📊 MongoDB 中共有 {count} 篇包含「{keyword}」的新聞文件。")
    print(f"\n  ⚠️  提醒：如果 market 欄位結構不是 [{{'name': '...', ...}}] 的格式，")
    print(f"     migrate_to_qdrant.py 的 stock_names 就會是空陣列，造成 Qdrant payload filter 失效。")


# ═══════════════════════════════════════════════════════════
# Test 2：Qdrant Payload Filter 查詢
# ═══════════════════════════════════════════════════════════

async def test_qdrant_payload_filter(keyword: str = TARGET_KEYWORD):
    """
    用 should filter (stock_names OR keywords) 查詢 Qdrant news collection，
    確認向量化後的 payload 欄位是否正確帶入關鍵字。
    """
    print(f"\n{'='*60}")
    print(f"[Test 2] Qdrant Payload Filter 查詢 (keyword='{keyword}')")
    print(f"{'='*60}")

    search_filter = models.Filter(
        should=[
            models.FieldCondition(
                key="stock_names",
                match=models.MatchValue(value=keyword),
            ),
            models.FieldCondition(
                key="keywords",
                match=models.MatchValue(value=keyword),
            ),
        ]
    )

    try:
        result, _next_offset = await qdrant_client.scroll(
            collection_name="news",
            scroll_filter=search_filter,
            limit=5,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print(f"  ❌ Qdrant scroll 發生錯誤：{e}")
        return

    if not result:
        print(f"  ❌ Qdrant payload filter 找不到任何命中「{keyword}」的 point！")
        print(f"     → 可能原因：")
        print(f"       (a) MongoDB 的 market.name 欄位格式不符，stock_names 寫入為空陣列。")
        print(f"       (b) MongoDB 的 keyword 欄位格式不符，keywords 寫入為空陣列。")
        print(f"       (c) 此股票的新聞尚未被 migrate_to_qdrant.py 遷移。")
        return

    print(f"  ✅ 找到 {len(result)} 個命中的 Qdrant points，顯示前 {len(result)} 筆：")
    for i, point in enumerate(result, 1):
        payload = point.payload or {}
        print(f"\n  ── Point {i} (id={point.id}) ──")
        print(f"     title      : {payload.get('title', '(無)')}")
        print(f"     stock_names: {payload.get('stock_names', [])}")
        print(f"     keywords   : {payload.get('keywords', [])}")
        print(f"     chunk_type : {payload.get('chunk_type', '(無)')}")
        print(f"     publishAt  : {payload.get('publishAt', '(無)')}")
        print(f"     content 前 80 字: {str(payload.get('content', ''))[:80]}...")


# ═══════════════════════════════════════════════════════════
# Test 3：Collection Schema 檢查 + 純向量搜尋
# ═══════════════════════════════════════════════════════════

async def test_qdrant_pure_vector_search(keyword: str = TARGET_KEYWORD):
    """
    Step A：先列出 news collection 的向量 schema，確認 named vector 是否正確。
    Step B：用 OpenAI text-embedding-3-small 做純 dense 向量搜尋（無 filter）。
            若 'dense' named vector 不存在，則嘗試 unnamed（舊版）vector。
    """
    print(f"\n{'='*60}")
    print(f"[Test 3] Collection Schema 檢查 + 純向量搜尋 (keyword='{keyword}')")
    print(f"{'='*60}")

    # ── Step A：檢查 collection 的向量 schema ──────────────
    print(f"\n  📋 Step A：檢查 Qdrant news collection 向量 schema")
    try:
        info = await qdrant_client.get_collection("news")
        vectors_config = info.config.params.vectors

        # ── Dense vectors（在 params.vectors）──
        if isinstance(vectors_config, dict):
            print(f"  ✅ Dense Named vectors 存在，共 {len(vectors_config)} 組：")
            for name, cfg in vectors_config.items():
                print(f"     • '{name}' → size={getattr(cfg, 'size', '?')}, "
                      f"distance={getattr(cfg, 'distance', '?')}")
            has_dense = "dense" in vectors_config
        else:
            # 舊版 unnamed single vector
            size = getattr(vectors_config, 'size', '?')
            print(f"  ⚠️  舊版 unnamed vector（size={size}），不支援 Hybrid 搜尋！")
            has_dense = False

        # ── Sparse vectors（在 params.sparse_vectors，與 dense 是不同屬性！）──
        sparse_vectors_config = getattr(info.config.params, "sparse_vectors", None) or {}
        has_text = "text" in sparse_vectors_config
        if sparse_vectors_config:
            print(f"  ✅ Sparse vectors 存在，共 {len(sparse_vectors_config)} 組：")
            for name in sparse_vectors_config:
                print(f"     • '{name}' (BM25 sparse)")
        else:
            print(f"  ⚠️  未發現任何 sparse vector 設定")

        print(f"\n  {'✅' if has_dense else '❌'}  'dense' named vector : {'存在' if has_dense else '不存在'}")
        print(f"  {'✅' if has_text  else '❌'}  'text'  sparse vector: {'存在' if has_text  else '不存在'}")

        if not has_dense:
            print(f"\n  ⚠️  dense schema 不完整！需要執行 setup_qdrant.py --reset 並重新遷移")
        if not has_text:
            print(f"\n  ⚠️  sparse schema 不完整！需要執行 setup_qdrant.py --reset 並重新遷移")

        points_count = info.points_count
        print(f"\n  📊 collection 目前共有 {points_count} 個 points")

    except Exception as e:
        print(f"  ❌ 取得 collection info 失敗：{e}")
        return

    # ── Step B：產生 embedding 並做純向量搜尋 ──────────────
    print(f"\n  🧠 Step B：純向量搜尋（無 filter）")
    print(f"  正在呼叫 OpenAI embedding API...")
    try:
        response = await openai_client.embeddings.create(
            input=keyword,
            model="text-embedding-3-small",
        )
        query_vector = response.data[0].embedding
        print(f"  ✅ Embedding 產生完畢，維度：{len(query_vector)}")
    except Exception as e:
        print(f"  ❌ OpenAI embedding 失敗：{e}")
        return

    # 先嘗試 named 'dense' vector，失敗則 fallback 到 unnamed vector
    for attempt, using_param in enumerate([{"using": "dense"}, {}], 1):
        label = "'dense' named vector" if attempt == 1 else "unnamed（舊版）vector"
        try:
            results = await qdrant_client.query_points(
                collection_name="news",
                query=query_vector,
                limit=5,
                with_payload=True,
                **using_param,
            )
            points = results.points
            print(f"  ✅ 使用 {label} 搜尋成功，命中 {len(points)} 筆")
            break
        except Exception as e:
            print(f"  ❌ 使用 {label} 搜尋失敗：{e}")
            if attempt == 2:
                print(f"  → 兩種 vector 都無法搜尋，collection 可能有問題。")
                return

    if not points:
        print(f"  ❌ 純向量搜尋回傳 0 筆，collection 可能是空的。")
        return

    print(f"\n  Top-{len(points)} 結果：")
    for i, point in enumerate(points, 1):
        payload = point.payload or {}
        print(f"\n  ── Rank {i}（score={point.score:.4f}）──")
        print(f"     title      : {payload.get('title', '(無)')}")
        print(f"     stock_names: {payload.get('stock_names', [])}")
        print(f"     keywords   : {payload.get('keywords', [])}")
        print(f"     chunk_type : {payload.get('chunk_type', '(無)')}")
        print(f"     publishAt  : {payload.get('publishAt', '(無)')}")
        print(f"     content 前 80 字: {str(payload.get('content', ''))[:80]}...")

    top_titles = [p.payload.get("title", "") for p in points if p.payload]
    has_match = any(keyword in t for t in top_titles)
    print(f"\n  📊 Top-5 中{'有' if has_match else '沒有'}標題包含「{keyword}」的結果。")
    if has_match:
        print(f"     → 向量可以找到！若 Test 2 失敗，問題在 payload filter。")
    else:
        print(f"     → 向量搜尋結果中無直接相關新聞（股票僅出現在 content 中，非標題）。")



# ═══════════════════════════════════════════════════════════
# Test 4：純 Sparse (BM25) 搜尋
# ═══════════════════════════════════════════════════════════

async def test_qdrant_sparse_search(keyword: str = TARGET_KEYWORD):
    """
    用 FastEmbed BM25 模型把關鍵字轉成 sparse vector，
    對 Qdrant news collection 做純 BM25 搜尋（不帶 filter）。
    與 Test 3 對比：看 sparse 關鍵字匹配是否比 dense 語意更能找到精確股票名稱。
    """
    print(f"\n{'='*60}")
    print(f"[Test 4] 純 Sparse (BM25) 搜尋（無 filter）(keyword='{keyword}')")
    print(f"{'='*60}")

    from app.backend.tools.qdrant_hybrid import embed_sparse_query
    import asyncio as _asyncio

    print(f"  🔤 正在產生 BM25 sparse vector...")
    try:
        sparse_vec = await _asyncio.to_thread(embed_sparse_query, keyword)
        print(f"  ✅ Sparse vector 產生完畢，非零維度數：{len(sparse_vec.indices)}")
    except Exception as e:
        print(f"  ❌ BM25 embed 失敗：{e}")
        return

    try:
        results = await qdrant_client.query_points(
            collection_name="news",
            query=sparse_vec,
            using="text",          # sparse vector 通道名稱
            limit=5,
            with_payload=True,
        )
        points = results.points
    except Exception as e:
        print(f"  ❌ Qdrant sparse 搜尋失敗：{e}")
        return

    if not points:
        print(f"  ❌ BM25 搜尋回傳 0 筆。")
        return

    print(f"  ✅ BM25 搜尋命中 {len(points)} 筆：")
    for i, point in enumerate(points, 1):
        payload = point.payload or {}
        in_content = keyword in str(payload.get("content", ""))
        in_title   = keyword in str(payload.get("title", ""))
        tag = "📌標題" if in_title else ("📄內文" if in_content else "")
        print(f"\n  ── Rank {i}（score={point.score:.4f}）{tag} ──")
        print(f"     title      : {payload.get('title', '(無)')}")
        print(f"     stock_names: {payload.get('stock_names', [])}")
        print(f"     keywords   : {payload.get('keywords', [])}")
        print(f"     chunk_type : {payload.get('chunk_type', '(無)')}")
        print(f"     publishAt  : {payload.get('publishAt', '(無)')}")
        print(f"     content 前 80 字: {str(payload.get('content', ''))[:80]}...")

    matched = sum(1 for p in points if p.payload and keyword in str(p.payload.get("content", "")))
    print(f"\n  📊 Top-5 中有 {matched} 筆 content 包含「{keyword}」。")


# ═══════════════════════════════════════════════════════════
# Test 5：Hybrid Search (Dense + Sparse RRF)
# ═══════════════════════════════════════════════════════════

async def test_qdrant_hybrid_search(keyword: str = TARGET_KEYWORD):
    """
    同時使用 Dense (OpenAI) 和 Sparse (BM25) 做 Hybrid RRF 搜尋，
    並依 mongo_id 分組（group_size=5）。
    與 Test 3 / Test 4 對比，驗證 Hybrid 是否確實比單一搜尋更好。
    """
    print(f"\n{'='*60}")
    print(f"[Test 5] Hybrid RRF 搜尋 + Grouping（無 filter）(keyword='{keyword}')")
    print(f"{'='*60}")

    from app.backend.tools.qdrant_hybrid import hybrid_rrf_grouped, embed_sparse_query
    import asyncio as _asyncio

    # Step 1: Dense embedding
    print(f"  🧠 產生 Dense embedding...")
    try:
        resp = await openai_client.embeddings.create(
            input=keyword,
            model="text-embedding-3-small",
        )
        query_dense = resp.data[0].embedding
        print(f"  ✅ Dense vector 維度：{len(query_dense)}")
    except Exception as e:
        print(f"  ❌ Dense embedding 失敗：{e}")
        return

    # Step 2: Sparse embedding
    print(f"  🔤 產生 Sparse BM25 vector...")
    try:
        sparse_vec = await _asyncio.to_thread(embed_sparse_query, keyword)
        print(f"  ✅ Sparse vector 非零維度：{len(sparse_vec.indices)}")
    except Exception as e:
        print(f"  ❌ Sparse embed 失敗：{e}")
        return

    # Step 3: Hybrid RRF + Grouping
    try:
        grouped = await hybrid_rrf_grouped(
            qdrant_client,
            "news",
            query_dense,
            sparse_vec,
            query_filter=None,       # 無 filter，純向量召回
            group_by_payload_key="mongo_id",
            group_size=5,
            top_k=10,
            score_threshold=None,
        )
    except Exception as e:
        print(f"  ❌ Hybrid RRF 搜尋失敗：{e}")
        return

    if not grouped:
        print(f"  ❌ Hybrid 搜尋回傳 0 組。")
        return

    print(f"  ✅ Hybrid 搜尋共回傳 {len(grouped)} 篇新聞（每篇最多 5 個 chunks）：")
    for rank, (mongo_id, hits) in enumerate(grouped, 1):
        first = hits[0].payload or {}
        combined = "\n".join((h.payload or {}).get("content", "") for h in hits)
        in_combined = keyword in combined
        print(f"\n  ── Rank {rank}（best_score={hits[0].score:.4f}，chunks={len(hits)}）"
              f"{'📌 keyword 出現在內文' if in_combined else ''} ──")
        print(f"     title      : {first.get('title', '(無)')}")
        print(f"     mongo_id   : {mongo_id}")
        print(f"     stock_names: {first.get('stock_names', [])}")
        print(f"     keywords   : {first.get('keywords', [])}")
        print(f"     publishAt  : {first.get('publishAt', '(無)')}")
        print(f"     合併內文前 120 字: {combined[:120]}...")

    found = sum(1 for _, hits in grouped
                if keyword in "\n".join((h.payload or {}).get("content", "") for h in hits))
    print(f"\n  📊 共 {found}/{len(grouped)} 篇新聞的合併內文包含「{keyword}」。")
    print(f"  💡 對比 Test 3 (Dense) 與 Test 4 (Sparse)，Hybrid 找到的新聞是否更相關？")


# ═══════════════════════════════════════════════════════════
# 主程式
# ═══════════════════════════════════════════════════════════

async def main():
    await test_mongodb_exists(TARGET_KEYWORD)
    await test_qdrant_payload_filter(TARGET_KEYWORD)
    await test_qdrant_pure_vector_search(TARGET_KEYWORD)   # Test 3: Dense only
    await test_qdrant_sparse_search(TARGET_KEYWORD)         # Test 4: Sparse only
    await test_qdrant_hybrid_search(TARGET_KEYWORD)         # Test 5: Hybrid RRF

    print(f"\n{'='*60}")
    print("✅ 全部診斷測試完成")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())

