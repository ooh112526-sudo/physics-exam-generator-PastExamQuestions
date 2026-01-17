# 使用輕量級的 Python 映像檔
FROM python:3.9-slim

# 設定工作目錄
WORKDIR /app

# 複製需求文件並安裝
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製所有程式碼
COPY . .

# 告訴 Cloud Run 我們要在 8080 埠口運行
EXPOSE 8080

# 設定 Streamlit 啟動指令
# server.port 8080: 配合 Cloud Run
# server.address 0.0.0.0: 允許外部連線
CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0"]
