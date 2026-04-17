import pytest
from app.backend.tools.ai_analysis import search_recommendations
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

@pytest.mark.asyncio
async def test_search_recommendations():
    print("\n🔍 正在測試推薦工具 (get_market_recommendations)...")
    
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    query_vector = await embeddings.aembed_query("推薦股票、強勢產業、潛力標的、看好板塊")
    
    # 測試寬鬆一點的時間範圍，看看是不是時間太窄
    result = await search_recommendations(
        query_embedding=query_vector,
        start_date="2026-04-01T00:00:00Z",
        end_date="2026-04-18T23:59:59Z",
        top_k=5
    )
    
    if result["stocks"] or result["industries"]:
        print(f"✅ 成功獲取推薦！")
        print(f"📈 推薦股票: {result['stocks']}")
        print(f"🏭 推薦產業: {result['industries']}")
        print(f"📄 來源數量: {len(result['sources'])}")
        for src in result['sources']:
            print(f"   - 標題: {src['title']} | 股票數: {len(src['stocks'])}")
    else:
        print("❌ 推薦搜尋結果為空。")
        print("💡 建議檢查：1. Qdrant 中 ai_analysis 集合是否有資料？ 2. Payload 欄位是否正確？")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_search_recommendations())
