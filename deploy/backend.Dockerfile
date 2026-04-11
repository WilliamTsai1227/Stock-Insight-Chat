FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PYTHONPATH=/app

# 安裝編譯依賴 (針對 asyncpg/psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 安裝 Python 依賴
COPY app/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製後端程式與模組
COPY app/backend ./backend

# 啟動命令 (uvicorn 會自動載入模組路徑)
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
