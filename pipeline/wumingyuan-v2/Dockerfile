FROM python:3.12-slim

# 安装系统级依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
    ripgrep && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /project/pipeline/wumingyuan-v2

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8888

CMD ["python", "server.py"]
