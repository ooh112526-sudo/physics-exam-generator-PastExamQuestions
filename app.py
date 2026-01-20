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
from streamlit_cropper import st_cropper 
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

    def upload_bytes(self, file_bytes, filename, folder="uploads", content_type=None):
        if not self.storage_client: return None
        try:
            target_bucket_name = self.bucket_name
            if not target_bucket_name:
                try:
                    if "GCS_BUCKET_NAME" in st.secrets:
                        target_bucket_name = st.secrets["GCS_BUCKET_NAME"]
                except: pass
            
            if not target_bucket_name:
                st.error("未設定 Bucket 名稱")
                return None

            bucket = self.storage_client.bucket(target_bucket_name)
            # 使用固定檔名邏輯或加蓋 UUID 視需求而定，這裡維持不變以確保唯一性
            unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
            blob = bucket.blob(unique_name)
            blob.upload_from_string(file_bytes, content_type=content_type)
            
            try:
                url = blob.generate_signed_url(
                    version="v4",
                    expiration=datetime.timedelta(days=7),
                    method="GET",
                    service_account_email=self.credentials.service_account_email if hasattr(self.credentials, 'service_account_email') else None,
                    access_token=self.credentials.token if hasattr(self.credentials, 'token') else None
                )
                return url
            except:
                return blob.public_url 

        except Exception as e:
            print(f"上傳失敗: {e}")
            return None

    # --- 檔案庫管理功能 ---
    
    # [新增] 檢查檔案是否已存在
    def check_file_exists(self, filename):
        """檢查 Firestore 中是否有同名檔案"""
        if not self.db: return None
        try:
            # 查詢 exam_files 集合中 filename 欄位等於 filename 的文件
            docs = self.db.collection("exam_files").where("filename", "==", filename).limit(1).stream()
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                return data # 回傳已存在的檔案資料
            return None
        except Exception as e:
            print(f"檢查檔案失敗: {e}")
            return None

    def save_file_record(self, file_info, overwrite_id=None):
        """儲存或更新檔案記錄"""
        if not self.db: return False
        try:
            # 如果是覆蓋，使用舊 ID；否則生成新 ID
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

    # --- 題庫管理功能 ---
    def save_question(self, question_dict):
        if not self.db: return False
        try:
            if question_dict.get("image_data_b64"):
                try:
                    img_bytes = base64.b64decode(question_dict["image_data_b64"])
                    fname = f"q_{question_dict.get('id', 'unknown')}.png"
                    img_url = self.upload_bytes(img_bytes, fname, folder="question_images", content_type="image/png")
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
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "chapter": self.chapter,
            "content": self.content,
            "options": self.options,
            "answer": self.answer,
            "image_data_b64": img_str, 
            "image_url": self.image_url,
            "parent_id": self.parent_id,
            "is_group_parent": self.is_group_parent,
            "sub_questions": subs,
            "source_file_id": self.source_file_id
        }

    @staticmethod
    def from_dict(data):
        img_bytes = None
        img_url = data.get("image_url")
        if data.get("image_data_b64"):
            try:
                img_bytes = base64.b64decode(data["image_data_b64"])
            except: pass
        
        q = Question(
            q_type=data.get("type", "Single"),
            content=data.get("content", ""),
            options=data.get("options", []),
            answer=data.get("answer", ""),
            original_id=0,
            image_data=img_bytes,
            image_url=img_url,
            source=data.get("source", ""),
            chapter=data.get("chapter", "未分類"),
            db_id=data.get("id"),
            parent_id=data.get("parent_id"),
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
        type_label = {'Single': '【單選】', 'Multi': '【多選】', 'Fill': '【填充】', 'Group': '【題組】'}.get(q.type, '')
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

def process_single_file(filename, api_key, file_id_in_db=None):
    if filename not in st.session_state['file_queue']: return
    info = st.session_state['file_queue'][filename]
    info['status'] = 'processing'
    
    with st.spinner(f"正在分析 {filename}..."):
        res = smart_importer.parse_with_gemini(info['data'], info['type'], api_key)
    
    if isinstance(res, dict) and "error" in res:
        info['status'] = 'error'
        info['error_msg'] = res['error']
        st.error(f"{filename} 辨識失敗: {res['error']}")
    else:
        info['status'] = 'done'
        info['result'] = res
        if file_id_in_db:
            cloud_manager.update_file_status(file_id_in_db, "已辨識")
        st.success(f"{filename} 辨識完成！")
        
    st.rerun()

# ==========================================
# Interface
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

with st.sidebar:
    st.header("設定")
    env_api_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_api_key, type="password")
    
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
    
    if st.button("強制儲存至雲端"):
        if cloud_manager.has_connection:
            progress_bar = st.progress(0)
            total = len(st.session_state['question_pool'])
            for i, q in enumerate(st.session_state['question_pool']):
                cloud_manager.save_question(q.to_dict())
                progress_bar.progress((i + 1) / total)
            st.success("儲存完成！")

