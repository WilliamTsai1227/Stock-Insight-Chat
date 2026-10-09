# 本機測試用的 MCP Server，只在 docker-compose.yml（dev）用到。
# 正式環境（docker-compose.prod.yml）沒有這個服務。
FROM python:3.11-slim

WORKDIR /src
ENV PYTHONUNBUFFERED=1

# 刻意用 2.x，而後端 client 是 1.30.0（被 openai-agents 0.3.3 的 mcp<2 綁住）——
# 真實的外部 MCP Server 本來就可能是任何版本，這樣順便驗協議協商。
# 已實測 1.30.0 client ↔ 2.2.0 server 可正常 list_tools / call_tool。
RUN pip install --no-cache-dir mcp==2.2.0

COPY server.py .

EXPOSE 8080
CMD ["python", "server.py"]
