"""
Qdrant Collection 初始化工具 v2
================================
配合 migrate_to_qdrant.py v2 的新 metadata 設計，
建立 collection 及所有必要的 payload indexes。
"""

import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models

# 載入 .env 環境變數
load_dotenv()

# 配置 Qdrant 連線
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

# 指定向量維度為 1536 (OpenAI text-embedding-3-small)
VECTOR_SIZE = 1536

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

# ─── Collection 定義 ────────────────────────────────────────

COLLECTION_DEFINITIONS = {
    "news": {
        "datetime_indexes": ["publishAt"],
        "keyword_indexes": [
            "source",           # 新聞來源: anue, cnyes, etc.
            "category",         # 分類: headline, tw_stock, etc.
            "type",             # 台股新聞 / 國際新聞
            "stock_codes",      # 相關股票代碼: ["3017", "2330", ...]
            "stock_names",      # 相關股票名稱: ["奇鋐", "台積電", ...]
            "keywords",         # 新聞關鍵字
            "collection_type",  # 固定為 "news"
            "chunk_type",       # "full" or "partial"
        ],
        "integer_indexes": [
            "chunk_idx",
            "total_chunks",
        ],
    },
    "ai_analysis": {
        "datetime_indexes": ["publishAt"],
        "keyword_indexes": [
            "sentiment_label",  # positive / negative / neutral
            "industry_list",    # 產業標籤
            "category",         # headline, etc.
            "chunk_type",       # summary / key_news / stock_insight
            "collection_type",  # 固定為 "ai_analysis"
        ],
        "bool_indexes": [
            "is_summary",       # 是否為彙總報告
        ],
        "integer_indexes": [
            "analysis_batch",
            "chunk_idx",
        ],
        "text_indexes": [
            "mongo_id",         # 用於 group_by 聚合
        ],
    },
}


def setup_collections():
    """建立 Collection 與所有 payload indexes"""

    for collection_name, config in COLLECTION_DEFINITIONS.items():
        print(f"\n{'─'*50}")
        print(f"📦 設定 Collection: {collection_name}")
        print(f"{'─'*50}")

        # 1. 建立 Collection
        try:
            if client.collection_exists(collection_name):
                print(f"  ⚠️  Collection '{collection_name}' 已存在，跳過建立")
            else:
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=VECTOR_SIZE,
                        distance=models.Distance.COSINE
                    )
                )
                print(f"  ✅ 建立 Collection: {collection_name}")
        except Exception as e:
            print(f"  ❌ 建立 Collection 失敗: {e}")
            continue

        # 2. 建立 Datetime Indexes
        for field in config.get("datetime_indexes", []):
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.DATETIME
                )
                print(f"  📅 Datetime index: {field}")
            except Exception as e:
                print(f"  ⚠️  Datetime index '{field}' 可能已存在: {e}")

        # 3. 建立 Keyword Indexes
        for field in config.get("keyword_indexes", []):
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                print(f"  🏷️  Keyword index: {field}")
            except Exception as e:
                print(f"  ⚠️  Keyword index '{field}' 可能已存在: {e}")

        # 4. 建立 Integer Indexes
        for field in config.get("integer_indexes", []):
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.INTEGER
                )
                print(f"  🔢 Integer index: {field}")
            except Exception as e:
                print(f"  ⚠️  Integer index '{field}' 可能已存在: {e}")

        # 5. 建立 Bool Indexes
        for field in config.get("bool_indexes", []):
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.BOOL
                )
                print(f"  ✓  Bool index: {field}")
            except Exception as e:
                print(f"  ⚠️  Bool index '{field}' 可能已存在: {e}")

        # 6. 建立 Text Indexes (用於 group_by 等文字型操作)
        for field in config.get("text_indexes", []):
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                print(f"  📝 Text/Keyword index: {field}")
            except Exception as e:
                print(f"  ⚠️  Text index '{field}' 可能已存在: {e}")

        print(f"  ✨ {collection_name} 索引設定完成")

    print(f"\n{'='*50}")
    print(f"🏁 所有 Collection 設定完成！")
    print(f"{'='*50}")


def reset_collections():
    """危險操作：刪除並重建所有 collection"""
    print("⚠️  正在刪除所有 Collection...")
    for name in COLLECTION_DEFINITIONS:
        try:
            if client.collection_exists(name):
                client.delete_collection(name)
                print(f"  🗑️  已刪除: {name}")
        except Exception as e:
            print(f"  ❌ 刪除 {name} 失敗: {e}")

    print("\n重新建立...")
    setup_collections()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Qdrant Collection 初始化工具")
    parser.add_argument("--reset", action="store_true",
                        help="刪除並重建所有 Collection (危險操作)")
    args = parser.parse_args()

    if args.reset:
        confirm = input("⚠️  確定要刪除所有 Collection 並重建嗎？(y/N): ")
        if confirm.lower() == "y":
            reset_collections()
        else:
            print("已取消")
    else:
        setup_collections()
