import streamlit as st
import os
import datetime
import uuid
from google.cloud import firestore
from google.cloud import storage
from google.oauth2 import service_account
import base64

# ==========================================
# 初始化設定
# ==========================================
# 嘗試從環境變數讀取 Bucket 名稱，若無則需手動設定
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "physics-exam-assets") 

# 初始化 Firestore 與 Storage Client
try:
    # 優先嘗試使用環境變數中的憑證 (Cloud Run 環境)
    db = firestore.Client()
    storage_client = storage.Client()
    HAS_DB = True
except Exception as e:
    # 本機開發時的 fallback (若未設定 gcloud auth application-default login)
    print(f"Cloud 連線初始化失敗: {e}")
    db = None
    storage_client = None
    HAS_DB = False

def get_db():
    return db

# ==========================================
# Cloud Storage 檔案處理
# ==========================================
def upload_bytes_to_storage(file_bytes, filename, folder="uploads", content_type=None):
    """
    將二進位資料上傳至 GCS，並回傳公開 URL (或 Signed URL)
    """
    if not storage_client:
        return None
    
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        # 生成唯一檔名，避免覆蓋: folder/timestamp_uuid_filename
        unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
        blob = bucket.blob(unique_name)
        
        blob.upload_from_string(file_bytes, content_type=content_type)
        
        # 這裡有兩種做法：
        # 1. 公開讀取 (適合公開題庫): blob.make_public(); return blob.public_url
        # 2. 保持私有 (透過 App 存取): 這裡僅回傳路徑，前端顯示時再動態生成 Signed URL (較安全但複雜)
        # 為了教學工具方便，我們先假設 Bucket 設為 Uniform Access 且允許公開讀取，或者我們只存 gs:// 路徑
        
        # 簡單起見，我們回傳 public_url (需確認 Bucket 權限有開)
        # 若 Bucket 未公開，此 URL 無法直接訪問，需改用 blob.generate_signed_url()
        return blob.public_url
        
    except Exception as e:
        print(f"上傳 Storage 失敗: {e}")
        st.error(f"上傳雲端硬碟失敗: {e}")
        return None

# ==========================================
# Firestore 資料庫處理
# ==========================================
def save_question_to_cloud(question_dict):
    """
    儲存題目到 Firestore。
    若題目包含 Base64 圖片，會先上傳到 Storage 轉成 URL，再存入資料庫。
    """
    if not db:
        return False
    
    try:
        # 1. 處理圖片：將 Base64 轉存為 Storage URL
        if question_dict.get("image_data_b64"):
            try:
                # 解碼 Base64
                img_bytes = base64.b64decode(question_dict["image_data_b64"])
                # 上傳圖片
                fname = f"q_{question_dict.get('id', 'unknown')}.png"
                img_url = upload_bytes_to_storage(img_bytes, fname, folder="question_images", content_type="image/png")
                
                if img_url:
                    question_dict["image_url"] = img_url
                    # 移除肥大的 Base64 字串，節省資料庫空間
                    del question_dict["image_data_b64"]
            except Exception as e:
                print(f"圖片轉存失敗: {e}")

        # 2. 寫入 Firestore
        doc_ref = db.collection("questions").document(question_dict["id"])
        doc_ref.set(question_dict)
        return True
    except Exception as e:
        st.error(f"儲存題目失敗: {e}")
        return False

def load_questions_from_cloud():
    """從 Firestore 載入所有題目"""
    if not db:
        return []
    
    try:
        questions = []
        docs = db.collection("questions").order_by("id").stream()
        for doc in docs:
            questions.append(doc.to_dict())
        return questions
    except Exception as e:
        st.error(f"讀取題庫失敗: {e}")
        return []

def delete_question_from_cloud(doc_id):
    """刪除題目"""
    if db:
        db.collection("questions").document(doc_id).delete()
