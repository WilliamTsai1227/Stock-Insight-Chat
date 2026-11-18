---
id: sector
mention: 產業比較
label: 產業比較
description: 產業結構、市場規模、主要玩家競爭定位與估值比較
source: https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/agent-plugins/market-researcher/skills/sector-overview/SKILL.md; https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/competitive-analysis/SKILL.md
license: Apache-2.0
modifications: 合併 sector-overview 與 competitive-analysis 兩個 skill，改寫為繁中、聊天回覆長度、台股供應鏈情境；移除 Word／PowerPoint／Excel 輸出與排版規範；資料來源改為深度研究檢索。
---

# 產業比較（改編自 Anthropic sector-overview 與 competitive-analysis skills）

## 資料蒐集

1. **界定範圍**：產業／次產業定義要具體（例如「AI 伺服器 ODM」而非「電子業」），
   確認比較對象是上市櫃公司、還是含國際與未上市業者。
2. **找出產業的關鍵指標**（先做這步，後面的比較都圍繞它）：
   例如晶圓代工看製程節點與產能利用率、記憶體看位元出貨與報價、航運看運價與艙位。
3. **市場規模與成長**：總市場規模（TAM）與出處、過去 5 年 CAGR、未來預測與假設、
   市場區隔（產品、地區、終端應用）。
4. **產業結構**：前五大市佔率（集中或分散）、價值鏈各環節與利潤分布、進入門檻、
   台股公司在全球供應鏈的位置。
5. **主要玩家（5–8 家）**：營收、成長率、利潤率、市佔率、主要差異化、近期動態
   （財報、併購、新產品、擴產），以及估值（P/E、EV/EBITDA、P/B）。
6. **趨勢**：3–5 個長期順風、主要逆風、技術顛覆、政策法規（關稅、補貼、出口管制）。

數字要記下時間點與出處；不同來源差異大時兩個都記。產業資料過時得很快，舊資料要標日期。

## 分析與輸出

### 1. 摘要
三到五點：產業目前所處的循環位置、最大的結構性變化、誰是贏家誰在失守。

### 2. 產業概況
市場規模與成長（附出處）、區隔、價值鏈與利潤落點、進入門檻。
區分「炒作中的 TAM」與實際可觸及的市場。

### 3. 競爭格局比較表
Markdown 表格：公司｜營收｜成長率｜毛利率／營益率｜市佔率｜關鍵差異化｜估值倍數。
**同一欄位必須同一期間、同一定義**；做不到就註明。

### 4. 競爭動態
各家靠什麼競爭（價格、技術、客戶綁定、產能）、誰在搶市佔誰在流失、為什麼；
新進者或跨界者的威脅。

### 5. 估值脈絡
產業目前倍數相對歷史區間的位置、哪些公司享有溢價及其理由（成長、利潤、地位）。

### 6. 投資意涵
最佳風險報酬落在哪個環節；多空雙方的主要辯論；可能改變產業敘事的催化劑與時間點。

來源互相矛盾時：優先採用一手資料（公司財報、官方統計）勝過二手報導，並說明取捨。
