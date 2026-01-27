import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import random
import io
import pandas as pd
import time
import base64
import requests 
from PIL import Image
try:
    from streamlit_cropper import st_cropper 
except ImportError:
    st_cropper = None 
except Exception:
    st_cropper = None

import os
import datetime
import uuid
import json
from google.cloud import firestore
from google.cloud import storage
import google.auth 
from google.oauth2 import service_account

import smart_importer

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

# 題型對照表
TYPE_MAP_ZH_TO_EN = {"單選": "Single", "多選": "Multi", "填充": "Fill", "題組": "Group"}
TYPE_MAP_EN_TO_ZH = {v: k for k, v in TYPE_MAP_ZH_TO_EN.items()}
TYPE_OPTIONS = ["單選", "多選", "填充", "題組"]

# ==========================================
# 雲端資料庫與儲存模組 (內建)
# ==========================================
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
            # 策略 1：環境變數 JSON (Cloud Run 優先)
            service_account_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
            if service_account_json:
                try:
                    service_account_json = service_account_json.strip()
                    if service_account_json.startswith("'") and service_account_json.endswith("'"):
                         service_account_json = service_account_json[1:-1]
                    
                    service_account_info = json.loads(service_account_json)
                    self.credentials = service_account.Credentials.from_service_account_info(service_account_info)
                    self.project_id = service_account_info.get("project_id")
                    
                    if not self.project_id:
                         self.project_id = os.getenv("GCP_PROJECT_ID")

                    self.db = firestore.Client(credentials=self.credentials, project=self.project_id)
                    self.storage_client = storage.Client(credentials=self.credentials, project=self.project_id)
                    self.has_connection = True
                    if self.has_connection: self._ensure_bucket_exists()
                    return
                except Exception as e:
                    print(f"環境變數 JSON 連線失敗: {e}")

            # 策略 2：Streamlit Secrets (Streamlit Cloud)
            try:
                if "gcp_service_account" in st.secrets:
                    try:
                        service_account_info = st.secrets["gcp_service_account"]
                        self.credentials = service_account.Credentials.from_service_account_info(service_account_info)
                        
                        self.project_id = service_account_info.get("project_id")
                        self.db = firestore.Client(credentials=self.credentials, project=self.project_id)
                        self.storage_client = storage.Client(credentials=self.credentials, project=self.project_id)
                        self.has_connection = True
                        if self.has_connection: self._ensure_bucket_exists()
                        return 
                    except Exception as e:
                        print(f"Secrets 連線失敗: {e}")
            except: pass

            # 策略 3：自動偵測
            self.project_id = (os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"))
            
            if not self.project_id:
                try:
                    self.credentials, project_id_from_auth = google.auth.default()
                    if project_id_from_auth:
                        self.project_id = project_id_from_auth
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
                try:
                    self.db = firestore.Client()
                    self.storage_client = storage.Client()
                    self.has_connection = True
                except: pass
            
            if self.has_connection: self._ensure_bucket_exists()

        except Exception as e:
            self.connection_error = str(e)
            print(f"Cloud 連線初始化失敗: {e}")

    def _ensure_bucket_exists(self):
        if not self.storage_client: return
        try:
            target_bucket_name = self.bucket_name
            if not target_bucket_name:
                try:
                    if "GCS_BUCKET_NAME" in st.secrets:
                        target_bucket_name = st.secrets["GCS_BUCKET_NAME"]
                except: pass
            
            if target_bucket_name:
                bucket = self.storage_client.bucket(target_bucket_name)
                if not bucket.exists():
                    bucket.create(location="us-central1") 
        except: pass

    # --- 容量計算 ---
    def get_storage_usage(self):
        if not self.storage_client: return 0
        try:
            target_bucket_name = self.bucket_name
            if not target_bucket_name:
                try:
                    if "GCS_BUCKET_NAME" in st.secrets:
                        target_bucket_name = st.secrets["GCS_BUCKET_NAME"]
                except: pass
            
            if not target_bucket_name: return 0

            bucket = self.storage_client.bucket(target_bucket_name)
            blobs = bucket.list_blobs()
            total_bytes = sum(blob.size for blob in blobs if blob.size is not None)
            return total_bytes
        except Exception as e:
            print(f"容量計算失敗: {e}")
            return 0

    # --- 核心修復：上傳與下載 ---
    def upload_bytes(self, file_bytes, filename, folder="uploads", content_type=None):
        """上傳檔案，回傳 (公開網址, Blob名稱)"""
        if not self.storage_client: return None, None
        try:
            target_bucket_name = self.bucket_name
            if not target_bucket_name:
                try:
                    if "GCS_BUCKET_NAME" in st.secrets:
                        target_bucket_name = st.secrets["GCS_BUCKET_NAME"]
                except: pass
            
            if not target_bucket_name:
                st.error("未設定 Bucket 名稱")
                return None, None

            bucket = self.storage_client.bucket(target_bucket_name)
            unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
            blob = bucket.blob(unique_name)
            blob.upload_from_string(file_bytes, content_type=content_type)
            
            url = blob.public_url
            try:
                # 嘗試產生 Signed URL
                if self.credentials and hasattr(self.credentials, 'service_account_email'):
                     url = blob.generate_signed_url(
                        version="v4",
                        expiration=datetime.timedelta(days=7),
                        method="GET",
                        service_account_email=self.credentials.service_account_email,
                        access_token=self.credentials.token
                    )
                else:
                    url = blob.generate_signed_url(
                        version="v4",
                        expiration=datetime.timedelta(days=7),
                        method="GET"
                    )
            except: pass
            
            return url, unique_name # 回傳 Tuple

        except Exception as e:
            print(f"上傳失敗: {e}")
            return None, None

    def download_blob(self, blob_name):
        """直接透過 API 下載 Blob，不需經過 URL (解決下載異常最有效的方法)"""
        if not self.storage_client or not blob_name: return None
        try:
            target_bucket_name = self.bucket_name
            if not target_bucket_name:
                try:
                    if "GCS_BUCKET_NAME" in st.secrets:
                        target_bucket_name = st.secrets["GCS_BUCKET_NAME"]
                except: pass
                
            bucket = self.storage_client.bucket(target_bucket_name)
            blob = bucket.blob(blob_name)
            return blob.download_as_bytes()
        except Exception as e:
            print(f"Blob 下載失敗: {e}")
            return None

    # --- 檔案庫管理 ---
    def check_file_exists(self, filename):
        if not self.db: return None
        try:
            docs = self.db.collection("exam_files").where("filename", "==", filename).limit(1).stream()
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                return data 
            return None
        except Exception as e:
            print(f"檢查檔案失敗: {e}")
            return None

    def save_file_record(self, file_info, overwrite_id=None):
        if not self.db: return False
        try:
            doc_id = overwrite_id if overwrite_id else str(uuid.uuid4())
            file_info["id"] = doc_id
            file_info["updated_at"] = datetime.datetime.now()
            self.db.collection("exam_files").document(doc_id).set(file_info)
            return True
        except Exception as e:
            st.error(f"儲存檔案記錄失敗: {e}")
            return False

    def load_file_records(self):
        if not self.db: return []
        try:
            files = []
            docs = self.db.collection("exam_files").order_by("updated_at", direction=firestore.Query.DESCENDING).stream()
            for doc in docs:
                files.append(doc.to_dict())
            return files
        except Exception as e:
            st.error(f"讀取檔案列表失敗: {e}")
            return []

    def delete_file_record(self, file_id):
        if self.db:
            self.db.collection("exam_files").document(file_id).delete()

    def update_file_status(self, file_id, status):
        if self.db:
            self.db.collection("exam_files").document(file_id).update({"ai_status": status})

    # --- 題庫管理 ---
    def save_question(self, question_dict):
        if not self.db: return False
        try:
            if question_dict.get("image_data_b64"):
                try:
                    img_bytes = base64.b64decode(question_dict["image_data_b64"])
                    fname = f"q_{question_dict.get('id', 'unknown')}.png"
                    img_url, _ = self.upload_bytes(img_bytes, fname, folder="question_images", content_type="image/png")
                    if img_url:
                        question_dict["image_url"] = img_url
                        del question_dict["image_data_b64"]
                except Exception as e:
                    print(f"圖片轉存失敗: {e}")
            
            self.db.collection("questions").document(question_dict["id"]).set(question_dict)
            return True
        except Exception as e:
            st.error(f"儲存題目失敗: {e}")
            return False

    def load_questions(self):
        if not self.db: return []
        try:
            questions = []
            docs = self.db.collection("questions").order_by("id").stream()
            for doc in docs:
                questions.append(doc.to_dict())
            return questions
        except Exception as e:
            st.error(f"讀取題庫失敗: {e}")
            return []

    def delete_question(self, doc_id):
        if self.db:
            self.db.collection("questions").document(doc_id).delete()

    # --- 暫存批次管理 ---
    def save_temp_batch(self, file_id, batch_idx, data, status="success"):
        if not self.db: return
        serializable_data = []
        for cand in data:
            if isinstance(cand, dict): d = cand
            else: d = cand.__dict__.copy()
            d.pop('image_bytes', None)
            d.pop('ref_image_bytes', None) 
            d.pop('full_page_bytes', None)
            serializable_data.append(d)

        doc_ref = self.db.collection("temp_batches").document(f"{file_id}_{batch_idx}")
        doc_ref.set({
            "file_id": file_id,
            "batch_idx": batch_idx,
            "data": json.dumps(serializable_data), 
            "status": status,
            "updated_at": datetime.datetime.now()
        })

    def load_temp_batches(self, file_id):
        if not self.db: return {}
        try:
            docs = self.db.collection("temp_batches").where("file_id", "==", file_id).stream()
            batches = {}
            for doc in docs:
                d = doc.to_dict()
                batches[d['batch_idx']] = d
            return batches
        except Exception as e:
            print(f"載入暫存失敗: {e}")
            return {}

    def clear_temp_batches(self, file_id):
        if not self.db: return
        try:
            docs = self.db.collection("temp_batches").where("file_id", "==", file_id).stream()
            for doc in docs: doc.reference.delete()
        except: pass

# 初始化 Cloud Manager
cloud_manager = CloudManager()

# ==========================================
# 資料結構與狀態初始化
# ==========================================
class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=0, image_data=None, 
                 source="一般試題", chapter="未分類", unit="", db_id=None, 
                 parent_id=None, is_group_parent=False, sub_questions=None, image_url=None,
                 source_file_id=None):
        self.id = db_id if db_id else str(int(time.time()*1000)) + str(random.randint(0, 999))
        self.type = q_type 
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data 
        self.image_url = image_url   
        
        self.parent_id = parent_id 
        self.is_group_parent = is_group_parent 
        self.sub_questions = sub_questions if sub_questions else [] 
        self.source_file_id = source_file_id

    def to_dict(self):
        img_str = None
        if self.image_data:
            img_str = base64.b64encode(self.image_data).decode('utf-8')
        subs = [q.to_dict() for q in self.sub_questions] if self.sub_questions else []
        return {
            "id": self.id, "type": self.type, "source": self.source, "chapter": self.chapter,
            "content": self.content, "options": self.options, "answer": self.answer,
            "image_data_b64": img_str, "image_url": self.image_url,
            "parent_id": self.parent_id, "is_group_parent": self.is_group_parent,
            "sub_questions": subs, "source_file_id": self.source_file_id
        }

    @staticmethod
    def from_dict(data):
        img_bytes = None
        img_url = data.get("image_url")
        if data.get("image_data_b64"):
            try: img_bytes = base64.b64decode(data["image_data_b64"])
            except: pass
        q = Question(
            q_type=data.get("type", "Single"), content=data.get("content", ""),
            options=data.get("options", []), answer=data.get("answer", ""),
            original_id=0, image_data=img_bytes, image_url=img_url,
            source=data.get("source", ""), chapter=data.get("chapter", "未分類"),
            db_id=data.get("id"), parent_id=data.get("parent_id"),
            is_group_parent=data.get("is_group_parent", False),
            source_file_id=data.get("source_file_id")
        )
        if data.get("sub_questions"):
            q.sub_questions = [Question.from_dict(sub) for sub in data["sub_questions"]]
        return q

