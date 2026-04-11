import os
from qdrant_client import QdrantClient
from qdrant_client.http import models

# 配置 Qdrant 連線 (依照 docker-compose.yml 的配置)
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

# 指定向量維度為 1536 (OpenAI text-embedding-3-small)
VECTOR_SIZE = 1536 

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

def setup_collections():
    definitions = {
        "news": {
            "payload_indexes": ["publishAt", "source", "category", "stock_list"]
        },
        "ai_analysis": {
            "payload_indexes": ["publishAt", "sentiment", "industry_list", "stock_list"]
        }
    }
    
    for collection_name, config in definitions.items():
        # 1. 建立 Collection
        try:
            if client.collection_exists(collection_name):
                print(f"Collection '{collection_name}' already exists. Skipping creation...")
            else:
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=VECTOR_SIZE, 
                        distance=models.Distance.COSINE
                    )
                )
                print(f"✅ Successfully created Collection: {collection_name}")
            
            # 2. 建立 Datetime Index (Qdrant 1.8.0 特點)
            print(f"Creating Datetime Index for '{collection_name}.publishAt'...")
            client.create_payload_index(
                collection_name=collection_name,
                field_name="publishAt",
                field_schema=models.DatetimeIndexParams(
                    type=models.PayloadSchemaType.DATETIME,
                    is_indexed=True
                )
            )

            # 3. 建立 Keyword Index
            for field in config["payload_indexes"]:
                if field == "publishAt": continue # 已建立
                
                print(f"Creating Keyword Index for '{collection_name}.{field}'...")
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
            
            print(f"✨ Indexes initialized for {collection_name}")

        except Exception as e:
            print(f"❌ Error setting up {collection_name}: {e}")

if __name__ == "__main__":
    setup_collections()
