import pytest
import os
from openai import OpenAI
from app.backend.tools.news import search_news, get_full_news
from dotenv import load_dotenv

load_dotenv()

@pytest.fixture
def openai_client():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@pytest.fixture
def real_embedding(openai_client):
    """取得一個真實的向量以進行精準查詢測試"""
    def _get_embedding(text):
        response = openai_client.embeddings.create(
            input=text,
            model="text-embedding-3-small"
        )
        return response.data[0].embedding
    return _get_embedding

@pytest.mark.asyncio
async def test_search_news_basic(real_embedding):
    """測試新聞混合搜尋功能"""
    # 模擬使用者問題
    query = "台積電最近的營收表現如何？"
    embedding = real_embedding(query)
    chat_id = "test_chat_001"
    
    # 執行搜尋
    result = await search_news(
        query=query,
        query_embedding=embedding,
        chat_id=chat_id,
        top_k=5
    )
    
    # 驗證結構
    assert "context" in result
    assert result["chat_id"] == chat_id
    assert isinstance(result["context"], list)
    
    # 如果已經有資料遷移，驗證是否拿到內容
    if len(result["context"]) > 0:
        print(f"\n✅ 成功搜尋到 {len(result['context'])} 則新聞片段")
        for idx, item in enumerate(result["context"]):
            assert "title" in item
            assert "content" in item
            assert "mongo_id" in item
            snippet = item['content'][:100].replace('\n', ' ')
            print(f"   [{idx+1}] {item['title']}")
            print(f"       >>> 內容片段: {snippet}...")
    else:
        print("\n⚠️ 搜尋結果為空，請確認 Qdrant 中是否有資料")

@pytest.mark.asyncio
async def test_get_full_news_flow(real_embedding):
    """測試從 MongoDB 獲取全文的功能"""
    # 1. 先搜出 ID
    query = "台積電"
    embedding = real_embedding(query)
    search_res = await search_news(query, embedding, "test_flow")
    
    if not search_res["context"] or "mongo_id" not in search_res["context"][0]:
        pytest.skip("Qdrant 中無資料，跳過全文測試")
        
    mongo_ids = [item["mongo_id"] for item in search_res["context"][:5] if item.get("mongo_id")]
    
    # 2. 用 ID 拿全文
    full_res = await get_full_news(
        news_ids=mongo_ids,
        chat_id="test_flow",
        query=query,
        query_embedding=embedding
    )
    
    assert len(full_res["context"]) > 0
    for doc in full_res["context"]:
        assert "content" in doc
        assert len(doc["content"]) > 10 # 確保拿到的是長文本全文
        print(f"✅ 成功獲取全文: {doc['title']}")
