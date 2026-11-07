# Stock Insight Agent 規格說明 (Agent Specification)

本文件定義 Stock Insight Chat 的 Agent 實作邏輯與架構設計。

## 0. 三種對話路徑（先看這個）

`POST /api/chat/messages` 依 `chat_mode` 與 `response_mode` 走三條完全不同的程式路徑：

| 路徑 | 觸發條件 | 實作 | 模型 |
| :--- | :--- | :--- | :--- |
| **思考模式**（本文件主體） | `chat_mode=stock_agent` + `response_mode=thinking`（預設） | [`agent/chat.py`](../app/backend/agent/chat.py) LangGraph ReAct | Router `gpt-5-mini` / Analyst `gpt-5` |
| **快捷模式（Flash）** | `chat_mode=stock_agent` + `response_mode=flash` | [`agent/flash_pipeline.py`](../app/backend/agent/flash_pipeline.py) 線性管線，**不走 LangGraph** | `FLASH_ANALYST_MODEL`（預設 `gpt-5-mini`） |
| **一般對話** | `chat_mode=general` | [`agent/general_chat.py`](../app/backend/agent/general_chat.py) | `GENERAL_CHAT_MODEL`（預設 `gpt-4o-mini`） |

Flash 模式預設**略過 Router LLM**（`FLASH_SKIP_ROUTER=1`），直接以使用者原文填檢索參數，省掉一次 LLM 往返；其可調變數見 [`env.md`](./env.md) §九與本文件 §9。一般對話不做向量檢索，可選開啟網路搜尋（`GENERAL_CHAT_ENABLE_WEB_SEARCH`）。

**§1 之後的內容只描述思考模式。**

---

## 1. 核心開發框架：LangGraph (ReAct)

系統採用 **LangGraph** 的 **Stateful Graph** 架構實作 **ReAct (Reasoning and Acting)** 循環。

### 核心節點與流轉：
1.  **Router (導航)**: 使用 `gpt-5-mini` 進行意圖辨識。它決定接下來要調用哪些工具，或是否已經具備充足資訊。支援動態綁定前端指定的工具集。
2.  **Retry Check (重試檢查)**: 一個安全閥節點。若工具回傳空結果且未達重試上限（與程式常數 **`ROUTER_MAX_CYCLES = 3`** 對齊之 Router 輪次），則注入提示引導 Router 調整策略（如擴大時間範圍或更換關鍵字）。
3.  **Tools (工具箱)**: 並行執行 (`asyncio.gather`) 所有被觸發的工具。執行完畢後**回到 Router** 進行下一輪思考。
4.  **Analyst (分析)**: 使用 `gpt-5` 撰寫最終分析報告。

### 1.1 推理強度（`reasoning_effort`）

`gpt-5` / `gpt-5-mini` 是 reasoning 模型，**延遲對思考強度極度敏感**。兩個節點各自可調：

| 環境變數 | 預設 | 理由 |
| :--- | :--- | :--- |
| `ROUTER_REASONING_EFFORT` | `minimal` | Router 只需選工具與參數，不需深度推理 |
| `ANALYST_REASONING_EFFORT` | `low` | Analyst 需論述，但預設深度過重會明顯拖慢 |

實作細節：目前的 `langchain-openai` 版本沒有 `reasoning_effort` 欄位，因此經 `model_kwargs` 直接帶進 OpenAI API（[`chat.py`](../app/backend/agent/chat.py) 的 `_with_reasoning_effort()`）。留空＝不傳此參數，走模型預設。

**Flash 模式與一般對話的模型不套用這兩個變數。**

驗證方式：OpenAI Platform → Logs → 展開該筆的 Tokens，看 **reasoning tokens**（`minimal` 應接近 0）。

---

## 2. 狀態定義 (Agent State)

```python
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages] # 完全對話歷史
    trace: Dict[str, Any]           # 紀錄各節點執行時間、Tool Calls 與 Thought
    retrieved_data: List[Dict]      # 儲存 Qdrant 的原始結構化數據，供 API 溯源
    enabled_tools: List[str]        # 前端動態指定的可用工具列表
```

---

## 3. 動態工具綁定 (Dynamic Tool Binding)

