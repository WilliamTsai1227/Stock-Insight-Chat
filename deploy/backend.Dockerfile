FROM python:3.11-slim

# 工作目錄設為 /src
WORKDIR /src

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
# 只需要將 /src 加入路徑，Python 就會找到底下的 app 資料夾
ENV PYTHONPATH=/src

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 安裝依賴
COPY app/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製整個 app 目錄到 /src/app
COPY app ./app

# 啟動命令：指向層級明確的 app.backend.app:app
CMD ["uvicorn", "app.backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
