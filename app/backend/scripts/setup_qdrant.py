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
# Hybrid：named dense + FastEmbed BM25 sparse（與 app/backend/tools/qdrant_hybrid.py 一致）
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "text"

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


def _expected_payload_index_fields(config: dict) -> list[str]:
    """本腳本應建立的 payload 索引欄位（與 COLLECTION_DEFINITIONS 一致）。"""
    names: list[str] = []
    for key in (
        "datetime_indexes",
        "keyword_indexes",
        "integer_indexes",
        "bool_indexes",
    ):
        names.extend(config.get(key, []))
    names.extend(config.get("text_indexes", []))
    return names


def verify_collection_layout(collection_name: str, config: dict) -> bool:
    """
    讀取 Qdrant 實際 schema，對照本腳本預期（dense + sparse text + payload indexes）。
    回傳是否通過（向量名稱／維度／sparse 皆正確且預期索引欄位皆在 payload_schema）。
    """
    all_ok = True
    print(f"\n  {'─'*46}")
    print(f"  🔍 結構驗證: {collection_name}")
    print(f"  {'─'*46}")

    try:
        info = client.get_collection(collection_name)
    except Exception as e:
        print(f"  ❌ 無法讀取 collection: {e}")
        return False

    params = info.config.params
    points = getattr(info, "points_count", 0)
    status = getattr(info, "status", None)
    print(f"  • status: {status}")
    print(f"  • points_count: {points}")

    # --- Named dense + sparse (Hybrid) ---
    vectors = params.vectors
    dense_ok = False
    dim_ok = False
    if isinstance(vectors, dict):
        d = vectors.get(DENSE_VECTOR_NAME)
        if d is not None:
            dense_ok = True
            sz = getattr(d, "size", None)
            dim_ok = sz == VECTOR_SIZE
            print(
                f"  • dense「{DENSE_VECTOR_NAME}」: "
                f"{'✅' if dim_ok else '❌'} "
                f"size={sz} (預期 {VECTOR_SIZE})"
            )
        else:
            print(f"  ❌ 缺少 named vector「{DENSE_VECTOR_NAME}」")
            all_ok = False
    else:
        print("  ❌ 非 named vectors（舊版單一向量）；Hybrid 需 --reset 重建")
        all_ok = False

    sparse_cfg = getattr(params, "sparse_vectors", None) or {}
    if isinstance(sparse_cfg, dict):
        has_sp = SPARSE_VECTOR_NAME in sparse_cfg
        print(
            f"  • sparse「{SPARSE_VECTOR_NAME}」: "
            f"{'✅ 已設定' if has_sp else '❌ 未設定'}"
        )
        if not has_sp:
            all_ok = False
    else:
        print("  ❌ sparse_vectors 格式異常")
        all_ok = False

    # --- Payload indexes（與腳本預期欄位比對）---
    expected = set(_expected_payload_index_fields(config))
    ps = getattr(info, "payload_schema", None)
    indexed: set[str] = set()
    if isinstance(ps, dict):
        indexed = set(ps.keys())
        print(f"  • payload_schema 欄位數: {len(indexed)}（預期至少 {len(expected)}）")
    else:
        print("  ⚠️  無法讀取 payload_schema（版本或 API 差異）；略過欄位比對")

    if indexed:
        missing = sorted(expected - indexed)
        if missing:
            print(f"  ❌ 缺少索引欄位: {missing}")
            all_ok = False
        else:
            print(f"  ✅ 預期 {len(expected)} 個 payload 索引欄位皆已註冊")
        extra = sorted(indexed - expected)
        if extra:
            print(f"  ℹ️  額外已存在欄位（非本腳本列表）: {extra}")
    elif expected:
        print(
            "  ⚠️  payload_schema 為空，無法比對欄位（新 collection 或未寫入過 points 時"
            "部份版本可能如此）；向量檢查仍有效。"
        )

    if all_ok and dense_ok and dim_ok:
        print(f"  ✅ 小結: {collection_name} Hybrid 結構與索引檢查通過")
    else:
        print(f"  ⚠️  小結: {collection_name} 有項目未通過；若為舊版 collection 可執行 --reset 後再 migrate")
    return all_ok


def setup_collections():
    """建立 Collection 與所有 payload indexes"""

    for collection_name, config in COLLECTION_DEFINITIONS.items():
        print(f"\n{'─'*50}")
        print(f"📦 設定 Collection: {collection_name}")
        print(f"{'─'*50}")

        # 1. 建立 Collection
        try:
            if client.collection_exists(collection_name):
                print(f"  ℹ️  Collection '{collection_name}' 已存在，略過 create_collection")
                print(
                    "      （若要刪除此 collection 並依本腳本重建，請用: "
                    "python3 app/backend/scripts/setup_qdrant.py --reset）"
                )
            else:
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config={
                        DENSE_VECTOR_NAME: models.VectorParams(
                            size=VECTOR_SIZE,
                            distance=models.Distance.COSINE,
                        ),
                    },
                    sparse_vectors_config={
                        SPARSE_VECTOR_NAME: models.SparseVectorParams(
                            modifier=models.Modifier.IDF,
                        ),
                    },
                )
                print(f"  ✅ 建立 Collection (dense+BM25 sparse): {collection_name}")
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

        print(f"  ✨ {collection_name} 索引步驟完成")
        verify_collection_layout(collection_name, config)

    print(f"\n{'='*50}")
    print(f"🏁 所有 Collection 設定完成！（各段「結構驗證」請見上方）")
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
