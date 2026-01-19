# 使用穩定的 Debian Bullseye 基底映像檔，避免軟體源問題
FROM python:3.9-slim-bullseye

# 設定工作目錄
WORKDIR /app

# 安裝系統依賴套件
# poppler-utils: 用於 pdf2image 處理 PDF
# tesseract-ocr: 用於 pytesseract 文字辨識 (若有使用)
# libgl1: OpenCV 或 Pillow 處理圖片時常需要的函式庫
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

# 複製其餘的應用程式程式碼
COPY . .

# 設定環境變數
ENV PYTHONUNBUFFERED=1

# 宣告 Cloud Run 使用的通訊埠 (預設 8080)
EXPOSE 8080

# 啟動應用程式
CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0"]
