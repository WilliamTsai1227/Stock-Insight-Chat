# Stock Insight Agent 規格說明 (Agent Specification)

本文件定義了「股市洞察系統」的核心對話大腦 (Agent) 實作邏輯與架構設計。

## 1. 核心開發框架：LangGraph (ReAct)

系統採用 **LangGraph** 的 **Stateful Graph** 架構實作 **ReAct (Reasoning and Acting)** 循環，確保 Agent 具備嚴謹的思考流程。

### 核心節點與流轉：
1.  **Router (導航)**: 使用 `gpt-5-mini` 進行意圖辨識。它決定接下來要調用哪些工具，或是否已經具備充足資訊。支援動態綁定前端指定的工具集。
2.  **Retry Check (重試檢查)**: 一個安全閥節點。若工具回傳空結果且未達重試上限（與程式常數 **`ROUTER_MAX_CYCLES = 3`** 對齊之 Router 輪次），則注入提示引導 Router 調整策略（如擴大時間範圍或更換關鍵字）。
3.  **Tools (工具箱)**: 並行執行 (`asyncio.gather`) 所有被觸發的工具。執行完畢後**回到 Router** 進行下一輪思考。
4.  **Analyst (分析)**: 使用 `gpt-5` 進行深度報告撰寫。這保證了最後的分析文本具備極高的專業度與 Gemini 風格的高層次洞察。

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
*   **目前支援工具**: `search_stock_news`, `search_market_ai_analysis`, `get_market_recommendations`。

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

系統為前端提供了全透明的執行指標：
*   **執行步驟 (Steps)**: 詳細列出每一輪 Router 的 `thought` 與 `tool_calls`（含參數）。
*   **性能監測**: 記錄每個節點的 `execution_time`。
*   **精確來源 (retrieval_sources)**: 每個回答均附帶原始資料的 `mongo_id`、`publishAt`、`url` 與 `score`。

---

## 6. 指令規範 (Analyst Instructions)

最後的回覆必須遵循 **Gemini 風格**：
1.  **語意化結構**: 使用多級標題，避免死板的條列式。
2.  **數據高亮**: 重要的「日期、股價、股票代碼 (XXXX)」必須使用 **粗體**。
3.  **深度合成**: 交叉驗證多來源資訊，解釋對投資者的實質意義。
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

### 8.4 縮小檢索廣度：`RETRIEVAL_TOP_K = 8`

**做法**：呼叫 `search_news`、`search_ai_analysis`、`search_recommendations` 時，明確傳入 `top_k=RETRIEVAL_TOP_K`（預設 **8**；相較於工具函式預設常見的 **10**）。

**效果**：

- Qdrant 端回傳的 **group 數量** 減少，單次檢索的計算與傳輸量略降。
- 後續組裝出的 `context` 條目變少，連帶降低格式化成字串的成本。

**取捨**：可能略少「邊緣相關」片段；若產品上更重召回率，可將常數調回 `10` 或改為環境變數可配置。

### 8.5 縮短寫入 LLM 的工具正文：`MAX_TOOL_ITEM_CHARS` 與 `_clip_for_llm`

**做法**：對 **餵給模型的 ToolMessage 字串**（`@tool` 包裝層與 `call_tools` 內的 `ai_content`）在組字時，對每則 `content` 套用 `_clip_for_llm(..., MAX_TOOL_ITEM_CHARS)`，預設 **1200** 字元，超過則截斷並以「…」結尾。

**效果**：

- **Router** 下一輪讀到的 Tool 結果仍為摘要 → 輸入 token 較短 → 推理較快、較便宜。
- **Analyst**：見下方 **8.5.1**，在進入 Analyst 節點時另注入【完整參考資料】，使用 **`retrieved_data` 未截斷正文** 撰寫報告；對話串中的 ToolMessage 仍為摘要。

**與 API / 溯源的關係**：`retrieved_data` 由後端從 **未截斷** 的檢索結果組裝（`{**c, "source_tool": ...}`），供前端或 API 做來源展示，並作為 Analyst 完整參考區塊的資料來源。

#### 8.5.1 Router 決策與截斷：無法數學「保證」的取捨

ToolMessage 中的正文經 `_clip_for_llm(..., MAX_TOOL_ITEM_CHARS)`（預設約 **1200** 字）後，**Router** 判斷「有無資料、是否重試、下一輪工具參數」所依據的主要是：**標題、metadata、每則開頭約 1200 字、以及空結果時的固定文案**（如「找不到相關新聞」）。多數情境下，開頭片段已足夠做上述決策。

**並無嚴格保證**：若關鍵資訊僅出現在 **1200 字之後**（例如數字、轉折語意落在文末），Router 可能低估該筆資料價值或較難微調下一輪參數。這是 **品質／延遲／成本** 的權衡，無法僅由截斷「證明」行為與全文等價。

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