為了防止 AI 的幻覺與資源浪費，系統實作了**硬性工具過濾**：
*   **權限檢查**: 在 `call_router` 階段，系統會讀取 `enabled_tools`。
*   **動態繫結**: 僅將「被允許且系統已實作」的工具物件 (Tool objects) 透過 `.bind_tools()` 提供給 Router。
*   **目前支援工具**（[`chat.py`](../app/backend/agent/chat.py) 的 `tools` 清單，共 **4** 個）：

| 工具 | 資料來源 | 說明 |
| :--- | :--- | :--- |
| `search_stock_news` | Qdrant `news` + MongoDB | 股市新聞 Hybrid 檢索 |
| `search_market_ai_analysis` | Qdrant `ai_analysis` + MongoDB | AI 深度分析報告檢索 |
| `get_market_recommendations` | Qdrant `ai_analysis` | 結構化推薦股票／產業提取 |
| `tavily_global_search` | **Tavily Search API（外部）** | 即時網路搜尋：最新時事、一般知識、政策法規、非股市或非向量庫覆蓋的問題 |

`tavily_global_search` 需要 `TAVILY_API_KEY`，未設定時不可用；單輪呼叫次數由 `TAVILY_MAX_CALLS_PER_TURN`（預設 2）限制，避免 Agent 反覆搜尋拖慢回應。工具規格見 [`tools_spec.md`](./tools_spec.md) §2-D。

---

## 4. 數據優先與空結果策略 (Data-First & Retry Policy)

系統透過強化的 **System Prompt** 與 **Retry Check** 確保答案的真實性：
1.  **禁令**: 嚴禁僅憑內部記憶回答具體標的或公司清單。
2.  **檢證**: 必須透過工具獲取具備來源證明的資料。
3.  **自動重試**: 當工具回傳「找不到」時，Agent 會自動嘗試以下策略：
    *   擴大時間範圍 (例如推至 90 天)。
    *   切換關鍵字 (如中英文互換)。
    *   放寬過濾條件 (移除 stock_code 或 sentiment 限制)。
4.  **時間規範**: 針對「最新」、「近期」等詞彙，統一換算為伺服器時間往前回推 14 天。

---

## 5. 執行追蹤 (Transparency & Tracing)

系統為前端提供以下執行指標：
*   **執行步驟 (Steps)**: 詳細列出每一輪 Router 的 `thought` 與 `tool_calls`（含參數）。
*   **性能監測**: 記錄每個節點的 `execution_time`。
*   **精確來源 (retrieval_sources)**: 每個回答均附帶原始資料的 `mongo_id`、`publishAt`、`url` 與 `score`。

---

## 6. 指令規範 (Analyst Instructions)

最終回覆須遵循以下格式規範：
1.  **語意化結構**: 使用多級標題，避免單一條列式。
2.  **數據高亮**: 重要的「日期、股價、股票代碼 (XXXX)」使用 **粗體**。
3.  **多來源整合**: 交叉比對多來源資訊，說明對投資者的實質意義。
4.  **標的總結**: 在報告末尾列出提到的股票，並附上入選理由。

---

## 7. Token 管理與計量 (Token Management)

Agent 產出的 `trace` 包含完整的執行軌跡。後端 API 層會負責：
1.  **統計消耗**: 加總所有 LLM 呼叫的 Token。
2.  **非同步寫入**: 將數據寫入 PostgreSQL 的 `token_usage_logs`。
3.  **配額扣除**: 原子化更新 `user_usage_quotas`。

---

## 8. 檢索與延遲優化 (`app/backend/agent/chat.py`)

本節說明 Agent 在 **不改變圖流程**（Router → Retry Check → Tools → … → Analyst）的前提下，針對「查詢與後續 LLM 推理」所做的效能取向調整。目標是：**減少重複 Embedding 請求、略縮 Qdrant 結果量、縮短注入模型的上下文長度**，從而加速每一輪 Router / 最終 Analyst，並略降 API 成本。

### 8.1 為什麼「變快」：瓶頸在哪裡？

單次使用者提問的延遲大致由這幾類工作加總：

| 類型 | 典型開銷來源 |
|------|----------------|
| **Embedding** | 每次呼叫 OpenAI `text-embedding-3-small` 的網路與計費 API。 |
| **向量檢索** | Qdrant `search_groups` / `search` 的 `limit`（組數）與每組 chunk 數量；回傳 payload 越大，序列化與後處理越久。 |
| **Router (`gpt-5-mini`)** | 輸入 = 系統提示 + 歷史訊息 + **所有 ToolMessage**。Tool 回傳字數越長，延遲與計費越高。 |
| **Analyst (`gpt-5`)** | 同上，需在極長 context 上生成完整報告，通常是最重的一節。 |

