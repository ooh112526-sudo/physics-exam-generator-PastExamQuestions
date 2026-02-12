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
from models import ExamRecord # [Spec] 匯入 ExamRecord
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
            # 處理路徑
            if folder:
                unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
            else:
                unique_name = f"{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
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
    def delete_blob(self, blob_name):
        if not self.storage_client or not blob_name: return
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            blob = bucket.blob(blob_name)
            blob.delete()
            print(f"Deleted blob: {blob_name}")
        except Exception as e:
            print(f"Failed to delete blob {blob_name}: {e}")
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
            doc_ref = self.db.collection("exam_files").document(file_id)
            doc_ref.delete()
    def update_file_status(self, file_id, status):
        if self.db: self.db.collection("exam_files").document(file_id).update({"ai_status": status})
    # ==========================================
    # 批次處理狀態管理 (AI Processing State)
    # ==========================================
    def init_batch_process(self, file_id, total_batches):
        if not self.db: return
        self.db.collection("exam_files").document(file_id).update({
            "ai_status": "處理中",
            "total_batches": total_batches,
            "processed_batches": 0
        })
        batch_collection = self.db.collection("exam_files").document(file_id).collection("batches")
        for i in range(total_batches):
            doc_ref = batch_collection.document(str(i))
            if not doc_ref.get().exists:
                doc_ref.set({
                    "batch_index": i, "status": "pending", "last_error": "", "updated_at": datetime.datetime.now()
                })
    def get_processing_status(self, file_id):
        if not self.db: return []
        batches = []
        docs = self.db.collection("exam_files").document(file_id).collection("batches").order_by("batch_index").stream()
        for doc in docs: batches.append(doc.to_dict())
        return batches
    def save_batch_result(self, file_id, batch_index, candidates_data, status="done", error_msg=""):
        """
        儲存單一批次的辨識結果，並解決 Firestore 1MB 限制問題：
        將 Base64 圖片上傳至 GCS，JSON 中僅保留 URL。
        """
        if not self.db: return
        # 1. 圖片處理：Base64 -> GCS URL
        if status == "done" and candidates_data:
            # 遍歷每個題目
            for item in candidates_data:
                # 定義所有可能包含 Base64 圖片的欄位 (新增 ai_crop_backup_b64)
                image_keys = ['image_b64', 'ref_image_b64', 'full_page_b64', 'ai_crop_backup_b64']
                
                for key in image_keys:
                    if item.get(key):
                        try:
                            # 解碼 Base64
                            img_bytes = base64.b64decode(item[key])
                            
                            # 定義儲存路徑 (temp_images 用於區分暫存)
                            fname = f"{item.get('number', 'unknown')}_{key}.jpg"
                            folder_path = f"temp_images/{file_id}/{batch_index}"
                            
                            # 上傳至 GCS
                            img_url, blob_name = self.upload_bytes(img_bytes, fname, folder=folder_path, content_type="image/jpeg")
                            
                            if img_url:
                                # 替換為 URL
                                url_key = key.replace('_b64', '_url')
                                item[url_key] = img_url
                                # 儲存 blob_name 以便後端下載 (如果 URL 無法公開存取)
                                item[key.replace('_b64', '_blob_name')] = blob_name
                                # 重要：刪除原始 Base64 資料，釋放 JSON 空間
                                del item[key]
                                
                        except Exception as e:
                            print(f"圖片轉存失敗 ({key}): {e}")
                            # 若失敗，至少保留 Base64 以免資料遺失 (但可能導致 Save 失敗)
                            pass
            # 2. 寫入輕量化後的 JSON 至 Firestore
            results_ref = self.db.collection("exam_files").document(file_id).collection("ai_results").document(str(batch_index))
            results_ref.set({"data": candidates_data})
        # 3. 更新批次狀態
        batch_ref = self.db.collection("exam_files").document(file_id).collection("batches").document(str(batch_index))
        batch_ref.update({
            "status": status,
            "last_error": error_msg,
            "updated_at": datetime.datetime.now()
        })
    def load_all_ai_results(self, file_id):
        if not self.db: return []
        all_results = []
        docs = self.db.collection("exam_files").document(file_id).collection("ai_results").stream()
        temp_data = []
        for doc in docs:
            try:
                batch_idx = int(doc.id)
                data = doc.to_dict().get("data", [])
                temp_data.append((batch_idx, data))
            except: pass
        temp_data.sort(key=lambda x: x[0])
        for _, data_list in temp_data: all_results.extend(data_list)
        return all_results
    def reset_batch_status(self, file_id, batch_index):
        if self.db:
            self.db.collection("exam_files").document(file_id).collection("batches").document(str(batch_index)).update({
                "status": "pending", "last_error": ""
            })
    # [Critical Fix] 清除批次資料時，同步刪除 GCS 上的暫存圖片
    def clean_old_batch_data(self, file_id):
        """清除舊的批次處理資料，以便重新辨識"""
        if not self.db: return
        try:
    # 1. 清除 Batches 集合
            batches_ref = self.db.collection("exam_files").document(file_id).collection("batches")
            for doc in batches_ref.stream(): doc.reference.delete()
            # 2. 清除 AI Results 集合，並同步刪除 GCS Blobs
            results_ref = self.db.collection("exam_files").document(file_id).collection("ai_results")
            for doc in results_ref.stream():
                data = doc.to_dict()
                # 掃描並刪除關聯的圖片
                if 'data' in data and isinstance(data['data'], list):
                    for q in data['data']:
                        # 檢查所有可能的圖片欄位 key
                        for key in ['image_blob_name', 'ref_image_blob_name', 'full_page_blob_name', 'ai_crop_backup_blob_name']:
                            if q.get(key):
                                self.delete_blob(q[key]) # 刪除 GCS 檔案
                
                doc.reference.delete() # 刪除 Firestore 文件
        except Exception as e:
            print(f"Cleanup Error: {e}")
    # --- Question Management ---
    def save_question(self, question_dict):
        if not self.db: return False
        # 若是正式入庫，也做一次 Base64 -> URL (如果有的話)
        
        # [Update] 若有新圖片資料，上傳並記錄 blob_name
        if question_dict.get("image_data_b64"):
            try:
                img_bytes = base64.b64decode(question_dict["image_data_b64"])
                fname = f"q_{question_dict.get('id')}.png"
                img_url, blob_name = self.upload_bytes(img_bytes, fname, folder="question_images", content_type="image/png")
                
                if img_url: 
                    question_dict["image_url"] = img_url
                    question_dict["image_blob_name"] = blob_name 
                    del question_dict["image_data_b64"]
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
        if not self.db: return
        
        doc_ref = self.db.collection("questions").document(doc_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            
            # [New] 連動刪除母題圖片
            if data.get('image_blob_name'):
                self.delete_blob(data['image_blob_name'])
                
            # [New] 連動刪除子題圖片
            if data.get('sub_questions'):
                for sub in data['sub_questions']:
                    if sub.get('image_blob_name'): self.delete_blob(sub['image_blob_name'])
            doc_ref.delete()
    
    # ==========================================
    # [Spec] 試卷履歷管理 (Exam History)
    # ==========================================
    def save_exam_history(self, title, question_ids):
        """將生成的試卷存入履歷"""
        if not self.db: return False
        try:
            record = ExamRecord(title, question_ids)
            doc_ref = self.db.collection("exam_history").document() # Auto ID
            doc_ref.set(record.to_dict())
            return True
        except Exception as e:
            print(f"Save History Error: {e}")
            return False
    def load_exam_history(self):
        """讀取所有試卷履歷"""
        if not self.db: return []
        records = []
        try:
            docs = self.db.collection("exam_history").order_by("created_at", direction=firestore.Query.DESCENDING).stream()
            for doc in docs:
                records.append(ExamRecord.from_dict(doc.to_dict(), db_id=doc.id))
        except: pass
        return records
    def delete_exam_history(self, doc_id):
        """刪除試卷履歷 (釋放題目)"""
        if not self.db: return
        self.db.collection("exam_history").document(doc_id).delete()
    def get_used_question_ids(self):
        """取得所有歷史試卷中已使用過的題目 ID 集合"""
        if not self.db: return set()
        used_ids = set()
        try:
            # 為了效能，只抓 question_ids 欄位
            docs = self.db.collection("exam_history").select(["question_ids"]).stream()
            for doc in docs:
                d = doc.to_dict()
                if "question_ids" in d and isinstance(d["question_ids"], list):
                    used_ids.update(d["question_ids"])
        except Exception as e:
            print(f"Get Used IDs Error: {e}")
        return used_ids
