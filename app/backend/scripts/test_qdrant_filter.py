import os
import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

# 配置
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

async def test_search_with_filters():
    client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    
    # 測試條件: 
    # 1. 時間大於 2026-03-30 (上週一)
    # 2. 產業包含 "能源"
    # 3. 情緒為 "negative"
    # 4. 使用 Group By 功能聚合 Chunks
    
    # 建構過濾器
    filter_condition = models.Filter(
        must=[
            models.FieldCondition(
                key="publishAt",
                range=models.DatetimeRange(
                    gt="2026-03-30T00:00:00+08:00"
                )
            ),
            models.FieldCondition(
                key="industry_list",
                match=models.MatchValue(value="能源")
            ),
            models.FieldCondition(
                key="sentiment",
                match=models.MatchValue(value="negative")
            )
        ]
    )
    
    print("🔍 執行帶過濾條件的混合搜尋 (含 Group By mongo_id)...")
    
    try:
        # 使用 search_groups 確保結果不會出現重複文章的不同片段
        result = await client.search_groups(
            collection_name="ai_analysis",
            query_vector=[0.1] * 1536, # 此處為示範用向量，實際應傳入 query embedding
            group_by="mongo_id",
            group_size=1, # 每個 mongo_id 只取一筆最相關的段落
            query_filter=filter_condition,
            limit=5
        )
        
        print(f"✅ 找到 {len(result.groups)} 筆不重複的聚合結果：")
        for group in result.groups:
            # 取出該組中分數最高的點
            top_hit = group.hits[0]
            print(f"- [ID: {group.hits[0].payload['mongo_id']}] {group.hits[0].payload['title']} (Score: {top_hit.score})")
            
    except Exception as e:
        print(f"❌ 搜尋測試失敗: {e}")
        # 如果是 Collection 為空導致的錯誤，也算環境檢測的一環
    
    await client.close()

if __name__ == "__main__":
    asyncio.run(test_search_with_filters())
