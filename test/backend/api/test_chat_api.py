import sys
import os
# 加入專案路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../app"))) # 新增這一行

from fastapi.testclient import TestClient
from app.backend.app import app  # 修正引用路徑
import json

client = TestClient(app)

def test_get_ai_response():
    print("\n🚀 正在測試 /api/chat/getAIResponse 接口...")
    
    payload = {
        "query": "最近台積電表現如何，有推薦什麼股票嗎？",
        "chat_id": None,  # 測試自動生成 UUID
        "agent_config": {
            "enabled_tools": ["search_stock_news", "get_market_recommendations"]
        }
    }
    
    response = client.post("/api/getAIResponse", json=payload)
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ 請求成功！")
        print(f"🆔 Chat ID: {data['chat_id']}")
        print(f"⏳ 總耗時: {data['total_execution_time']} 秒")
        
        print("\n--- Router 階段 ---")
        print(f"⏱️ 耗時: {data['router_trace']['execution_time']} 秒")
        print(f"🛠️ 調用工具: {[tc['name'] for tc in data['router_trace']['tool_calls']]}")
        
        print("\n--- Analyst 階段 ---")
        print(f"⏱️ 耗時: {data['analyst_trace']['execution_time']} 秒")
        print(f"📝 分析內容摘要: {data['analyst_trace']['content'][:100]}...")
        
        print("\n--- 檢索來源 ---")
        for src in data['retrieval_sources']:
            print(f"📍 工具: {src['tool']} (長度: {src['content_length']})")
    else:
        print(f"❌ 請求失敗: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    test_get_ai_response()
