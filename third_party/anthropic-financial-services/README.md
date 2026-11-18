# Anthropic Financial Services Skills — 授權與出處

`app/backend/skills/library/` 中的 `dcf`、`comps`、`earnings`、`sector` 四個 skill
改編自 [anthropics/financial-services](https://github.com/anthropics/financial-services)，
授權為 Apache License 2.0，全文見同層 [`LICENSE`](LICENSE)。

來源 commit：[`3e32c22`](https://github.com/anthropics/financial-services/tree/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f)（2026-11-12）

| 本專案 skill | 改編自 |
|---|---|
| `@dcf` | [dcf-model](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/dcf-model/SKILL.md) |
| `@comps` | [comps-analysis](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/comps-analysis/SKILL.md) |
| `@財報解讀` | [earnings-analysis](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/agent-plugins/earnings-reviewer/skills/earnings-analysis/SKILL.md) |
| `@產業比較` | [sector-overview](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/agent-plugins/market-researcher/skills/sector-overview/SKILL.md) ＋ [competitive-analysis](https://github.com/anthropics/financial-services/blob/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/financial-analysis/skills/competitive-analysis/SKILL.md) |

## 修改說明（Apache 2.0 第 4(b) 條）

原文是寫給 Claude ＋ Excel／PowerPoint 環境用的（輸出 .xlsx、openpyxl、SEC EDGAR）。
本專案保留其分析方法、公式與檢查清單，改寫為繁體中文、聊天回覆長度、台股情境，
並改用深度研究的網路查證取得資料。各檔案的具體修改寫在其 frontmatter 的 `modifications` 欄位。

`@籌碼分析`（`chips`）為本專案自行撰寫，不屬於上述授權範圍。