tab_files, tab_upload_process, tab_review, tab_bank = st.tabs(["📂 檔案庫管理", "🧠 上傳與辨識", "📝 匯入校對", "📚 題庫管理"])

# === Tab 1: 檔案庫管理 ===
with tab_files:
    st.subheader("已上傳考古題檔案庫")
    cloud_files = cloud_manager.load_file_records()
    
    if not cloud_files:
        st.info("目前沒有已上傳的檔案記錄。請至「上傳與辨識」分頁新增。")
    else:
        col_header1, col_header2, col_header3, col_header4, col_header5 = st.columns([2, 1, 1, 1, 2])
        col_header1.markdown("**檔案名稱**")
        col_header2.markdown("**考試類型**")
        col_header3.markdown("**年度**")
        col_header4.markdown("**AI 狀態**")
        col_header5.markdown("**操作**")
        st.divider()

        for f_record in cloud_files:
            c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 2])
            with c1:
                st.write(f"📄 {f_record.get('filename', '未知')}")
                if f_record.get('url'):
                    st.caption(f"[下載原始檔]({f_record.get('url')})")
            with c2: st.write(f_record.get('exam_type', '-'))
            with c3: st.write(f_record.get('year', '-'))
            with c4:
                status = f_record.get('ai_status', '未辨識')
                if status == '已辨識': st.success("已辨識")
                elif status == '處理中': st.warning("處理中")
                else: st.info("未辨識")
            with c5:
                if st.button("AI 辨識", key=f"ai_{f_record['id']}"):
                    fname = f_record['filename']
                    if fname not in st.session_state['file_queue']:
                        try:
                            file_url = f_record.get('url')
                            if file_url:
                                resp = requests.get(file_url)
                                if resp.status_code == 200:
                                    st.session_state['file_queue'][fname] = {
                                        "status": "uploaded", 
                                        "data": resp.content,
                                        "type": fname.split('.')[-1].lower(),
                                        "result": [],
                                        "error_msg": "",
                                        "source_tag": f"{f_record.get('exam_type','')}-{f_record.get('year','')}",
                                        "backup_url": file_url,
                                        "db_id": f_record['id']
                                    }
                                    process_single_file(fname, api_key_input, f_record['id'])
                                else: st.error("無法從雲端下載檔案")
                        except Exception as e: st.error(f"下載失敗: {e}")
                    else:
                        process_single_file(fname, api_key_input, f_record['id'])

                if st.button("刪除", key=f"del_f_{f_record['id']}", type="primary"):
                    cloud_manager.delete_file_record(f_record['id'])
                    st.rerun()
            st.divider()

