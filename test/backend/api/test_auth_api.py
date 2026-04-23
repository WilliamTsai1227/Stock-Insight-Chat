import sys
import os
import uuid
import json

# 加入專案路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))

from fastapi.testclient import TestClient
from app.backend.app import app

client = TestClient(app)

def test_auth_workflow():
    print("\n🚀 開始測試 身份驗證 (Auth) 完整流程...")

    # 產生隨機測試帳號
    test_id = str(uuid.uuid4())[:8]
    test_email = f"test_{test_id}@example.com"
    test_username = f"user_{test_id}"
    test_password = "SecurePassword123!"

    # 1. 測試註冊 (Register)
    print(f"\n--- Step 1: 註冊新帳號 ({test_email}) ---")
    reg_payload = {
        "email": test_email,
        "username": test_username,
        "password": test_password
    }
    reg_response = client.post("/api/auth/register", json=reg_payload)
    
    if reg_response.status_code == 201:
        print("✅ 註冊成功！")
        print(f"回傳資料: {reg_response.json()}")
    else:
        print(f"❌ 註冊失敗: {reg_response.status_code}")
        print(reg_response.text)
        return

    # 2. 測試重複註冊 (Duplicate Register)
    print("\n--- Step 2: 測試重複註冊 (預期應失敗) ---")
    dup_response = client.post("/api/auth/register", json=reg_payload)
    if dup_response.status_code == 400:
        print("✅ 成功攔截重複註冊")
    else:
        print(f"⚠️ 攔截失敗，狀態碼: {dup_response.status_code}")

    # 3. 測試登入 (Login)
    print("\n--- Step 3: 測試登入 ---")
    login_payload = {
        "email": test_email,
        "password": test_password
    }
    login_response = client.post("/api/auth/login", json=login_payload)
    
    access_token = None
    if login_response.status_code == 200:
        data = login_response.json()
        access_token = data["access_token"]
        print("✅ 登入成功！")
        print(f"🔑 取得 Access Token: {access_token[:20]}...")
        print(f"👤 使用者資料: {data['user']}")
        
        # 檢查 Cookie 是否包含 refresh_token
        refresh_token_cookie = login_response.cookies.get("refresh_token")
        if refresh_token_cookie:
            print("🍪 成功取得 HttpOnly Refresh Token Cookie")
        else:
            print("❌ 未在 Cookie 中找到 refresh_token")
    else:
        print(f"❌ 登入失敗: {login_response.status_code}")
        print(login_response.text)
        return

    # 4. 測試錯誤密碼登入
    print("\n--- Step 4: 測試錯誤密碼登入 (預期應失敗) ---")
    wrong_payload = {"email": test_email, "password": "wrong_password"}
    wrong_response = client.post("/api/auth/login", json=wrong_payload)
    if wrong_response.status_code == 401:
        print("✅ 成功攔截錯誤密碼")

    # 5. 測試登出 (Logout)
    print("\n--- Step 5: 測試登出 ---")
    # 模擬帶入 Cookie 進行登出
    logout_response = client.post("/api/auth/logout", cookies={"refresh_token": "mock_token"})
    if logout_response.status_code == 200:
        print("✅ 登出成功！")
        # 檢查 Cookie 是否被清除
        if not logout_response.cookies.get("refresh_token"):
            print("🧹 Cookie 已成功標記為刪除")
    else:
        print(f"❌ 登出失敗: {logout_response.status_code}")

if __name__ == "__main__":
    test_auth_workflow()