因此優化策略分成兩條線：**減少上下游資料量**（top\_k、片段截斷）、**減少重複 Embedding**（共用客戶端 + 同一輪快取）。

### 8.2 共用 Embedding 客戶端：`_get_shared_embeddings()`

**做法**：以模組層級單例 `OpenAIEmbeddings(model="text-embedding-3-small")`，透過 `_get_shared_embeddings()` 取得，避免在每一次工具包裝或每一輪建 graph 時重複建立客戶端物件。

**效果**：減少重複初始化（HTTP 客戶端、設定物件等）的開銷；在多輪對話、多次工具呼叫時行為更穩定。對單次 Embedding API 的耗時影響較小，但對整體資源與微幅延遲仍有幫助。

### 8.3 同一輪工具執行內：Embedding 去重快取（`call_tools`）

**做法**：在 `call_tools` 節點內維護 `embedding_cache: Dict[str, List[float]]`，以「當輪 `aembed_query` 的查詢字串」為鍵；多個 tool call 若使用相同查詢文字（或推薦工具與其他工具共用同一 embedding 輸入語句），**只打一次** Embedding API。

**效果**：當 Router 在同一輪並行呼叫多個工具且參數中的 query 重複時，可明顯減少 OpenAI Embedding 的次數與等待時間。

**範圍**：快取僅限**該次** `call_tools` 執行，不跨輪持久化（避免舊向量與語義漂移混淆）。

### 8.4 縮小檢索廣度：`RETRIEVAL_TOP_K = 5`

**做法**：呼叫 `search_news`、`search_ai_analysis`、`search_recommendations` 時，明確傳入 `top_k=RETRIEVAL_TOP_K`（預設 **5**；相較於工具函式預設常見的 **10**）。

**效果**：

- Qdrant 端回傳的 **group 數量** 減少，單次檢索的計算與傳輸量略降。
- 後續組裝出的 `context` 條目變少，連帶降低格式化成字串的成本。

**取捨**：可能略少「邊緣相關」片段；若產品上更重召回率，可將常數調回 `10` 或改為環境變數可配置。

### 8.5 縮短寫入 LLM 的工具正文：`MAX_TOOL_ITEM_CHARS` 與 `_clip_for_llm`

**做法**：對 **餵給模型的 ToolMessage 字串**（`@tool` 包裝層與 `call_tools` 內的 `ai_content`）在組字時，對每則 `content` 套用 `_clip_for_llm(..., MAX_TOOL_ITEM_CHARS)`，預設 **500** 字元，超過則截斷並以「…」結尾。

**效果**：

- **Router** 下一輪讀到的 Tool 結果仍為摘要 → 輸入 token 較短 → 推理較快、較便宜。
- **Analyst**：見下方 **8.5.1**，在進入 Analyst 節點時另注入【完整參考資料】，使用 **`retrieved_data` 未截斷正文** 撰寫報告；對話串中的 ToolMessage 仍為摘要。

**與 API / 溯源的關係**：`retrieved_data` 由後端從 **未截斷** 的檢索結果組裝（`{**c, "source_tool": ...}`），供前端或 API 做來源展示，並作為 Analyst 完整參考區塊的資料來源。

#### 8.5.1 Router 決策與截斷：無法數學「保證」的取捨

ToolMessage 中的正文經 `_clip_for_llm(..., MAX_TOOL_ITEM_CHARS)`（預設約 **500** 字）後，**Router** 判斷「有無資料、是否重試、下一輪工具參數」所依據的主要是：**標題、metadata、每則開頭約 500 字、以及空結果時的固定文案**（如「找不到相關新聞」）。多數情境下，開頭片段已足夠做上述決策。

**並無嚴格保證**：若關鍵資訊僅出現在 **500 字之後**（例如數字、轉折語意落在文末），Router 可能低估該筆資料價值或較難微調下一輪參數。這是 **品質／延遲／成本** 的權衡，無法僅由截斷「證明」行為與全文等價。

**可調整方向**：