# === Tab 2: 上傳與辨識 (含重複檢查) ===
with tab_upload_process:
    st.markdown("### 📤 上傳新考古題")
    
    # 使用 Form 之前，先上傳檔案以檢查重複
    # 為了實現「上傳前檢查」，我們不能把 file_uploader 放在 form 裡面，因為 form 只在 submit 時傳送
    # 所以這裡將流程拆開：先上傳 -> 檢查 -> 填寫資料 -> 確認儲存
    
    uploaded_files = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'], accept_multiple_files=True)
    
    # 檢查是否有重複檔案
    duplicate_files = []
    if uploaded_files:
        for f in uploaded_files:
            existing = cloud_manager.check_file_exists(f.name)
            if existing:
                duplicate_files.append((f, existing))
    
    # 顯示重複警告與操作選項
    files_to_process = []
    if duplicate_files:
        st.warning(f"⚠️ 發現 {len(duplicate_files)} 個檔名重複的檔案！")
        
        # 讓使用者決定每個重複檔案的處理方式
        overwrite_decisions = {} # {filename: True/False}
        
        for f, existing_record in duplicate_files:
            col_warn, col_opt = st.columns([3, 1])
            with col_warn:
                st.markdown(f"**{f.name}** (上次上傳：{existing_record.get('updated_at', '未知時間')})")
            with col_opt:
                # 預設不覆蓋 (False)
                decision = st.radio(f"處理方式 ({f.name})", ["跳過", "覆蓋"], key=f"dup_{f.name}")
                if decision == "覆蓋":
                    files_to_process.append((f, existing_record['id'])) # 傳入舊 ID 以便更新
                else:
                    st.caption("將略過此檔案")
    
    # 加入非重複的檔案
    if uploaded_files:
        for f in uploaded_files:
            is_dup = False
            for dup_f, _ in duplicate_files:
                if f.name == dup_f.name:
                    is_dup = True
                    break
            if not is_dup:
                files_to_process.append((f, None)) # None 代表新檔案

    # 顯示 metadata 表單 (只有當有檔案要處理時)
    if files_to_process:
        st.divider()
        st.info(f"準備處理 {len(files_to_process)} 個檔案")
        
        with st.form("upload_meta_form"):
            col_m1, col_m2, col_m3 = st.columns(3)
            with col_m1:
                u_type = st.selectbox("考試類型", ["學測", "分科", "北模", "中模", "全模", "其他"])
            with col_m2:
                u_year = st.text_input("年度 (例如 112)", value="112")
            with col_m3:
                u_exam_no = st.selectbox("考試次別", ["第一次", "第二次", "第三次", "正式考試"])
            
            submitted = st.form_submit_button("確認上傳")
            
            if submitted:
                success_count = 0
                progress_bar = st.progress(0)
                
                for idx, (f, old_id) in enumerate(files_to_process):
                    file_bytes = f.read()
                    f.seek(0) # 重置指標，確保讀取正確
                    
                    # 1. 上傳 Storage (覆蓋同名檔案)
                    backup_url = cloud_manager.upload_bytes(
                        file_bytes, 
                        f.name, 
                        folder="raw_uploads", 
                        content_type=f.type
                    )
                    
                    # 2. 寫入/更新 Firestore
                    file_record = {
                        "filename": f.name,
                        "url": backup_url,
                        "exam_type": u_type,
                        "year": u_year,
                        "exam_no": u_exam_no,
                        "ai_status": "未辨識", # 重新上傳後狀態重置
                    }
                    
                    # 如果是覆蓋，save_file_record 會使用 old_id
                    cloud_manager.save_file_record(file_record, overwrite_id=old_id)
                    
                    # 3. 加入本地暫存
                    st.session_state['file_queue'][f.name] = {
                        "status": "uploaded", 
                        "data": file_bytes,
                        "type": f.name.split('.')[-1].lower(),
                        "result": [],
                        "error_msg": "",
                        "source_tag": f"{u_type}-{u_year}",
                        "backup_url": backup_url,
                    }
                    success_count += 1
                    progress_bar.progress((idx + 1) / len(files_to_process))
                
                if success_count > 0:
                    st.success(f"已成功處理 {success_count} 個檔案！")
                    time.sleep(1)
                    st.rerun()

