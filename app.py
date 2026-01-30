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
from pdf2image import convert_from_bytes # 用於動態轉圖

import smart_importer

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

# 題型對照表
TYPE_MAP_ZH_TO_EN = {"單選": "Single", "多選": "Multi", "填充": "Fill", "題組": "Group"}
TYPE_MAP_EN_TO_ZH = {v: k for k, v in TYPE_MAP_ZH_TO_EN.items()}
TYPE_OPTIONS = ["單選", "多選", "填充", "題組"]

# ==========================================
# 雲端資料庫與儲存模組 (完整版)
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
                except Exception as e: print(f"JSON Env Error: {e}")

            if not self.has_connection:
                self.project_id = (os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"))
                if not self.project_id:
                     try: _, self.project_id = google.auth.default()
                     except: pass
                
                if self.project_id:
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
        except Exception as e: self.connection_error = str(e)

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
            blobs = bucket.list_blobs()
            return sum(blob.size for blob in blobs if blob.size)
        except: return 0

    def upload_bytes(self, file_bytes, filename, folder="uploads", content_type=None):
        if not self.storage_client: return None, None
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
            blob = bucket.blob(unique_name)
            blob.upload_from_string(file_bytes, content_type=content_type)
            
            url = blob.public_url
            try:
                if self.credentials:
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

    # --- 檔案與暫存 ---
    def check_file_exists(self, filename):
        if not self.db: return None
        docs = self.db.collection("exam_files").where("filename", "==", filename).limit(1).stream()
        for doc in docs: 
            d = doc.to_dict(); d['id'] = doc.id
            return d
        return None

    def save_file_record(self, file_info):
        if not self.db: return False
        if not file_info.get("id"): file_info["id"] = str(uuid.uuid4())
        file_info["updated_at"] = datetime.datetime.now()
        self.db.collection("exam_files").document(file_info["id"]).set(file_info)
        return True

    def load_file_records(self):
        if not self.db: return []
        files = []
        docs = self.db.collection("exam_files").order_by("updated_at", direction=firestore.Query.DESCENDING).stream()
        for doc in docs: files.append(doc.to_dict())
        return files

    def delete_file_record(self, file_id):
        if self.db:
            self.db.collection("exam_files").document(file_id).delete()
            self.clear_temp_batches(file_id)

    def update_file_status(self, file_id, status):
        if self.db:
            self.db.collection("exam_files").document(file_id).update({"ai_status": status})

    # [關鍵優化] 暫存批次管理
    def save_temp_batch(self, file_id, batch_idx, data, status="success"):
        if not self.db: return
        serializable_data = []
        for cand in data:
            if isinstance(cand, dict): d = cand
            else: d = cand.__dict__.copy()
            # 移除二進位資料，只存文字與座標
            d.pop('image_bytes', None)
            d.pop('ref_image_bytes', None) 
            d.pop('full_page_bytes', None)
            serializable_data.append(d)
        
        self.db.collection("temp_batches").document(f"{file_id}_{batch_idx}").set({
            "file_id": file_id, "batch_idx": batch_idx, "data": json.dumps(serializable_data),
            "status": status, "updated_at": datetime.datetime.now()
        })

    def load_temp_batches(self, file_id):
        if not self.db: return {}
        docs = self.db.collection("temp_batches").where("file_id", "==", file_id).stream()
        batches = {}
        for doc in docs:
            d = doc.to_dict()
            batches[d['batch_idx']] = d
        return batches

    def clear_temp_batches(self, file_id):
        if not self.db: return
        docs = self.db.collection("temp_batches").where("file_id", "==", file_id).stream()
        for doc in docs: doc.reference.delete()

    def save_question(self, question_dict):
        if not self.db: return False
        if question_dict.get("image_data_b64"):
            try:
                img_bytes = base64.b64decode(question_dict["image_data_b64"])
                fname = f"q_{question_dict.get('id')}.png"
                img_url, _ = self.upload_bytes(img_bytes, fname, folder="question_images", content_type="image/png")
                if img_url: 
                    question_dict["image_url"] = img_url
                    del question_dict["image_data_b64"]
            except: pass
        self.db.collection("questions").document(question_dict["id"]).set(question_dict)
        return True

    def load_questions(self):
        if not self.db: return []
        questions = []
        docs = self.db.collection("questions").order_by("id").stream()
        for doc in docs: questions.append(doc.to_dict())
        return questions

    def delete_question(self, doc_id):
        if self.db: self.db.collection("questions").document(doc_id).delete()

cloud_manager = CloudManager()

# ... (Question Class) ...
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
        if self.image_data: img_str = base64.b64encode(self.image_data).decode('utf-8')
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
        if data.get("image_data_b64"):
            try: img_bytes = base64.b64decode(data["image_data_b64"])
            except: pass
        q = Question(
            q_type=data.get("type", "Single"), content=data.get("content", ""),
            options=data.get("options", []), answer=data.get("answer", ""),
            image_data=img_bytes, image_url=data.get("image_url"),
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
        data = cloud_manager.load_questions()
        if data: st.session_state['question_pool'] = [Question.from_dict(d) for d in data]
    except: pass

if 'file_queue' not in st.session_state: st.session_state['file_queue'] = {}

# --- Helper Functions ---
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
            if max_len < 10 and len(opts) > 0: doc.add_paragraph("　　".join(opts))
            elif max_len < 25 and len(opts) > 0 and len(opts) % 2 == 0:
                table = doc.add_table(rows=(len(opts) // 2), cols=2)
                table.autofit = True
                for i, opt in enumerate(opts): table.cell(i // 2, i % 2).text = opt
                doc.add_paragraph("")
            else:
                for opt in opts: doc.add_paragraph(f"{opt}")
        elif q.type == 'Fill': doc.add_paragraph("答：______________________")
        doc.add_paragraph("") 

    for q in selected_questions:
        if q.is_group_parent:
            write_single_question(exam_doc, q, "題組")
            for sub in q.sub_questions:
                write_single_question(exam_doc, sub, str(q_counter))
                ans_doc.add_paragraph(f"{q_counter}. {sub.answer}")
                q_counter += 1
        else:
            write_single_question(exam_doc, q, str(q_counter))
            ans_doc.add_paragraph(f"{q_counter}. {q.answer}")
            q_counter += 1
    f1, f2 = io.BytesIO(), io.BytesIO()
    exam_doc.save(f1); ans_doc.save(f2)
    f1.seek(0); f2.seek(0)
    return f1, f2

# [核心功能] 分頁批次處理邏輯 (解決 OOM 與 TimeOut)
def process_file_in_batches(filename, api_key, file_id, batch_size=5, target_batch_idx=None):
    file_bytes = None
    if filename in st.session_state.get('file_queue', {}):
        file_bytes = st.session_state['file_queue'][filename]['data']
    else:
        # 從雲端重新下載檔案
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

    # 計算頁數
    try:
        from pdf2image.pdf2image import pdfinfo_from_bytes
        info = pdfinfo_from_bytes(file_bytes)
        total_pages = info["Pages"]
    except:
        try:
            info = convert_from_bytes(file_bytes, size=1) 
            total_pages = len(info)
            if total_pages == 0: total_pages = 20
        except: total_pages = 20
    
    num_batches = (total_pages + batch_size - 1) // batch_size
    batches_to_run = range(num_batches) if target_batch_idx is None else [target_batch_idx]

    progress_bar = st.progress(0)
    for i, b_idx in enumerate(batches_to_run):
        start_page = b_idx * batch_size
        end_page = min((b_idx + 1) * batch_size, total_pages)
        st.caption(f"正在分析第 {start_page+1}~{end_page} 頁...")
        
        # 呼叫 smart_importer (帶入頁數範圍)
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
                # 確保座標存在
                if not d.get('full_question_box_2d'): d['full_question_box_2d'] = None
                serializable_data.append(d)
            # 存入暫存
            cloud_manager.save_temp_batch(file_id, b_idx, serializable_data, "success")
        else:
            cloud_manager.save_temp_batch(file_id, b_idx, [], "failed")
            st.error(f"第 {b_idx+1} 批次失敗")
        progress_bar.progress((i + 1) / len(batches_to_run))
    
    cloud_manager.update_file_status(file_id, "已辨識")
    st.success("處理完成！")
    st.session_state['just_processed_file'] = filename
    time.sleep(1)
    st.rerun()

# [核心功能] 動態生成校對圖片 (解決截圖空白與 OOM)
def get_page_image(pdf_bytes, page_index):
    try:
        images = convert_from_bytes(pdf_bytes, first_page=page_index+1, last_page=page_index+1, dpi=100, fmt='jpeg')
        if images:
            img_byte_arr = io.BytesIO()
            images[0].save(img_byte_arr, format='JPEG')
            return img_byte_arr.getvalue()
    except Exception as e: 
        return None
    return None

# ==========================================
# UI
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

with st.sidebar:
    st.header("設定")
    env_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_key, type="password", key="sidebar_api_key")
    if cloud_manager.has_connection:
        st.success("☁️ Cloud: 已連線")
        if cloud_manager.bucket_name: st.caption(f"Bucket: {cloud_manager.bucket_name}")
    else:
        st.warning("☁️ Cloud: 未連線")
    st.divider()
    st.metric("題庫總數", len(st.session_state['question_pool']))
    
    if cloud_manager.has_connection:
        st.divider()
        try:
            total_bytes = cloud_manager.get_storage_usage()
            total_mb = total_bytes / (1024 * 1024)
            limit_mb = 1024.0
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

tab_upload, tab_files, tab_review, tab_bank = st.tabs(["🧠 考古題上傳", "📂 檔案管理及AI辨識", "📝 AI匯入校對", "📚 題庫管理"])

# Tab 1: Upload (Simplified)
with tab_upload:
    st.markdown("### 📤 上傳")
    uploaded_files = st.file_uploader("PDF", type=['pdf'], accept_multiple_files=True)
    if uploaded_files:
        st.divider()
        if 'upload_configs' not in st.session_state: st.session_state['upload_configs'] = {}
        with st.expander("批次設定"):
            c1, c2, c3, c4 = st.columns(4)
            with c1: b_type = st.selectbox("類型", ["學測", "分科", "北模", "中模", "全模", "其他"], key="bt")
            with c2: b_year = st.text_input("年度", value="112", key="by")
            with c3: b_exam_no = st.selectbox("次別", ["第一次", "第二次", "第三次", "正式考試"], key="bn")
            with c4: 
                if st.button("全部套用"):
                    for uf in uploaded_files: st.session_state['upload_configs'][uf.name] = {"type": b_type, "year": b_year, "exam_no": b_exam_no}
                    st.success("已套用")

        files_to_upload = []
        for i, f in enumerate(uploaded_files):
            conf = st.session_state['upload_configs'].get(f.name, {"type": "學測", "year": "112", "exam_no": "正式考試"})
            with st.container():
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                with c1: 
                    st.markdown(f"**{f.name}**")
                    new_name = f"{conf['year']}-{conf['type']}-{conf['exam_no']}.{f.name.split('.')[-1]}"
                    st.caption(f"➝ `{new_name}`")
                with c2: n_type = st.selectbox("類型", ["學測", "分科", "北模", "中模", "全模", "其他"], index=0, key=f"t_{f.name}")
                with c3: n_year = st.text_input("年度", value=conf['year'], key=f"y_{f.name}")
                with c4: n_no = st.selectbox("次別", ["第一次", "第二次", "第三次", "正式考試"], index=3, key=f"n_{f.name}")
                st.session_state['upload_configs'][f.name] = {"type": n_type, "year": n_year, "exam_no": n_no}
                files_to_upload.append({"file_obj": f, "new_filename": new_name, "type": n_type, "year": n_year, "exam_no": n_no})
            st.divider()

        if st.button("確認上傳", type="primary"):
            dup = []
            for item in files_to_upload:
                if cloud_manager.check_file_exists(item['new_filename']): dup.append(item['new_filename'])
            if dup: st.error(f"檔名重複: {', '.join(dup)}")
            else:
                prog = st.progress(0)
                for idx, item in enumerate(files_to_upload):
                    f = item['file_obj']; f.seek(0); fb = f.read()
                    url, blob = cloud_manager.upload_bytes(fb, item['new_filename'], folder="raw_uploads", content_type=f.type)
                    rec = {"filename": item['new_filename'], "url": url, "blob_name": blob, "exam_type": item['type'], "year": item['year'], "exam_no": item['exam_no'], "ai_status": "未辨識", "created_at": datetime.datetime.now()}
                    cloud_manager.save_file_record(rec)
                    st.session_state['file_queue'][item['new_filename']] = {"status": "uploaded", "data": fb, "type": "pdf", "backup_url": url, "blob_name": blob}
                    prog.progress((idx+1)/len(files_to_upload))
                st.success("上傳成功！")
                st.session_state['upload_configs'] = {}
                time.sleep(1); st.rerun()

# Tab 2: File Manage
with tab_files:
    if 'just_processed_file' in st.session_state:
        st.success(f"🎉 **{st.session_state['just_processed_file']}** 辨識完成！請至校對分頁。")
        del st.session_state['just_processed_file']
    
    files = cloud_manager.load_file_records()
    if not files: st.info("無檔案")
    else:
        tree = {}
        for f in files:
            t = f.get('exam_type', '未分類'); y = f.get('year', '未知');
            if t not in tree: tree[t] = {}
            if y not in tree[t]: tree[t][y] = []
            tree[t][y].append(f)
        
        for t in sorted(tree.keys()):
            with st.expander(f"📁 {t}", expanded=False):
                for y in sorted(tree[t].keys(), key=lambda x: -int(x) if x.isdigit() else 0):
                    with st.expander(f"📁 {y} 年度", expanded=False):
                        sorted_fs = sorted(tree[t][y], key=lambda x: x.get('exam_no', ''))
                        for f in sorted_fs:
                            c1, c2, c3 = st.columns([5, 2, 3], vertical_alignment="center")
                            with c1: st.write(f"📄 {f['filename']}")
                            with c2:
                                s = f.get('ai_status', '未辨識')
                                st.button("✅ 已辨識" if s=='已辨識' else "⬜ 未辨識", disabled=True, key=f"st_{f['id']}")
                            with c3:
                                b1, b2 = st.columns(2)
                                with b1:
                                    if st.button("辨識" if s!='已辨識' else "重辨", key=f"r_{f['id']}"):
                                        process_file_in_batches(f['filename'], api_key_input, f['id'])
                                with b2:
                                    if st.button("🗑️", key=f"d_{f['id']}"):
                                        cloud_manager.delete_file_record(f['id']); st.rerun()
                            
                            batches = cloud_manager.load_temp_batches(f['id'])
                            if batches:
                                with st.expander("查看批次處理詳情 (可單獨重試)", expanded=False):
                                    for b_idx, b_data in sorted(batches.items()):
                                        b_status = b_data.get('status', 'unknown')
                                        b_icon = "✅" if b_status == "success" else "❌"
                                        col_b1, col_b2 = st.columns([3, 1])
                                        col_b1.write(f"Batch {b_idx+1}: {b_icon}")
                                        if col_b2.button("重試", key=f"retry_{f['id']}_{b_idx}"):
                                            process_file_in_batches(f['filename'], api_key_input, f['id'], target_batch_idx=b_idx)
                            st.divider()

# Tab 3: Review (Pagination & Cleanup)
with tab_review:
    st.subheader("匯入校對")
    processed_files = [f for f in cloud_manager.load_file_records() if f.get('ai_status') == '已辨識']
    if not processed_files:
        st.warning("無已辨識檔案")
    else:
        opts = {f['filename']: f['id'] for f in processed_files}
        idx = 0
        if 'last_file' in st.session_state and st.session_state['last_file'] in opts:
             idx = list(opts.keys()).index(st.session_state['last_file'])
        
        sel_name = st.selectbox("選擇檔案", list(opts.keys()), index=idx)
        st.session_state['last_file'] = sel_name
        sel_id = opts[sel_name]

        cands = []
        batches = cloud_manager.load_temp_batches(sel_id)
        for b in sorted(batches.keys()):
            if batches[b].get('data'): cands.extend(json.loads(batches[b]['data']))
        
        if not cands: st.info("無題目資料")
        else:
            PER_PAGE = 5
            if 'p' not in st.session_state: st.session_state['p'] = 0
            max_p = max(0, (len(cands)-1)//PER_PAGE)
            
            c_prev, _, c_next = st.columns([1, 2, 1])
            if c_prev.button("⬅️", key="prev", disabled=st.session_state['p']==0): st.session_state['p'] -= 1; st.rerun()
            if c_next.button("➡️", key="next", disabled=st.session_state['p']>=max_p): st.session_state['p'] += 1; st.rerun()

            # Load PDF for dynamic cropping
            if 'curr_pdf' not in st.session_state or st.session_state.get('curr_name') != sel_name:
                rec = cloud_manager.check_file_exists(sel_name)
                if rec and rec.get('blob_name'):
                    st.session_state['curr_pdf'] = cloud_manager.download_blob(rec['blob_name'])
                    st.session_state['curr_name'] = sel_name
            
            start = st.session_state['p'] * PER_PAGE
            end = min(start + PER_PAGE, len(cands))
            
            with st.form(f"rev_{sel_id}_{st.session_state['p']}"):
                for i, item in enumerate(cands[start:end]):
                    real_idx = start + i
                    st.markdown(f"**第 {item.get('number')} 題**")
                    if item.get('type') == "Group": st.info("📖 題組")
                    
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        st.text_area("題目", item.get('content'), key=f"c_{real_idx}")
                        curr_type_zh = TYPE_MAP_EN_TO_ZH.get(item.get('type', 'Single'), "單選")
                        st.selectbox("題型", TYPE_OPTIONS, index=TYPE_OPTIONS.index(curr_type_zh) if curr_type_zh in TYPE_OPTIONS else 0, key=f"t_{real_idx}")
                        if item.get('type') != 'Group':
                            st.text_area("選項", "\n".join(item.get('options', [])), key=f"o_{real_idx}")
                        st.text_input("答案", item.get('answer'), key=f"a_{real_idx}")
                    with c2:
                        st.write("圖片預覽")
                        if 'curr_pdf' in st.session_state and st.session_state['curr_pdf']:
                            page_idx = item.get('page_index', 0)
                            box = item.get('full_question_box_2d')
                            # 動態裁切 (Auto Position)
                            img_bytes = get_review_image(st.session_state['curr_pdf'], page_idx, box_2d=box)
                            
                            if img_bytes:
                                st.image(img_bytes, caption=f"Page {page_idx+1}", use_container_width=True)
                                # [互動裁切] 若需要調整，這裡可放 cropper
                                # 為了效能，這裡先只顯示靜態圖，可加入 checkbox 開啟
                            else: st.warning("無法載入頁面")
                        else: st.info("PDF載入中...")
                
                st.form_submit_button("暫存此頁修改")

            st.divider()
            # [功能 3] 匯入並清理
            if st.button("✅ 確認匯入題庫 (清除暫存)", type="primary"):
                prog = st.progress(0)
                for idx, item in enumerate(cands):
                    # Convert & Save
                    q = Question(
                        q_type=item.get('type'), content=item.get('content'),
                        options=item.get('options'), answer=item.get('answer'),
                        source=selected_file_name.split('.')[0]
                    )
                    cloud_manager.save_question(q.to_dict())
                    prog.progress((idx+1)/len(cands))
                
                # Cleanup
                cloud_manager.clear_temp_batches(sel_id)
                # 刪除原始檔案 (可選)
                # cloud_manager.delete_file_record(sel_id) 

                st.success("匯入完成！")
                st.rerun()

# === Tab 4: Bank ===
with tab_bank:
    st.subheader("題庫管理")
    if not st.session_state['question_pool']: st.info("無題目")
    else:
        for i, q in enumerate(st.session_state['question_pool']):
            with st.expander(f"{q.content[:20]}..."):
                st.write(q.content)
                if st.button("刪除", key=f"del_q_{i}"):
                    cloud_manager.delete_question(q.id)
                    st.rerun()
