"""
Qdrant 過濾搜尋測試工具 v2
===========================
配合 v2 遷移腳本的新 metadata 設計，
測試各種過濾條件及 search_groups 聚合功能。
"""

import os
import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

# 配置
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))


async def test_news_search():
    """測試 news collection 的過濾搜尋"""
    client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    print("\n" + "=" * 60)
    print("📰 測試 News Collection 搜尋")
    print("=" * 60)

    # 測試 1: 按時間 + 股票代碼過濾
    print("\n--- 測試 1: 時間 + 股票代碼過濾 ---")
    filter_condition = models.Filter(
        must=[
            models.FieldCondition(
                key="publishAt",
                range=models.DatetimeRange(gt="2026-03-01T00:00:00+08:00")
            ),
            models.FieldCondition(
                key="stock_codes",
                match=models.MatchValue(value="2330")
            ),
        ]
    )

    try:
        result = await client.search_groups(
            collection_name="news",
            query_vector=[0.1] * 1536,
            group_by="mongo_id",
            group_size=1,
            query_filter=filter_condition,
            limit=5,
            with_payload=True,
        )
        print(f"✅ 找到 {len(result.groups)} 筆不重複新聞：")
        for group in result.groups:
            hit = group.hits[0]
            p = hit.payload
            print(f"  - [{p.get('type', '')}] {p.get('title', '')}")
            print(f"    股票: {p.get('stock_names', [])} | 關鍵字: {p.get('keywords', [])[:5]}")
            print(f"    chunks: {p.get('chunk_idx')}/{p.get('total_chunks')} | score: {hit.score:.4f}")
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")

    # 測試 2: 按關鍵字過濾
    print("\n--- 測試 2: 按 keywords 過濾 ---")
    filter_condition = models.Filter(
        must=[
            models.FieldCondition(
                key="keywords",
                match=models.MatchValue(value="AI伺服器")
            ),
        ]
    )

    try:
        result = await client.search_groups(
            collection_name="news",
            query_vector=[0.1] * 1536,
            group_by="mongo_id",
            group_size=1,
            query_filter=filter_condition,
            limit=3,
            with_payload=True,
        )
        print(f"✅ 找到 {len(result.groups)} 筆：")
        for group in result.groups:
            p = group.hits[0].payload
            print(f"  - {p.get('title', '')} | source: {p.get('source')}")
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")

    await client.close()


async def test_ai_analysis_search():
    """測試 ai_analysis collection 的過濾搜尋"""
    client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    print("\n" + "=" * 60)
    print("🤖 測試 AI Analysis Collection 搜尋")
    print("=" * 60)

    # 測試 1: 按 chunk_type 過濾 (只搜 summary)
    print("\n--- 測試 1: 只搜尋 summary 類型的 chunk ---")
    filter_condition = models.Filter(
        must=[
            models.FieldCondition(
                key="chunk_type",
                match=models.MatchValue(value="summary")
            ),
        ]
    )

    try:
        result = await client.search(
            collection_name="ai_analysis",
            query_vector=[0.1] * 1536,
            query_filter=filter_condition,
            limit=3,
            with_payload=True,
        )
        print(f"✅ 找到 {len(result)} 筆 summary chunks：")
        for hit in result:
            p = hit.payload
            print(f"  - {p.get('title', '')} | 情緒: {p.get('sentiment_label')}")
            print(f"    內容前100字: {p.get('content', '')[:100]}...")
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")

    # 測試 2: 按 sentiment_label + industry_list 過濾
    print("\n--- 測試 2: 正面情緒 + 特定產業 ---")
    filter_condition = models.Filter(
        must=[
            models.FieldCondition(
                key="sentiment_label",
                match=models.MatchValue(value="positive")
            ),
            models.FieldCondition(
                key="publishAt",
                range=models.DatetimeRange(gt="2026-03-01T00:00:00+08:00")
            ),
        ]
    )

    try:
        result = await client.search_groups(
            collection_name="ai_analysis",
            query_vector=[0.1] * 1536,
            group_by="mongo_id",
            group_size=2,       # 每篇分析取最多 2 個 chunks
            query_filter=filter_condition,
            limit=5,
            with_payload=True,
        )
        print(f"✅ 找到 {len(result.groups)} 筆不重複的分析報告：")
        for group in result.groups:
            first = group.hits[0].payload
            chunk_types = [h.payload.get("chunk_type") for h in group.hits]
            print(f"  - {first.get('title', '')} | 情緒: {first.get('sentiment_label')}")
            print(f"    產業: {first.get('industry_list', [])} | chunk types: {chunk_types}")
            print(f"    來源新聞: {first.get('source_news_titles', [])[:2]}")
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")

    # 測試 3: 只搜 stock_insight (推薦標的)
    print("\n--- 測試 3: 只搜尋 stock_insight 類型 ---")
    filter_condition = models.Filter(
        must=[
            models.FieldCondition(
                key="chunk_type",
                match=models.MatchValue(value="stock_insight")
            ),
        ]
    )

    try:
        result = await client.search(
            collection_name="ai_analysis",
            query_vector=[0.1] * 1536,
            query_filter=filter_condition,
            limit=3,
            with_payload=True,
        )
        print(f"✅ 找到 {len(result)} 筆潛力標的分析：")
        for hit in result:
            p = hit.payload
            print(f"  - {p.get('title', '')} | stock_list: {p.get('stock_list', [])}")
            print(f"    內容: {p.get('content', '')[:150]}...")
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")

    await client.close()


async def test_collection_stats():
    """顯示 collection 統計資訊"""
    client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    print("\n" + "=" * 60)
    print("📊 Collection 統計")
    print("=" * 60)

    for name in ["news", "ai_analysis"]:
        try:
            info = await client.get_collection(name)
            print(f"\n  📦 {name}:")
            print(f"     向量數量: {info.points_count}")
            print(f"     向量維度: {info.config.params.vectors.size}")
            print(f"     索引狀態: {info.status}")
        except Exception as e:
            print(f"  ❌ {name}: {e}")

    await client.close()


async def main():
    await test_collection_stats()
    await test_news_search()
    await test_ai_analysis_search()
    print("\n🏁 所有測試完成！")


if __name__ == "__main__":
    asyncio.run(main())