# === Tab 3: 匯入校對 ===
with tab_review:
    st.subheader("匯入校對與截圖")
    ready_files = [f for f, info in st.session_state['file_queue'].items() if info['status'] == 'done']
    
    if not ready_files:
        st.warning("沒有已完成辨識的檔案。請先至「檔案庫管理」點擊辨識，或上傳新檔案。")
    else:
        selected_file = st.selectbox("選擇要處理的檔案", ready_files)
        file_info = st.session_state['file_queue'][selected_file]
        candidates = file_info['result']
        
        st.markdown(f"**正在編輯：{selected_file} (共 {len(candidates)} 題)**")
        col_src1, col_src2 = st.columns(2)
        with col_src1:
            default_tag = file_info.get("source_tag", "未分類")
            source_tag = st.text_input("設定此批試卷來源標籤", value=default_tag)
        
        st.divider()
        
        with st.form(key=f"edit_form_{selected_file}"):
            for i, cand in enumerate(candidates):
                st.markdown(f"**第 {cand.number} 題**")
                c1, c2 = st.columns([1, 1])
                with c1:
                    cand.content = st.text_area(f"題目內容 #{i}", cand.content, height=100, key=f"{selected_file}_c_{i}")
                    opts_text = "\n".join(cand.options)
                    new_opts = st.text_area(f"選項 #{i}", opts_text, height=80, key=f"{selected_file}_o_{i}")
                    cand.options = new_opts.split('\n') if new_opts else []
                    type_idx = ["Single", "Multi", "Fill"].index(cand.q_type) if cand.q_type in ["Single", "Multi", "Fill"] else 0
                    cand.q_type = st.selectbox(f"題型 #{i}", ["Single", "Multi", "Fill"], index=type_idx, key=f"{selected_file}_t_{i}")
                    ans_key = f"{selected_file}_ans_{i}"
                    default_ans = st.session_state.get(ans_key, "")
                    st.text_input(f"答案 (可留空) #{i}", value=default_ans, key=ans_key)
                    chap_idx = 0
                    if cand.predicted_chapter in smart_importer.PHYSICS_CHAPTERS_LIST:
                        chap_idx = smart_importer.PHYSICS_CHAPTERS_LIST.index(cand.predicted_chapter)
                    cand.predicted_chapter = st.selectbox(f"章節分類 #{i}", smart_importer.PHYSICS_CHAPTERS_LIST, index=chap_idx, key=f"{selected_file}_ch_{i}")
                    if cand.image_bytes: st.image(cand.image_bytes, caption="目前附圖", width=200)
                    else: st.caption("🚫 目前無附圖")

                with c2:
                    st.markdown("✂️ **截圖工具**")
                    image_to_crop = cand.ref_image_bytes if cand.ref_image_bytes else cand.full_page_bytes
                    if image_to_crop:
                        try:
                            pil_ref = Image.open(io.BytesIO(image_to_crop))
                            st_cropper(
                                pil_ref, realtime_update=True, box_color='#FF0000',
                                key=f"{selected_file}_cropper_{i}", aspect_ratio=None
                            )
                            st.caption("提示：截圖需在 Form 提交後或獨立操作")
                        except: st.error("截圖載入失敗")
                    else:
                        st.info("無法取得此題的參考圖片 (也無整頁圖片)")
                st.divider()
            
            st.form_submit_button("💾 暫存所有修改 (不會上傳)")
        
        if st.button(f"✅ 確認匯入 [{selected_file}] 至雲端", type="primary"):
            progress_bar = st.progress(0)
            count = 0
            total = len(candidates)
            db_file_id = file_info.get("db_id")

            for i, cand in enumerate(candidates):
                ans_val = st.session_state.get(f"{selected_file}_ans_{i}", "")
                new_q = Question(
                    q_type=cand.q_type,
                    content=cand.content,
                    options=cand.options,
                    source=source_tag, 
                    chapter=cand.predicted_chapter,
                    image_data=cand.image_bytes,
                    answer=ans_val,
                    source_file_id=db_file_id
                )
                cloud_manager.save_question(new_q.to_dict())
                st.session_state['question_pool'].append(new_q)
                count += 1
                progress_bar.progress((i + 1) / total)
            
            st.success(f"成功匯入 {count} 題！")
            st.session_state['file_queue'][selected_file]['status'] = 'imported'
            if db_file_id:
                cloud_manager.update_file_status(db_file_id, "已匯入")
            st.rerun()

# === Tab 4: 題庫管理 ===
with tab_bank:
    st.subheader("題庫總覽與試卷輸出")
    if not st.session_state['question_pool']:
        st.info("目前沒有題目。")
    else:
        all_sources = sorted(list(set([q.source for q in st.session_state['question_pool']])))
        selected_questions_for_export = []
        for src in all_sources:
            qs_in_src = [q for q in st.session_state['question_pool'] if q.source == src]
            with st.expander(f"📁 {src} ({len(qs_in_src)} 題)"):
                if st.checkbox(f"選取全套 [{src}] 進行匯出", key=f"sel_src_{src}"):
                    selected_questions_for_export.extend(qs_in_src)
                for i, q in enumerate(qs_in_src):
                    type_badge = {'Single': '單', 'Multi': '多', 'Fill': '填', 'Group': '題組'}.get(q.type, '未知')
                    if q.parent_id: continue 
                    st.markdown(f"**[{type_badge}] {q.content[:30]}...**")
                    if q.image_url: st.caption("🖼️ 雲端圖片")
                    elif q.image_data: st.caption("💾 本機圖片 (未同步)")
                    with st.popover("編輯"):
                        q.content = st.text_area("題目", q.content, key=f"edt_c_{q.id}")
                        q.answer = st.text_input("答案", q.answer, key=f"edt_a_{q.id}")
                        if st.button("儲存", key=f"save_{q.id}"):
                            cloud_manager.save_question(q.to_dict())
                            st.rerun()
                        if st.button("刪除", key=f"del_{q.id}", type="primary"):
                            cloud_manager.delete_question(q.id)
                            st.rerun()
                    st.divider()

        st.divider()
        st.subheader(f"已選取 {len(selected_questions_for_export)} 題準備匯出")
        if st.button("生成 Word 試卷"):
            f1, f2 = generate_word_files(selected_questions_for_export)
            st.download_button("下載試題卷", f1, "exam.docx")
            st.download_button("下載答案卷", f2, "ans.docx")