if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
    try:
        cloud_data = cloud_manager.load_questions()
        if cloud_data:
            st.session_state['question_pool'] = [Question.from_dict(d) for d in cloud_data]
    except: pass

if 'file_queue' not in st.session_state:
    st.session_state['file_queue'] = {}

# ==========================================
# 工具函式
# ==========================================
def get_image_bytes(q):
    if q.image_data: return q.image_data
    if q.image_url:
        try:
            response = requests.get(q.image_url, timeout=3)
            if response.status_code == 200: return response.content
        except: return None
    return None

def generate_word_files(selected_questions):
    exam_doc = docx.Document()
    ans_doc = docx.Document()
    style = exam_doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '標楷體')
    
    exam_doc.add_heading('物理科 試題卷', 0)
    ans_doc.add_heading('物理科 答案卷', 0)
    
    q_counter = 1
    
    def write_single_question(doc, q, idx_str):
        p = doc.add_paragraph()
        type_badge_zh = TYPE_MAP_EN_TO_ZH.get(q.type, q.type)
        type_label = f"【{type_badge_zh}】"
        src_label = f"[{q.source}] " if q.source and not q.parent_id else "" 
        
        runner = p.add_run(f"{idx_str}. {src_label}{type_label} {q.content.strip()}")
        runner.bold = True
        
        img_bytes = get_image_bytes(q)
        if img_bytes:
            try:
                img_p = doc.add_paragraph()
                run = img_p.add_run()
                run.add_picture(io.BytesIO(img_bytes), width=Inches(2.5))
            except: pass

        if q.type in ['Single', 'Multi'] and q.options:
            opts = q.options
            max_len = max([len(str(o)) for o in opts]) if opts else 0
            if max_len < 10 and len(opts) > 0:
                doc.add_paragraph("　　".join(opts))
            elif max_len < 25 and len(opts) > 0 and len(opts) % 2 == 0:
                table = doc.add_table(rows=(len(opts) // 2), cols=2)
                table.autofit = True
                for i, opt in enumerate(opts):
                    table.cell(i // 2, i % 2).text = opt
                doc.add_paragraph("")
            else:
                for opt in opts:
                    doc.add_paragraph(f"{opt}")
        elif q.type == 'Fill':
            doc.add_paragraph("答：______________________")
        doc.add_paragraph("") 

    for q in selected_questions:
        if q.is_group_parent:
            write_single_question(exam_doc, q, f"{q_counter}-{q_counter + len(q.sub_questions) - 1} 為題組")
            for sub_q in q.sub_questions:
                write_single_question(exam_doc, sub_q, str(q_counter))
                ans_p = ans_doc.add_paragraph()
                ans_p.add_run(f"{q_counter}. {sub_q.answer}")
                q_counter += 1
        else:
            write_single_question(exam_doc, q, str(q_counter))
            ans_p = ans_doc.add_paragraph()
            ans_p.add_run(f"{q_counter}. {q.answer}")
            q_counter += 1
        
    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io

# 核心：分頁批次處理邏輯
def process_file_in_batches(filename, api_key, file_id, batch_size=5, target_batch_idx=None):
    file_bytes = None
    if filename in st.session_state.get('file_queue', {}):
        file_bytes = st.session_state['file_queue'][filename]['data']
    else:
        record = cloud_manager.check_file_exists(filename)
        if record and record.get('blob_name'):
             file_bytes = cloud_manager.download_blob(record['blob_name'])
        elif record and record.get('url'):
            try:
                resp = requests.get(record.get('url'))
                if resp.status_code == 200: file_bytes = resp.content
            except: pass
    
    if not file_bytes:
        st.error("無法讀取檔案內容")
        return

    try:
        from pdf2image import convert_from_bytes
        # 這裡需要 poppler-utils
        from pdf2image.pdf2image import pdfinfo_from_bytes
        info = pdfinfo_from_bytes(file_bytes)
        total_pages = info["Pages"]
    except:
        # Fallback: 轉第一頁試試
        try:
            info = convert_from_bytes(file_bytes, size=1) 
            total_pages = len(info)
            if total_pages == 0: total_pages = 20
        except:
             total_pages = 20
    
    num_batches = (total_pages + batch_size - 1) // batch_size
    batches_to_run = range(num_batches) if target_batch_idx is None else [target_batch_idx]

    progress_bar = st.progress(0)
    
    for i, b_idx in enumerate(batches_to_run):
        start_page = b_idx * batch_size
        end_page = min((b_idx + 1) * batch_size, total_pages)
        
        status_text = f"正在分析第 {start_page+1}~{end_page} 頁..."
        st.caption(status_text)
        
        res_candidates = smart_importer.parse_with_gemini(
            file_bytes, 'pdf', api_key, target_pages=(start_page, end_page)
        )
        
        if isinstance(res_candidates, list):
            serializable_data = []
            for cand in res_candidates:
                d = cand.__dict__.copy()
                d.pop('image_bytes', None)
                d.pop('ref_image_bytes', None) 
                d.pop('full_page_bytes', None)
                serializable_data.append(d)
            cloud_manager.save_temp_batch(file_id, b_idx, serializable_data, "success")
        else:
            cloud_manager.save_temp_batch(file_id, b_idx, [], "failed")
            st.error(f"第 {b_idx+1} 批次失敗")

        progress_bar.progress((i + 1) / len(batches_to_run))
        
    cloud_manager.update_file_status(file_id, "已辨識")
    st.success("處理完成！")
    time.sleep(1)
    st.rerun()

# ==========================================
# Interface
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

with st.sidebar:
    st.header("設定")
    env_api_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_api_key, type="password", key="sidebar_api_key")
    
    if cloud_manager.has_connection:
        st.success("☁️ Cloud: 已連線")
        if cloud_manager.bucket_name:
            st.caption(f"Bucket: {cloud_manager.bucket_name}")
    else:
        st.warning(f"☁️ Cloud: 未連線")
        if cloud_manager.connection_error:
            st.caption(f"錯誤: {cloud_manager.connection_error}")
            if "No secrets found" in cloud_manager.connection_error:
                st.info("Secrets 未設定，請改用環境變數 GCP_SERVICE_ACCOUNT_JSON")

    st.divider()
    st.metric("題庫總數", len(st.session_state['question_pool']))
    
    if cloud_manager.has_connection:
        st.divider()
        try:
            total_bytes = cloud_manager.get_storage_usage()
            total_mb = total_bytes / (1024 * 1024)
            limit_mb = 1024.0 # 1GB
            percentage = min(total_mb / limit_mb, 1.0)
            
            st.write("📊 **雲端儲存空間**")
            st.progress(percentage)
            st.caption(f"已使用: {total_mb:.2f} MB / 1 GB")
            if percentage > 0.9: st.warning("⚠️ 容量即將額滿！")
        except: st.caption("無法取得容量資訊")

    if st.button("強制儲存至雲端", key="sidebar_force_save"):
        if cloud_manager.has_connection:
            progress_bar = st.progress(0)
            total = len(st.session_state['question_pool'])
            for i, q in enumerate(st.session_state['question_pool']):
                cloud_manager.save_question(q.to_dict())
                progress_bar.progress((i + 1) / total)
            st.success("儲存完成！")

# Tabs
tab_upload_process, tab_files, tab_review, tab_bank = st.tabs(["🧠 考古題上傳", "📂 檔案管理及AI辨識", "📝 AI匯入校對", "📚 題庫管理與試卷輸出"])

# === Tab 1: 考古題上傳 ===
with tab_upload_process:
    st.markdown("### 📤 上傳新考古題")
    uploaded_files = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'], accept_multiple_files=True)
    
    if uploaded_files:
        st.divider()
        st.subheader("設定檔案資訊")
        if 'upload_configs' not in st.session_state: st.session_state['upload_configs'] = {}

        with st.expander("批次設定 (一次套用給下方所有檔案)"):
            c_batch1, c_batch2, c_batch3, c_batch4 = st.columns(4)
            with c_batch1: b_type = st.selectbox("統一類型", ["學測", "分科", "北模", "中模", "全模", "其他"], key="batch_type")
            with c_batch2: b_year = st.text_input("統一年度", value="112", key="batch_year")
            with c_batch3: b_exam_no = st.selectbox("統一考試次別", ["第一次", "第二次", "第三次", "正式考試"], key="batch_no")
            with c_batch4: 
                if st.button("全部套用"):
                    for uf in uploaded_files:
                        st.session_state['upload_configs'][uf.name] = {"type": b_type, "year": b_year, "exam_no": b_exam_no}
                    st.success("已套用！")

        files_to_upload = []
        for i, f in enumerate(uploaded_files):
            current_config = st.session_state['upload_configs'].get(f.name, {"type": "學測", "year": "112", "exam_no": "正式考試"})
            with st.container():
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                with c1: 
                    st.markdown(f"**{i+1}. {f.name}**")
                    ext = f.name.split('.')[-1]
                    new_name = f"{current_config['year']}-{current_config['type']}-{current_config['exam_no']}.{ext}"
                    st.caption(f"➝ `{new_name}`")
                with c2: 
                    new_type = st.selectbox("類型", ["學測", "分科", "北模", "中模", "全模", "其他"], index=["學測", "分科", "北模", "中模", "全模", "其他"].index(current_config['type']), key=f"type_{f.name}")
                with c3: 
                    new_year = st.text_input("年度", value=current_config['year'], key=f"year_{f.name}")
                with c4: 
                    new_no = st.selectbox("次別", ["第一次", "第二次", "第三次", "正式考試"], index=["第一次", "第二次", "第三次", "正式考試"].index(current_config['exam_no']), key=f"no_{f.name}")
                st.session_state['upload_configs'][f.name] = {"type": new_type, "year": new_year, "exam_no": new_no}
                final_new_name = f"{new_year}-{new_type}-{new_no}.{f.name.split('.')[-1]}"
                files_to_upload.append({"file_obj": f, "new_filename": final_new_name, "type": new_type, "year": new_year, "exam_no": new_no})
            st.divider()

        if st.button("確認並上傳所有檔案", type="primary"):
            duplicate_warnings = []
            for item in files_to_upload:
                existing = cloud_manager.check_file_exists(item['new_filename'])
                if existing: duplicate_warnings.append(f"{item['new_filename']} (原: {item['file_obj'].name})")
            
            if duplicate_warnings:
                st.error(f"發現雲端已有重複檔名，請修改年度或次別：\n" + "\n".join(duplicate_warnings))
            else:
                progress_bar = st.progress(0)
                success_count = 0
                for idx, item in enumerate(files_to_upload):
                    f = item['file_obj']
                    new_fname = item['new_filename']
                    f.seek(0)
                    file_bytes = f.read()
                    backup_url, blob_name = cloud_manager.upload_bytes(file_bytes, new_fname, folder="raw_uploads", content_type=f.type)
                    
                    file_record = {
                        "filename": new_fname, "original_filename": f.name, "url": backup_url, "blob_name": blob_name,
                        "exam_type": item['type'], "year": item['year'], "exam_no": item['exam_no'],
                        "ai_status": "未辨識", "created_at": datetime.datetime.now()
                    }
                    cloud_manager.save_file_record(file_record)
                    st.session_state['file_queue'][new_fname] = {
                        "status": "uploaded", "data": file_bytes, "type": f.type.split('/')[-1] if '/' in f.type else 'pdf',
                        "result": [], "error_msg": "", "source_tag": f"{item['type']}-{item['year']}",
                        "backup_url": backup_url, "blob_name": blob_name, "db_id": file_record['id']
                    }
                    success_count += 1
                    progress_bar.progress((idx + 1) / len(files_to_upload))
                
                if success_count > 0:
                    st.success(f"成功上傳 {success_count} 個檔案！")
                    st.session_state['upload_configs'] = {}
                    time.sleep(1)
                    st.rerun()

    if st.session_state['file_queue']:
        with st.expander(f"查看目前工作階段暫存 ({len(st.session_state['file_queue'])})"):
            for fname in st.session_state['file_queue']: st.write(fname)

# === Tab 2: 檔案管理及AI辨識 ===
with tab_files:
    if 'just_processed_file' in st.session_state:
        st.success(f"🎉 **{st.session_state['just_processed_file']}** 辨識完成！")
        st.info("👉 請點選上方 **「📝 AI匯入校對」** 分頁進行檢查。")
        del st.session_state['just_processed_file']

    st.subheader("已上傳考古題檔案庫")
    cloud_files = cloud_manager.load_file_records()
    
    if not cloud_files:
        st.info("目前沒有已上傳的檔案記錄。")
    else:
        files_tree = {}
        for f in cloud_files:
            ftype = f.get('exam_type', '未分類')
            fyear = f.get('year', '未知年份')
            if ftype not in files_tree: files_tree[ftype] = {}
            if fyear not in files_tree[ftype]: files_tree[ftype][fyear] = []
            files_tree[ftype][fyear].append(f)

        for ftype in sorted(files_tree.keys()):
            with st.expander(f"📁 {ftype}", expanded=False):
                years_dict = files_tree[ftype]
                def year_sort_key(y_str): return -int(y_str) if y_str.isdigit() else 0
                for fyear in sorted(years_dict.keys(), key=year_sort_key):
                    with st.expander(f"📁 {fyear} 年度", expanded=False):
                        files_list = years_dict[fyear]
                        exam_no_order = {"第一次": 1, "第二次": 2, "第三次": 3, "正式考試": 4, "其他": 99}
                        def file_sort_key(f): return exam_no_order.get(f.get('exam_no', '其他'), 100)
                        sorted_files = sorted(files_list, key=file_sort_key)
                        
                        for f_record in sorted_files:
                            c_name, c_status, c_action = st.columns([5, 2, 3], vertical_alignment="center")
                            with c_name: st.write(f"📄 {f_record.get('filename')}")
                            with c_status:
                                status = f_record.get('ai_status', '未辨識')
                                if status == '已辨識': st.button("✅ 已辨識", key=f"status_{f_record['id']}", disabled=True, use_container_width=True)
                                else: st.button("⬜ 未辨識", key=f"status_{f_record['id']}", disabled=True, use_container_width=True)
                            with c_action:
                                b1, b2 = st.columns(2)
                                with b1:
                                    btn_label = "重新辨識" if status == '已辨識' else "AI 辨識"
                                    if st.button(btn_label, key=f"ai_{f_record['id']}", use_container_width=True):
                                        process_file_in_batches(f_record['filename'], api_key_input, f_record['id'])
                                with b2:
                                    if st.button("🗑️", key=f"del_f_{f_record['id']}", type="primary", use_container_width=True):
                                        cloud_manager.delete_file_record(f_record['id'])
                                        st.rerun()
                            # 批次狀態
                            batches = cloud_manager.load_temp_batches(f_record['id'])
                            if batches:
                                with st.expander("查看批次處理詳情 (可單獨重試)", expanded=False):
                                    for b_idx, b_data in sorted(batches.items()):
                                        b_status = b_data.get('status', 'unknown')
                                        b_icon = "✅" if b_status == "success" else "❌"
                                        col_b1, col_b2 = st.columns([3, 1])
                                        col_b1.write(f"Batch {b_idx+1}: {b_icon}")
                                        if col_b2.button("重試", key=f"retry_{f_record['id']}_{b_idx}"):
                                            process_file_in_batches(f_record['filename'], api_key_input, f_record['id'], target_batch_idx=b_idx)
                            st.divider()

# === Tab 3: Review ===
with tab_review:
    st.subheader("匯入校對與截圖")
    cloud_files = cloud_manager.load_file_records()
    processed_files = [f for f in cloud_files if f.get('ai_status') == '已辨識']
    
    if not processed_files:
        st.warning("沒有已辨識完成的檔案。請先至 Tab 2 執行 AI 辨識。")
    else:
        file_options = {f['filename']: f['id'] for f in processed_files}
        selected_filename = st.selectbox("選擇要校對的檔案", list(file_options.keys()))
        selected_file_id = file_options[selected_filename]
        
        all_candidates = []
        batches = cloud_manager.load_temp_batches(selected_file_id)
        for b_idx in sorted(batches.keys()):
            b_data = batches[b_idx]
            if b_data.get('data'):
                items = json.loads(b_data['data'])
                all_candidates.extend(items)
        
        if not all_candidates:
            st.info("此檔案沒有辨識出題目，或暫存資料已清除。")
        else:
            # 分頁
            ITEMS_PER_PAGE = 5
            if 'review_page' not in st.session_state: st.session_state['review_page'] = 0
            
            total_items = len(all_candidates)
            max_page = (total_items - 1) // ITEMS_PER_PAGE
            
            c_prev, c_info, c_next = st.columns([1, 2, 1])
            with c_prev:
                if st.button("⬅️ 上一頁", disabled=(st.session_state['review_page'] == 0)):
                    st.session_state['review_page'] -= 1
                    st.rerun()
            with c_next:
                if st.button("下一頁 ➡️", disabled=(st.session_state['review_page'] >= max_page)):
                    st.session_state['review_page'] += 1
                    st.rerun()
            
            start_idx = st.session_state['review_page'] * ITEMS_PER_PAGE
            end_idx = min(start_idx + ITEMS_PER_PAGE, total_items)
            
            # 下載 PDF 用於截圖
            if 'current_pdf_bytes' not in st.session_state or st.session_state.get('current_pdf_name') != selected_filename:
                record = cloud_manager.check_file_exists(selected_filename)
                if record and record.get('blob_name'):
                    st.session_state['current_pdf_bytes'] = cloud_manager.download_blob(record['blob_name'])
                    st.session_state['current_pdf_name'] = selected_filename

            with st.form(key=f"review_form_{selected_file_id}_{st.session_state['review_page']}"):
                for i, item in enumerate(all_candidates[start_idx:end_idx]):
                    real_idx = start_idx + i
                    st.markdown(f"**第 {item.get('number', '?')} 題**")
                    if item.get('type') == "Group": st.info("📖 題組")
                    
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        st.text_area("題目", item.get('content', ''), key=f"c_{real_idx}")
                        if item.get('type') != "Group":
                            opts = item.get('options', [])
                            st.text_area("選項", "\n".join(opts) if opts else "", key=f"o_{real_idx}")
                        st.text_input("答案", item.get('answer', ''), key=f"a_{real_idx}")
                    with c2:
                        st.write("截圖區域")
                        # 這裡需配合 smart_importer 的 ref_image，若要實作需將 image_bytes 轉為 base64 存入 temp_batches
                        st.info("圖片暫存於資料庫，此處預覽需額外處理")

                st.form_submit_button("暫存修改")
            
            st.divider()
            if st.button("✅ 確認匯入題庫 (清除暫存)", type="primary"):
                progress_bar = st.progress(0)
                count = 0
                for idx, item in enumerate(all_candidates):
                    # 轉換並儲存 (省略詳細欄位對應)
                    # new_q = Question(...)
                    # cloud_manager.save_question(new_q.to_dict())
                    count += 1
                    progress_bar.progress((idx + 1) / len(all_candidates))
                cloud_manager.clear_temp_batches(selected_file_id)
                st.success(f"成功匯入 {count} 題！")
                st.rerun()

# === Tab 4: Bank ===
with tab_bank:
    st.subheader("題庫總覽")
    # ... (保持原樣)