- 調大 `MAX_TOOL_ITEM_CHARS`（Router 摘要變長、成本與延遲上升）。
- 維持截斷給 Router，並讓 **Analyst 改讀完整參考**（見 8.5.2），避免最終報告遺失細節。

#### 8.5.2 Analyst 輸入：摘要對話 + 完整參考（實作於 `call_analyst`）

**先前誤解澄清**：若僅使用 `full_messages = [SystemMessage(analyst_prompt)] + messages`，則 **Analyst 與 Router 共用同一條 `messages`**，兩者看到的 ToolMessage **皆為截斷後內容**；`retrieved_data` 僅在 state 中供 API／溯源，**不會自動成為 Analyst 輸入**。

**目前實作**：在 `call_analyst` 中，於對話 `messages` **之後**追加一則 `SystemMessage`，標題為 **【完整參考資料】**，內文由 `_format_retrieved_data_for_analyst(state["retrieved_data"])` 組版，每則條目使用 **`content` 全文（不經 `MAX_TOOL_ITEM_CHARS`）**。Analyst 的 system prompt 明確指示：**事實與數據以該完整參考為準**，Tool Message 僅作流程摘要。

因此：

| 角色 | 看到的工具相關內容 |
|------|-------------------|
| **Router** | 對話內 **ToolMessage**：截斷後摘要。 |
| **Analyst** | 同上對話摘要 **＋** 末尾 **【完整參考資料】** 未截斷正文。 |

**注意**：Analyst 輸入總長度因而上升（含完整 RAG 正文），可能增加 **延遲與 token 費用**；若未執行過任何檢索（`retrieved_data` 為空），則不附加該區塊，行為與僅讀摘要時一致。

### 8.6 與既有設計的關係（並行工具）

`call_tools` 仍以 **`asyncio.gather`** 並行執行同一輪內多個 tool call，此部分與本節的 top\_k / 截斷 / Embedding 優化**疊加**：並行負責「縮 wall-clock」；top\_k 與截斷負責「縮每條路的工作量與後續 LLM 輸入」。

### 8.7 調參建議（維運）

| 常數 | 調大 | 調小 |
|------|------|------|
| `RETRIEVAL_TOP_K` | 召回更完整，Qdrant 與後續字串更長、較慢。 | 更快、更省，可能漏掉次要來源。 |
| `MAX_TOOL_ITEM_CHARS` | Router（與對話中的 Tool 摘要）讀到較長片段；完整正文仍由 Analyst 【完整參考資料】提供。 | Router 上下文更短；但若關鍵在文末仍可能影響 Router 判斷（見 8.5.1）。 |

若延遲仍主要卡在 **Analyst**，應優先評估模型選型、`max_tokens`、或拆短 system prompt；若卡在 **Qdrant**，可再評估工具層的 `group_size`、score 閾值等（見 `news.py` / `ai_analysis.py`，非本檔規格核心）。

### 8.8 延遲調校的實務順序

上面 8.2–8.5 是**檢索側**的優化。實務上思考模式的延遲大頭通常在 **LLM 推理本身**，建議的調整順序是：

1. **`ROUTER_REASONING_EFFORT=minimal`**（見 §1.1）—— Router 通常能省數秒，且幾乎不影響品質。
2. **`ANALYST_REASONING_EFFORT`** 由 `low` 再降到 `minimal` —— 影響論述深度，改動前後請比對實際輸出。
3. **縮報告篇幅**：`ANALYST_TARGET_MAX_WORDS` / `ANALYST_HARD_CAP_WORDS`（見 [`env.md`](./env.md) §6.1）。若 reasoning tokens 已經很低但時間仍長，瓶頸就是輸出長度，調 effort 沒用。
4. 最後才動 `RETRIEVAL_TOP_K`（程式常數）與 `MAX_TOOL_ITEM_CHARS`。

前三項都是**純 `.env` 變更**，不需重 build image；改完用 `up -d backend` 重建容器即可（`restart` 不會重讀 env）。

診斷方法：OpenAI Platform → Logs，對照 Router 與 Analyst 兩筆的 tokens 與時間，先確認時間花在哪一段再動手。

---

## 9. 快捷模式（Flash）問句收成與雙軌檢索

（原 `readme_full_details.md`）

### 雙軌檢索策略（`FLASH_REWRITE_DUAL_SEARCH`）

