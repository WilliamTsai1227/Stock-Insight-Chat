"""本機測試用的 MCP Server（Streamable HTTP）。

只給 docker compose 本機開發用，讓你不必真的找一台外部 MCP Server
就能驗證「@ 提及 → Router 綁定 → 工具被呼叫」整條路。
回傳的是寫死的假資料，不連任何外部服務。

正式環境不要部署這個檔案。
"""
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

mcp = MCPServer(name="market-data", version="0.1.0")

# readOnlyHint 很重要：mcp_client.py 在沒設 allowed_tools 時，
# 只會匯入明確標示唯讀的工具。
READ_ONLY = ToolAnnotations(readOnlyHint=True)


@mcp.tool(description="取得個股即時報價（測試假資料）", annotations=READ_ONLY)
def get_quote(symbol: str) -> str:
    """symbol: 股票代號，例如 2330"""
    return (
        f"[測試資料] {symbol} 現價 1,085 元，漲跌 +12 (+1.12%)，"
        f"成交量 32,451 張，本益比 24.3。"
    )


@mcp.tool(description="取得公司基本資料與產業分類（測試假資料）", annotations=READ_ONLY)
def company_profile(symbol: str) -> str:
    """symbol: 股票代號，例如 2330"""
    return (
        f"[測試資料] {symbol} 台灣積體電路製造股份有限公司，"
        f"產業：半導體／晶圓代工，員工約 76,000 人，總部位於新竹。"
    )


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        streamable_http_path="/mcp",
        # 預設的 DNS rebinding 保護在 allowed_hosts 為空時會拒絕所有 Host
        # （包含 docker 服務名 mcp-test:8080）。測試用 server 直接關閉；
        # 正式的 MCP Server 應該改為明確列出 allowed_hosts。
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
