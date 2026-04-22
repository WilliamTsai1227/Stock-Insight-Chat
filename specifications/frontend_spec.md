# 前端規格說明書 (Frontend Specification)

本系統採用現代化、高品質的 **「玻璃擬態 (Glassmorphism)」** 與 **「AI-Native」** UI 設計風格。

## 1. 核心設計語彙
*   **視覺風格**: 半透明背景 (Backdrop Filter)、細緻描邊、深色主題、Outlined Icons。
*   **字體**: `Outfit` (標題) 與 `Inter` (內文)。
*   **互動**: 打字機效果、Loading 思考動畫、Marked.js Markdown 渲染。

## 2. 主要頁面結構

### 2.1 儀表板 (Dashboard / Chat)
*   **Sidebar**: 
    *   **PROJECTS**: 專案列表與切換。
    *   **CHATS**: 專案下的對話列表。
    *   **User Profile**: 顯示目前使用者。
*   **Main Chat**: 
    *   **Welcome Hero**: 初次進入顯示的歡迎畫面。
    *   **Message Bubbles**: 區分 User 與 AI 氣泡。
    *   **ReAct Trace Card**: 摺疊式面板，顯示 Agent 每一輪的 `Thought` 與 `Tool Calls`。
    *   **Sources Card**: 顯示該回答引用的原始新聞或報告來源，點擊可跳轉至原文網址。
*   **Input Area**: 
    *   **Auto-expanding Textarea**: 根據輸入內容自動調整高度。
    *   **Tool Control Popover**: 可手動切換「自動模式」或指定特定的工具權限。

### 2.2 會員與設定 (Planned)
*   **個人資料**: 等級顯示 (Free/Pro/Ultra)。
*   **用量監測**: 即時顯示 Token 消耗。

## 3. 技術棧
*   **核心**: HTML5, Vanilla JavaScript。
*   **樣式**: Vanilla CSS (使用 Flex/Grid 與 CSS Variables)。
*   **第三方庫**:
    *   `lucide-icons`: 向量圖示。
    *   `marked.js`: Markdown 解析。
    *   `fetch API`: 與後端 FastApi 通訊。

## 4. 特色實作：動態高亮
*   **Stock Ticker Highlighting**: 自動偵測內容中的 4 位數字股票代碼，並包裝成 `.stock-ticker` 高亮標籤。
*   **Streaming-like Typing**: 模擬 AI 思考與回覆的流程。
