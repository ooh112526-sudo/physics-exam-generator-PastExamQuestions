import os
import datetime
import uuid
import json
import base64
import streamlit as st
import google.auth
from google.cloud import firestore
from google.cloud import storage
from google.oauth2 import service_account

class CloudManager:
    def __init__(self):
        self.bucket_name = os.getenv("GCS_BUCKET_NAME", "physics-exam-assets")
        self.db = None
        self.storage_client = None
        self.has_connection = False
        self.connection_error = ""
        self.project_id = None
        self.credentials = None 

        try:
            # 1. 嘗試從環境變數讀取 JSON
            service_account_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
            if service_account_json:
                try:
                    service_account_json = service_account_json.strip()
                    if service_account_json.startswith("'") and service_account_json.endswith("'"):
                         service_account_json = service_account_json[1:-1]
                    service_account_info = json.loads(service_account_json)
                    self.credentials = service_account.Credentials.from_service_account_info(service_account_info)
                    self.project_id = service_account_info.get("project_id")
                    if not self.project_id: self.project_id = os.getenv("GCP_PROJECT_ID")
                    self.db = firestore.Client(credentials=self.credentials, project=self.project_id)
                    self.storage_client = storage.Client(credentials=self.credentials, project=self.project_id)
                    self.has_connection = True
                    if self.has_connection: self._ensure_bucket_exists()
                    return
                except Exception as e: print(f"JSON Env Error: {e}")

            # 2. 嘗試從 Streamlit Secrets 讀取
            try:
                if "gcp_service_account" in st.secrets:
                    service_account_info = st.secrets["gcp_service_account"]
                    self.credentials = service_account.Credentials.from_service_account_info(service_account_info)
                    self.project_id = service_account_info.get("project_id")
                    self.db = firestore.Client(credentials=self.credentials, project=self.project_id)
                    self.storage_client = storage.Client(credentials=self.credentials, project=self.project_id)
                    self.has_connection = True
                    return 
            except: pass

            # 3. 嘗試使用預設憑證 (Cloud Run 自動注入)
            self.project_id = (os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"))
            if not self.project_id:
                try: self.credentials, project_id_from_auth = google.auth.default(); self.project_id = project_id_from_auth
                except: pass

            if self.project_id:
                if self.credentials:
                    self.db = firestore.Client(credentials=self.credentials, project=self.project_id)
                    self.storage_client = storage.Client(credentials=self.credentials, project=self.project_id)
                else:
                    self.db = firestore.Client(project=self.project_id)
                    self.storage_client = storage.Client(project=self.project_id)
                self.has_connection = True
            else:
                try: self.db = firestore.Client(); self.storage_client = storage.Client(); self.has_connection = True
                except: pass
            
            if self.has_connection: self._ensure_bucket_exists()

        except Exception as e:
            self.connection_error = str(e)
            print(f"Cloud Init Error: {e}")

    def _ensure_bucket_exists(self):
        if not self.storage_client: return
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            if not bucket.exists(): bucket.create(location="us-central1") 
        except: pass

    def get_storage_usage(self):
        if not self.storage_client: return 0
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            return sum(blob.size for blob in bucket.list_blobs() if blob.size)
        except: return 0

    def upload_bytes(self, file_bytes, filename, folder="uploads", content_type=None):
        if not self.storage_client: return None
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
            blob = bucket.blob(unique_name)
            blob.upload_from_string(file_bytes, content_type=content_type)
            
            url = blob.public_url
            try:
                if self.credentials and hasattr(self.credentials, 'service_account_email'):
                     url = blob.generate_signed_url(version="v4", expiration=datetime.timedelta(days=7), method="GET", service_account_email=self.credentials.service_account_email, access_token=self.credentials.token)
                else:
                    url = blob.generate_signed_url(version="v4", expiration=datetime.timedelta(days=7), method="GET")
            except: pass
            return url, unique_name
        except: return None, None

    def download_blob(self, blob_name):
        if not self.storage_client or not blob_name: return None
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            blob = bucket.blob(blob_name)
            return blob.download_as_bytes()
        except: return None

    # --- File/Question Management ---
    def check_file_exists(self, filename):
        if not self.db: return None
        docs = self.db.collection("exam_files").where("filename", "==", filename).limit(1).stream()
        for doc in docs: d = doc.to_dict(); d['id'] = doc.id; return d
        return None

    def save_file_record(self, file_info, overwrite_id=None):
        if not self.db: return False
        doc_id = overwrite_id if overwrite_id else str(uuid.uuid4())
        file_info["id"] = doc_id; file_info["updated_at"] = datetime.datetime.now()
        self.db.collection("exam_files").document(doc_id).set(file_info)
        return True

    def load_file_records(self):
        if not self.db: return []
        files = []
        docs = self.db.collection("exam_files").order_by("updated_at", direction=firestore.Query.DESCENDING).stream()
        for doc in docs: files.append(doc.to_dict())
        return files

    def delete_file_record(self, file_id):
        if self.db: 
            # 刪除主檔與相關子集合
            doc_ref = self.db.collection("exam_files").document(file_id)
            # 刪除 ai_results 子集合 (Firestore 不會自動刪除子集合，這裡簡化處理，僅刪除主檔參照)
            # 若要完整刪除需遍歷子集合，但為效能考量暫略
            doc_ref.delete()

    def update_file_status(self, file_id, status):
        if self.db: self.db.collection("exam_files").document(file_id).update({"ai_status": status})

    # ==========================================
    # [新功能] 批次處理狀態管理 (AI Processing State)
    # ==========================================
    def init_batch_process(self, file_id, total_batches):
        """初始化批次任務狀態"""
        if not self.db: return
        
        # 設定主狀態
        self.db.collection("exam_files").document(file_id).update({
            "ai_status": "處理中",
            "total_batches": total_batches,
            "processed_batches": 0
        })

        # 初始化每個批次的狀態
        batch_collection = self.db.collection("exam_files").document(file_id).collection("batches")
        for i in range(total_batches):
            # 若該批次不存在才建立，避免覆蓋已完成的進度
            doc_ref = batch_collection.document(str(i))
            if not doc_ref.get().exists:
                doc_ref.set({
                    "batch_index": i,
                    "status": "pending",  # pending, processing, done, error
                    "last_error": "",
                    "updated_at": datetime.datetime.now()
                })

    def get_processing_status(self, file_id):
        """取得目前所有批次的狀態"""
        if not self.db: return []
        
        batches = []
        docs = self.db.collection("exam_files").document(file_id).collection("batches").order_by("batch_index").stream()
        for doc in docs:
            batches.append(doc.to_dict())
        return batches

    def save_batch_result(self, file_id, batch_index, candidates_data, status="done", error_msg=""):
        """儲存單一批次的辨識結果 (JSON)"""
        if not self.db: return

        # 1. 儲存結果 (分開存，避免單一文件過大)
        if status == "done" and candidates_data:
            results_ref = self.db.collection("exam_files").document(file_id).collection("ai_results").document(str(batch_index))
            # 將物件轉為 dict 並存入
            # 注意: candidates_data 應為 list of dict
            results_ref.set({"data": candidates_data})

        # 2. 更新批次狀態
        batch_ref = self.db.collection("exam_files").document(file_id).collection("batches").document(str(batch_index))
        batch_ref.update({
            "status": status,
            "last_error": error_msg,
            "updated_at": datetime.datetime.now()
        })
        
        # 3. 更新主檔進度 (Optional, 用於快速顯示)
        # 這裡不特別計算已完成數，由前端統計即可

    def load_all_ai_results(self, file_id):
        """讀取該檔案所有已完成的 AI 辨識結果"""
        if not self.db: return []
        
        all_results = []
        # 讀取 ai_results 子集合
        docs = self.db.collection("exam_files").document(file_id).collection("ai_results").stream()
        
        # 需要按照 batch_index 排序嗎？通常結果內的題號本身就有順序
        # 為了保險，我們可以把 batch index 也讀出來排序
        temp_data = []
        for doc in docs:
            try:
                batch_idx = int(doc.id)
                data = doc.to_dict().get("data", [])
                temp_data.append((batch_idx, data))
            except: pass
            
        temp_data.sort(key=lambda x: x[0])
        
        for _, data_list in temp_data:
            all_results.extend(data_list)
            
        return all_results

    def reset_batch_status(self, file_id, batch_index):
        """重設特定批次狀態為 pending (用於重試)"""
        if self.db:
            self.db.collection("exam_files").document(file_id).collection("batches").document(str(batch_index)).update({
                "status": "pending",
                "last_error": ""
            })

    # --- Question Management ---
    def save_question(self, question_dict):
        if not self.db: return False
        if question_dict.get("image_data_b64"):
            try:
                img_bytes = base64.b64decode(question_dict["image_data_b64"])
                fname = f"q_{question_dict.get('id')}.png"
                img_url, _ = self.upload_bytes(img_bytes, fname, folder="question_images", content_type="image/png")
                if img_url: question_dict["image_url"] = img_url; del question_dict["image_data_b64"]
            except: pass
        self.db.collection("questions").document(question_dict["id"]).set(question_dict)
        return True

    def load_questions(self):
        if not self.db: return []
        qs = []
        docs = self.db.collection("questions").order_by("id").stream()
        for doc in docs: qs.append(doc.to_dict())
        return qs

    def delete_question(self, doc_id):
        if self.db: self.db.collection("questions").document(doc_id).delete()
