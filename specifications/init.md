# 專案初始意圖 (Project Initialization - ⚠️ 僅供歷史參考)

> [!NOTE]
> 本文件紀錄了專案啟動時的初始構想。目前系統架構已演進，請優先參考各項正式規格說明書 (`agent_spec.md`, `api_spec.md`, `database_spec.md` 等)。

我現在的原本應用叫做stock insight 他就是一個應用後面的data 流程會是每天不斷去爬蟲股市新聞，每則新聞處理成一定結構存入mongodb 的 news collection ，然後另外一條pipeleine 去每天固定時間拿到一定數量的news 新聞去餵給llm 去產生統整報告，然後回存到mongodb 的ai_analysis collection (有產生推薦股票跟推薦產業）最終前端有可以去看新聞跟ai統整報告這樣，還有配一顆postgresql 去建立上市櫃公司十年歷史財報的數據，以結構化儲存，也有會員系統機制可以註冊跟登入，postgresql 也有結構化的會員相關表



我現在要做另外一個應用，叫做stock-insight-chat ，要做一個類似像是chatgpt 的應用，可以建立個專案，每個專案可以上傳文件（有上限），專案下可以建立多個聊天，重點是聊天送出後會去觸發agent 去調用tools 做搜尋，我要做幾個tools 如下

1. 新聞data search ( vector + 關鍵字 混合搜尋）（要有向量資料庫）

2. AI Analysis search ( vector + 關鍵字 混合搜尋）（要有向量資料庫）

搜尋流程：

使用者問問題（帶有chatid) ->根據使用者問題轉向量 ->去向量資料庫找到相似轉向量片段 -> 新聞或是AI Analysis ID 去重 ->Agent 判斷是否要拿ID 去 mongodb 拿完整的新聞跟AI Analysis  -> 去postgresql的messages 拿到此chat_id的最近多少筆的歷史問答紀錄 ->將使用者問題以及（檢索到的片段）或是（完整新聞跟AI Analysis）以及（最近多少筆的歷史問答紀錄）餵給llm -> llm 生成回答 -> 將問答紀錄寫入postgresql 這部分每一條不要包含·完整的新聞跟AI Analysis文章，因為會很大，要存找到相似轉向量片段即可）

儲存message 策略：
context_refs 欄位只儲存檢索到的新聞標題，id，片段
context_refs:{
    {"title": "新聞標題",
    "id": "新聞id",
    "snippet": "片段"},
    {"title": "新聞標題",
    "id": "新聞id",
    "snippet": "片段"}
}

回傳：
```json
{
  "status": "success",
  "data": {
    "message_id": "postgresql_id",
    "ai_response": "根據分析，台積電在 2021-2023 年間成長顯著...",
    "project_id": "550e8400-e29b-41d4-a716-446655440000",
    "chat_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "sources": [
        { "id": "file_001(postgresql_id)", "type":"file", "title": "2024Q3營收報告.pdf", "chunks": 2 },
        { "id": "file_002(postgresql_id)", "type":"image", "title": "台積電股價圖.jpg" },
        { "id": "news_001(mongodb_id)", "type":"news", "title": "台積電近期新聞" },
        { "id": "ai_analysis_001(mongodb_id)","type":"ai_analysis", "title": "台積電AI分析報告" },
    ],
    "usage": {
        "prompt_tokens": 120500,
        "completion_tokens": 500,
        "is_cached": true 
    }
  }
}
```


3. File 檢索（要支援比對多分文件內容，針對文件內容回答問題）
搜尋流程：

使用者問問題（帶有chatid,project_id)-> 去postgresql 找到這個project_id 的所有文件（去files table 找tltile以及文件內容），去postgresql messages找到此chat_id的最近多少筆的歷史問答紀錄 -> file 文件內容可能會儲存在s3，會有多一個延遲去s3拿文件內容 -> 有了文件完整內容跟使用者問題以及（最近多少筆的歷史問答紀錄）後一併餵給LLM -> LLM 生成回答

儲存message 策略：
context_refs 欄位只儲存文件的title 以及 s3 網址
context_refs:{
    {"file_name": "文件名稱",
    "s3_url": "s3 網址"},
    {"file_name": "文件名稱",
    "s3_url": "s3 網址"}
}

回傳：
```json
{
  "status": "success",
  "data": {
    "message_id": "postgresql_id",
    "ai_response": "根據分析，台積電在 2021-2023 年間成長顯著...",
    "project_id": "550e8400-e29b-41d4-a716-446655440000",
    "chat_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "sources": [
        { "id": "file_001(postgresql_id)", "type":"file", "title": "2024Q3營收報告.pdf", "chunks": 2 },
        { "id": "file_002(postgresql_id)", "type":"image", "title": "台積電股價圖.jpg" },
        { "id": "news_001(mongodb_id)", "type":"news", "title": "台積電近期新聞" },
        { "id": "ai_analysis_001(mongodb_id)","type":"ai_analysis", "title": "台積電AI分析報告" },
    ],
    "usage": {
        "prompt_tokens": 120500,
        "completion_tokens": 500,
        "is_cached": true 
    }
  }
}
```

4. global search 


### 記憶策略
當對話變得很長，超出模型限制或導致成本過高時，您需要採取以下策略：

1. 滑動窗口 (Sliding Window)：只抓取最近的 $N$ 筆（例如最近 10 筆）對話。這是最簡單、最常用的做法。

2. 自動摘要 (Summarization)： -> 日後優化

當對話超過一定長度，觸發一個背景任務，讓 LLM 將「舊的對話」總結成一段精簡的摘要。

發送格式：系統提示詞 + 過往摘要 + 最近 3 筆對話 + 當前問題。

3. 語義檢索記憶 (Long-term Memory / Message RAG)：-> 日後優化

如果專案很大，對話很多，可以將過往的 messages 也轉成向量存入 Qdrant。

當使用者問問題時，先去 Qdrant 搜「以前有沒有聊過類似的」，把相關的歷史片段抓出來當成參考。

### 「專案制」記憶設計建議

您有 project_id 和 chat_id 的階層：

1. Chat 級別記憶：處理「剛才說了什麼」。通常用 Sliding Window，讓使用者覺得對話流暢。

2. Project 級別記憶：處理「這個專案的背景是什麼」。這部分不建議把所有 Chat 記錄都餵進去，而是透過您設計的 File Tool 或 Project Description，以 RAG 的方式按需檢索。

3. 像是這個專案的 「研究筆記」 或 「狀態快照」。建議包含：

- 專案目標 (Goal)：例如「分析 2026 年半導體產業趨勢，重點關注台積電與輝達」。

- 關鍵實體 (Key Entities)：該專案反覆提到的股票代碼 (2330, NVDA)、特定產業關鍵字。

- 核心發現摘要 (Findings Summary)：這就是您提到的「由 LLM 更新」的部分。例如：「目前已上傳 3 份財報，初步結論顯示毛利率持續上升」。

- 使用者偏好 (Preferences)：例如「報告風格需簡潔」、「偏好技術面分析」。

4. 實作「自動更新專案描述」？
這是一個非常前衛且實用的做法（類似 Mem0 或 Long-term Memory 的概念）
實作流程：
- 觸發時機：當一個對話視窗結束（或每隔 5 則訊息）。

- 執行任務：啟動一個背景 LLM 任務，讀取最近的對話摘要與現有的專案描述。

- 整合更新：讓 LLM 判斷是否有新的重要資訊需要更新到專案描述中。

- 存回資料庫：更新 projects 表中的 description 或 summary 欄位。

這樣做的優勢： 之後不管開啟哪一個新對話 (Chat)，系統都會先讀取這段「最新的專案現況」，LLM 就能瞬間進入狀況，不需要使用者重新餵背景。

5. 建議在 projects 表增加一個欄位來存放這些「動態記憶」：

- 欄位名稱,資料型別,說明
- context_summary,JSONB,儲存 LLM 自動生成的專案摘要、關鍵字與當前進度。
- system_prompt_override,TEXT,(選配) 讓使用者針對此專案自定義的 AI 角色設定。


###  為什麼要用 RAG 而非全部塞進去？
如果專案下有 10 個對話、20 份文件，全部塞進 Context Window 會造成：

成本飆高：每次問話都要算幾萬個 Tokens。

注意力分散 (Lost in the Middle)：LLM 可能會忽略中間重要的細節。

正確策略：

專案描述 (Summary)：直接塞進 System Prompt（因為很精簡）。

專案文件 (Files)：透過 File Tool (RAG)，等 AI 覺得需要時再去檢索。

跨對話記憶：透過您提到的「自動更新描述」機制，將跨對話的重點精煉後塞入 System Prompt。



---

### 1. 向量資料庫：Qdrant 是好選擇嗎？還有其他選項嗎？

**Qdrant** 是目前開源界中，針對 **混合搜尋 (Hybrid Search)** 支援度與效能平衡得最好的選擇之一。

* **為什麼選 Qdrant？**
    * **原生混合搜尋**：它對向量與標籤（關鍵字）過濾的整合非常直覺。
    * **效能優異**：使用 Rust 編寫，資源消耗比 Milvus 低，效能比 PGVector 高。
    * **Payload 彈性**：可以在 Qdrant 存入 `news_id`、`source`、`category` 等 Metadata。

* **其他選擇：**
    * **PGVector (PostgreSQL 插件)**：既然您已經有一顆 PostgreSQL，這是最省事的做法。**優點**：不需維護新資料庫，一條 SQL 同時查關係資料與向量。**缺點**：當向量數達到百萬等級，效能會輸給專門的向量資料庫（如 Qdrant）。
    * **Pinecone**：全託管，完全不需要管機器，但長期成本較高且免費額度有限。
    * **Milvus**：適合超大規模（億級）資料，但部署非常重，對您目前的規模來說可能太過複雜。

**結論：** 繼續使用 **Qdrant** 是非常正確且具備擴展性的選擇。 ->選擇這個

---

### 2. 資料流與檢索流程優化

您的流程 `向量檢索 -> 拿 ID 回 MongoDB 拿全文 -> LLM` 是業界標準的 **RAG 實踐**，但細節上建議優化：

* **同步腳本 (ETL)**：您確實需要一個 Worker 定時監控 MongoDB 的 `news` 和 `ai_analysis`，將新內容：
    1.  進行 **Chunking (分段)**：一則新聞太長會導致向量捕捉不到細節，建議以 500-1000 tokens 為一段。
    2.  **存入 Qdrant**：Payload 必須包含 MongoDB 的 `_id` 以及 `text_chunk`（該片段的原文）。
* **查詢優化**：不需要每次都回 MongoDB 拿「全文」。
    * **建議**：在 Qdrant 搜尋時直接拿回「相似片段 (Chunks)」，如果片段夠完整，直接餵給 LLM 即可。只有當 LLM 需要參考「整篇文章」的上下文時，才去 MongoDB 撈全文。 －> 這部分要用langgraph 判斷？

---

### 3. PostgreSQL 儲存策略：Message 該存什麼？

這是您最擔心的部分。`messages` 表如果存入整篇新聞，資料庫會迅速膨脹，影響效能。

**建議做法：儲存「參考資訊 (References)」而非「全文內容」。**

| 方案 | 存入內容 | 優點 | 缺點 |
| :--- | :--- | :--- | :--- |
| **方案 A (推薦)** | **Metadata / Refs** | 存入 `[{type: "news", id: "mongo_id", title: "新聞標題"}]` | 節省空間，查詢歷史時前端需再撈一次標題。 |
| **方案 B** | **Retrieved Chunks** | 只存下當時餵給 LLM 的那幾個「相似片段」。 | 回溯對話時，能準確知道 AI 是根據哪一段話回答。 |  -> 我要選這個方案
| **方案 C (最差)** | **Full Article** | 存入整篇新聞全文。 | **千萬不要。** 浪費空間，且對話記錄會變得很髒。 |

**我的實務建議：**
在 `messages` 表中增加一個 JSONB 欄位叫做 `context_refs`：
```json
{
  "role": "ai",
  "content": "根據最新新聞，台積電...",
  "context_refs": [
    { "source": "news", "id": "65f123...", "title": "台積電法說會亮眼", "score": 0.92 },
    { "source": "file", "id": "file_uuid", "page": 5, "snippet": "..." }
  ]
}
```
這樣做的好處是：
1.  **資料量小**：PostgreSQL 負荷輕。
2.  **可追溯性**：前端可以根據這些 ID 渲染出「參考來源」卡片，點擊後再從 MongoDB 或 S3 讀取詳細內容。

---

### 4. 整體架構評價

您的 **MongoDB (大數據量/非結構化) + PostgreSQL (關係/業務核心) + Qdrant (向量檢索)** 是非常標準的 **AI-Native Stack**。

* **優點**：模組化極強。如果未來新聞量爆增，您只需擴展 Qdrant；如果會員系統變複雜，只需優化 PostgreSQL。
* **小提醒**：
    * **File 檢索 (Tool 3)**：檔案通常是私有的（屬於特定 Project），在 Qdrant 搜尋時，**務必加上 `filter={"project_id": "xxx"}`**，否則會發生資安漏洞，搜到別人的文件。
    * **Token 計費**：既然 `messages` 有 `tokens` 欄位，建議在寫入時就將該次對話消耗的 Input/Output Tokens 算好存入，方便以後統計成本。

這個設計非常扎實，直接執行是沒問題的！