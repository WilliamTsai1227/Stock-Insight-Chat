import pytest
import os
from openai import OpenAI
from app.backend.tools.ai_analysis import search_ai_analysis, get_full_ai_analysis
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
async def test_search_ai_analysis_basic(real_embedding):
    """測試 AI 分析報告混合搜尋功能"""
    query = "能源產業的未來展望是什麼？"
    embedding = real_embedding(query)
    chat_id = "test_ai_001"
    
    # 執行搜尋
    result = await search_ai_analysis(
        query=query,
        query_embedding=embedding,
        chat_id=chat_id,
        top_k=5
    )
    
    # 驗證結構
    assert "context" in result
    assert result["chat_id"] == chat_id
    
    if len(result["context"]) > 0:
        print(f"\n✅ 成功搜尋到 {len(result['context'])} 則 AI 分析片段")
        for idx, item in enumerate(result["context"]):
            assert "title" in item
            assert "content" in item
            snippet = item['content'][:100].replace('\n', ' ')
            print(f"   [{idx+1}] {item['title']}")
            print(f"       >>> 內容片段: {snippet}...")
    else:
        print("\n⚠️ AI 分析搜尋結果為空，請確認 Qdrant 的 ai_analysis collection 中是否有資料")

@pytest.mark.asyncio
async def test_get_full_ai_analysis_flow(real_embedding):
    """測試從 MongoDB 獲取 AI 分析全文的功能"""
    query = "半導體"
    embedding = real_embedding(query)
    search_res = await search_ai_analysis(query, embedding, "test_flow_ai")
    
    if not search_res["context"] or "mongo_id" not in search_res["context"][0]:
        pytest.skip("Qdrant 中無資料，跳過 AI 全文測試")
        
    mongo_ids = [item["mongo_id"] for item in search_res["context"][:5] if item.get("mongo_id")]
    
    # 2. 用 ID 拿全文 (從 AI_news_analysis 集合)
    full_res = await get_full_ai_analysis(
        ai_analysis_ids=mongo_ids,
        chat_id="test_flow_ai",
        query=query,
        query_embedding=embedding
    )
    
    assert len(full_res["context"]) > 0
    for doc in full_res["context"]:
        assert "mongo_id" in doc
        # 注意：在 AI 分析中 content 代表 summary
        assert "content" in doc 
        print(f"✅ 成功獲取 AI 分析全文 (摘要): {doc['title']}")
