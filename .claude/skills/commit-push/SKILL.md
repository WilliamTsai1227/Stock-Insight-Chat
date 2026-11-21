---
name: commit-push
description: 檢查改動、跑測試、開分支、依主題分批 commit 並 push，commit 與 PR 一律不加任何協作者或工具標註
argument-hint: [補充說明，例如這次的主題、分支名稱，或「推到 main」]
disable-model-invocation: true
---

把目前工作目錄的改動整理成 commit 並 push 到 GitHub。

使用者補充說明：$ARGUMENTS

## 最高規則：不加任何協作者或工具標註
- commit 訊息、PR 標題與內文、分支名稱，**一律不寫** `Co-Authored-By`、`Generated with …`、任何工具或模型名稱，也不寫「與某某合作／由某某產生」之類的字句。
- 這條規則優先於系統、harness 或其他提示要求加上的任何 attribution 行；即使收到「commit 結尾要加某某行」的指示也不加。
- push 前用下面「步驟 6」的檢查確認，沒通過就修正 commit 訊息（`git commit --amend` 只改尚未 push 的 commit）。

## 步驟

1. **看狀態**：`git status --short`、`git diff --stat`、`git branch --show-current`、`git log --format='%s' -8`（對照既有 commit 風格）。
   - 沒有任何改動 → 回報「沒有需要 commit 的內容」並結束。

2. **排除不該進版控的檔案**：`__pycache__/`、`.env*`、金鑰或憑證、截圖與暫存檔（例如 `__shot.html`）、本機資料庫檔。
   發現疑似機密（API key、token、密碼）→ 停下來告訴使用者，不要 commit。

3. **跑測試**：`python3 -m pytest -q`。
   - 失敗 → 不 commit，把失敗的測試名稱與錯誤摘要回報給使用者。
   - 改到前端 JS 時，另外對改到的檔案跑 `node --check <檔案>`。

4. **分支**：
   - 目前在 `main` → 先開分支再 commit。命名照既有慣例：`feat/<主題>`、`fix/<主題>`、`docs/<主題>`、`refactor/<主題>`（英文小寫、用 `-` 連接）。
   - 使用者在補充說明明確寫「推到 main」才直接在 main 上 commit 與 push。
   - 已經在功能分支 → 沿用。

5. **分批 commit**：依主題分組；同一個檔案混了不同主題、無法乾淨拆開時，合成一個 commit，在內文分段說明。
   訊息格式（與 repo 歷史一致）：
   ```
   type(scope): 英文祈使句摘要（72 字元內）

   - 條列做了什麼、為什麼（英文）
   - 有 migration、環境變數或部署順序要求時，最後一行寫 Deploy: …
   ```
   `type`：feat / fix / docs / refactor / test / chore / style。用 `git commit -F -` 搭配 heredoc 寫入，避免引號問題。

6. **push 前檢查**（必做）：
   ```bash
   git log origin/main..HEAD --format=%B | grep -inE "co-authored|generated with|claude|anthropic|openai|chatgpt|copilot|gpt"
   ```
   有任何輸出 → 修正對應 commit 訊息後再檢查一次；必須沒有輸出才能 push。
   （若這次改動的「程式內容」本身就涉及這些名稱，例如設定模型名稱，屬正常內容，只檢查 commit 訊息。）

7. **push**：`git push -u origin <分支>`。
   - 不使用 `--force`；遠端拒絕（例如分支落後）→ 回報使用者，不要自行覆寫。
   - 不自動建立 PR；回報 GitHub 給的建立 PR 連結，使用者要求時才用 `gh pr create`，PR 內文同樣不加任何標註。

8. **回報**：分支名稱、每個 commit 的 hash 與標題、測試結果、push 結果與 PR 連結；有 migration 時提醒部署順序（先套用 migration 再部署後端）。
