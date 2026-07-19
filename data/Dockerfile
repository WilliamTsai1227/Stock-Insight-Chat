# MongoDB → Qdrant 向量同步 Lambda（container image 部署）
# fastembed + onnxruntime 體積超過 Lambda zip 250MB 上限，故採 container image。
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build 時預先下載 BM25 模型烘入映像，避免每次冷啟動連 HuggingFace 下載
ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
RUN python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25', cache_dir='/opt/fastembed_cache')"

COPY lambda_function.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_function.lambda_handler"]