> 僅在 **`FLASH_LLM_QUERY_REWRITE=1`**（問句收成已開啟）時有意義；收成關閉時此變數不生效。

| 值 | 模式 | 行為 |
|----|------|------|
| **`1`**（預設） | **雙軌／並行** | **同時**做：① 用**原文**搜向量庫 ② 用 `FLASH_REWRITE_MODEL`（預設 `gpt-4o-mini`）收成問句。若收成成功且結果與原文**字串不同**（各先 `.strip()` 比對），再以收成後的 `query` **搜第二次**；兩批結果經 `_merge_news_retrieved` **合併、去重**。牆鐘時間通常接近「較慢的那條路」，而不是「收成時間 + 兩次檢索」完全相加。 |
| **`0`** | **單軌／循序** | **先**等收成完成，**只**用收成後的 `query` 搜**一次**（收成失敗／逾時／解析失敗則退回原文）。較省向量查詢次數，但無「原文並行保底」；牆鐘 ≈ 收成時間 + 一次檢索。 |

**何時會跑第二次搜尋？**（雙軌 `=1` 時，三者須同時成立；實作見 `app/backend/agent/flash_pipeline.py`）

1. 收成 LLM **成功回覆**（未逾時、未 API 錯誤）
2. 從 JSON 解析出的 `query` **非空**
3. 收成字串 **≠** 使用者原文（純字串相等比較，非語意相似度）

若 LLM 回傳與原文相同、解析失敗或逾時，則**不**跑第二次，僅保留第一次（原文）搜尋結果 `r_orig` 作為保底。

**雙軌流程圖**（`FLASH_LLM_QUERY_REWRITE=1` 且 `FLASH_REWRITE_DUAL_SEARCH=1`）：

```
使用者問句
    │
    ├─ 第 1 次搜尋（原文）─────────→ r_orig  [最多 FLASH_RETRIEVAL_TOP_K 篇，預設 10]
    │
    └─ 4o-mini 收成 → 若 query 與原文不同
           │
           └─ 第 2 次搜尋（收成 query）→ r_rw   [最多 FLASH_RETRIEVAL_TOP_K 篇]
                    │
                    ▼
         _merge_news_retrieved(r_orig, r_rw, cap=FLASH_MERGED_RETRIEVE_CAP)
                    │
         ┌──────────┴──────────┐
         │ 1. 先保留 r_orig 全部 │
         │ 2. 再補 r_rw 新篇     │
         │ 3. 依 mongo_id 去重   │
         │ 4. 最多 cap 篇        │  ← 預設 max(TOP_K×2, 16)，TOP_K=10 時為 20
         └──────────┬──────────┘
                    ▼
         build_flash_analyst_messages（每篇正文截 FLASH_REF_MAX_BODY_CHARS 字）
                    ▼
              FLASH_ANALYST_MODEL 作答（預設 gpt-5-mini）
```

**合併去重重點**（`_merge_news_retrieved`，`app/backend/agent/flash_pipeline.py`）：

| 問題 | 答案 |
|------|------|
| 第一次結果會被採納嗎？ | **會**，且排在前面、優先保留 |
| 第二次結果呢？ | **會**，只補第一次沒有的新聞 |
| 兩次都搜到同一篇？ | **只留第一次那份**（依 `mongo_id` 去重；無 `mongo_id` 時退而用 `title` + `publishAt`） |
| 會重新依分數排序嗎？ | **不會**，維持「第一次順序 + 第二次新增」 |
| 最多幾篇？ | `FLASH_MERGED_RETRIEVE_CAP`（預設 `max(FLASH_RETRIEVAL_TOP_K×2, 16)`） |

> **設計取捨**：雙軌的本質是 **第一次當主結果 + 第二次當補充召回**，不是「第二次取代第一次」。若收成 query 幾乎每次都與原文不同，牆鐘時間通常**不會**比單軌（收成 → 只搜一次）更快，但會多一次向量查詢；換取的是原文保底與更廣的召回。想壓低成本可設 `FLASH_REWRITE_DUAL_SEARCH=0`。

### 快捷模式（`response_mode: flash`）相關 `.env` 變數（選用）

