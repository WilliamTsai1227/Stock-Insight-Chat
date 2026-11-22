# 通用行事曆 Lambda（calendar-macro）— container image 部署，與 data/ 的 Qdrant 同步 Lambda 同一套流程
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sources.py lambda_function.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_function.lambda_handler"]
