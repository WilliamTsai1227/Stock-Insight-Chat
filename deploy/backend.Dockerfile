FROM python:3.11-slim

# 工作目錄設為 /src
WORKDIR /src

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
# 只需要將 /src 加入路徑，Python 就會找到底下的 app 資料夾
ENV PYTHONPATH=/src
# 禁用 HuggingFace 進度條，避免 tqdm 與 asyncio.to_thread 產生 lock 衝突
ENV HF_HUB_DISABLE_PROGRESS_BARS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 安裝依賴
COPY app/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 預下載 fastembed BM25 sparse 模型，避免 runtime 首次初始化時
# 在 asyncio 子執行緒中觸發 tqdm._lock 相容性問題
RUN python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25'); print('✅ BM25 model cached')"

# 只複製 app/backend 目錄到 /src/app/backend
COPY app/backend ./app/backend

# 啟動命令：指向層級明確的 app.backend.app:app
CMD ["uvicorn", "app.backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