下列變數**僅影響快捷模式**；程式內皆已有預設值，**不必**為了啟用快捷而強制寫入 `.env`。只有當你要**覆寫**預設（換模型、調速度／品質權衡、改檢索區間等）時，再在專案根目錄的 `.env` 中新增即可。修改後請重啟後端（或重建容器）讓環境變數生效。

**內建預設（與程式一致）** — 若你希望 `.env` 與目前程式預設對齊，可照下表填寫：

| 變數 | 建議填入（= 目前程式預設） | 簡短說明 |
|------|---------------------------|----------|
| `FLASH_SKIP_ROUTER` | `1` | `1`＝略過 Router LLM（較快）；`0`＝啟動 Router 協助改寫檢索關鍵字（較慢） |
| `FLASH_ANALYST_MODEL` | `gpt-5-mini` | 快捷模式 Analyst 使用的模型 |
| `FLASH_ANALYST_MAX_TOKENS` | `5000` | 對應 **`max_completion_tokens`**。`gpt-5-mini` 等會先耗用推理 token，設太小會**只剩標題／半句可見正文**。**不設定**＝用程式預設 `5000`；設為**空字串**＝不傳上限 |
| `FLASH_RETRIEVAL_TOP_K` | `10` | **`search_news`（快捷）** 每輪取回筆數；思考模式不依此變數 |
| `FLASH_REF_MAX_BODY_CHARS` | `2200` | 每段「參考正文」注入 Analyst【完整參考資料】前的截斷上限；字數愈少通常愈快 |
| `FLASH_DATE_RANGE_DAYS` | `80` | 快捷兩工具預設 `start_date`／`end_date` 的回溯天數；不寫入時同樣為 `80` |
| `FLASH_LLM_QUERY_REWRITE` | `0` | `1`＝檢索前以小模型收成 query；預設關閉 |
| `FLASH_REWRITE_DUAL_SEARCH` | `1` | `1`＝並行／雙軌搜尋＋合併去重（較不保證最便宜，但不把延遲變成「改寫＋檢索」完全相加） |
| `FLASH_REWRITE_MODEL` | `gpt-4o-mini` | 收成用模型 |
| `FLASH_REWRITE_MAX_COMPLETION_TOKENS` | `256` | 收成回覆 token 上限 |
| `FLASH_REWRITE_TIMEOUT_SEC` | `12` | 收成逾時（秒） |
| `FLASH_MERGED_RETRIEVE_CAP` | `max(FLASH_RETRIEVAL_TOP_K×2,16)` | 雙軌合併後參考上限 |

> **為什麼回答只有一兩行？**  
> `.env` 若仍是 **`FLASH_ANALYST_MAX_TOKENS=900`**（或其他過小數值），在 **`gpt-5-mini`** 上很常發生：**`max_completion_tokens` 多半先被內部推理用掉**，對使用者可見的段落幾乎寫不完，看起來像只有標題＋半截句子。請改 **`2800` 或以上**、或**刪除此變數**讓程式用預設 `5000`（設成空字串則完全不傳上限）。
>
> 注意專案根目錄 `.env.example` 的註解範例寫的是 `2800`，比程式預設保守；兩者都可用，但別誤以為 `2800` 是預設值。

**什麼時候才需要在 `.env` 加上述變數？**

- **想換模型**：例如 `FLASH_ANALYST_MODEL=gpt-4o-mini`（以你帳號／供應商實際可用模型為準）。
- **想要品質、可接受較慢**：例如 `FLASH_SKIP_ROUTER=0`、將 `FLASH_ANALYST_MAX_TOKENS` 調大、將 `FLASH_RETRIEVAL_TOP_K` 調大。
- **想再壓低延遲**：例如略降 `FLASH_RETRIEVAL_TOP_K`、`FLASH_REF_MAX_BODY_CHARS`。若曾開 **`FLASH_LLM_QUERY_REWRITE=1`**，可改 **`FLASH_REWRITE_DUAL_SEARCH=0`**（只搜一次較便宜）或將 **`FLASH_MERGED_RETRIEVE_CAP`** 調小。（**請勿對 `gpt-5-mini`** 將 `FLASH_ANALYST_MAX_TOKENS` 設得過低，否則可見正文極易被截斷。）

**結論**：不必為了「有預設值」而把整張表複製進 `.env`；依需求只覆寫少數鍵即可。其餘 Router／歷史載入相關變數仍見前文「可調整的環境變數」表格。
