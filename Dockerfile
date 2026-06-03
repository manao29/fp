FROM python:3.11-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# OCR (分层缓存)
RUN pip install --no-cache-dir paddlepaddle paddleocr || echo "PaddleOCR install skipped"

# 应用代码
COPY src/ src/
COPY templates/ templates/
COPY .env.example .env.example

# 数据目录
RUN mkdir -p /app/data/uploads /app/data/exports

EXPOSE 8000

CMD ["python", "-m", "src.app"]
