# 🚀 Stock Insight Chat API 規格書 (API Specification)

## 1. API 端點清單 (RESTful Structure)

| 模組 | 功能 | 新路徑 (RESTful) | 方法 | 說明 |
| :--- | :--- | :--- | :---: | :--- |
| **使用者 (User)** | 註冊帳號 | `/api/user/register` | `POST` | 建立新帳號 |
| | 登入 | `/api/user/login` | `POST` | 驗證並取得 Token |
| | 登出 | `/api/user/logout` | `POST` | 作廢 Session |
| | 刷新 Token | `/api/user/refresh` | `POST` | 無感刷新 Access Token |
| | **取得資料** | **`/api/user`** | **`GET`** | 取得當前使用者資訊 |
| | **修改資料** | **`/api/user`** | **`PATCH`** | 修改當前使用者資訊 |
| | **修改密碼** | **`/api/user/password`** | **`PATCH`** | 變更密碼並撤銷所有 Session |
| | **刪除帳號** | **`/api/user`** | **`DELETE`** | 永久註銷帳號 |
| **對話 (Chat)** | 發送訊息 | `/api/chat/messages` | `POST` | 驅動 Agent 進行股市分析 |
| **檔案 (Files)** | 上傳檔案 | `/api/files/upload` | `POST` | 上傳分析素材 |
| | 刪除檔案 | `/api/files/{id}` | `DELETE` | 刪除檔案紀錄 |

---

## 2. 詳細定義與範例

### 使用者模組
- **GET /api/user**: 必須攜帶 `Authorization: Bearer <token>`。
- **PATCH /api/user**: 目前支援欄位 `{ "username": "..." }`。
- **PATCH /api/user/password**: 需包含舊密碼 (`old_password`) 與新密碼 (`new_password`)。

### 對話模組
- **POST /api/chat/messages**: 
  ```json
  {
    "query": "台積電最新財報分析",
    "chat_id": "選填，不填則開啟新對話"
  }
  ```
