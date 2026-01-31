# 使用穩定的 Debian Bullseye 基底映像檔 (Python 3.9)
FROM python:3.9-slim-bullseye

# 設定工作目錄
WORKDIR /app

# 安裝系統依賴套件 (Critical for Cloud Run)
# poppler-utils: 讓 pdf2image 可以運作
# tesseract-ocr: 讓 OCR 功能運作
# libgl1: OpenCV/Pillow 處理圖片依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    software-properties-common \
    poppler-utils \
    tesseract-ocr \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# 複製需求清單並安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式程式碼
COPY . .

# 設定環境變數，確保 Log 即時顯示
ENV PYTHONUNBUFFERED=1

# 宣告 Cloud Run 使用的通訊埠 (預設 8080)
EXPOSE 8080

# 啟動應用程式
CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0"]
