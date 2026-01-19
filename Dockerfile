# 使用輕量級 Python 3.9
FROM python:3.9-slim

# 設定工作目錄
WORKDIR /app

# 安裝系統依賴 (poppler-utils 用於 pdf2image)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    software-properties-common \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 複製並安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製所有程式碼
COPY . .

# 設定環境變數 (讓 Python print 訊息直接輸出到 Log)
ENV PYTHONUNBUFFERED=1

# 告訴 Cloud Run 我們監聽 8080
EXPOSE 8080

# 啟動 Streamlit，強制設定 Port 為 8080
CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0"]
