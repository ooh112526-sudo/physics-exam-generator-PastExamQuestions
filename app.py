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
from google.cloud import firestore
from google.cloud import storage
import google.auth # 新增: 用於自動偵測專案 ID

import smart_importer

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

# ==========================================
# 雲端資料庫與儲存模組 (內建)
# ==========================================
class CloudManager:
    def __init__(self):
        self.bucket_name = os.getenv("GCS_BUCKET_NAME", "physics-exam-assets")
        
        # 1. 嘗試從環境變數讀取 Project ID
        self.project_id = (
            os.getenv("GCP_PROJECT_ID") or 
            os.getenv("GOOGLE_CLOUD_PROJECT") or 
            st.secrets.get("GCP_PROJECT_ID")
        )
        
        # 2. 如果環境變數沒設，嘗試使用 Google Auth 自動偵測 (適用於 Cloud Run)
        if not self.project_id:
            try:
                #這會嘗試從環境憑證中獲取專案ID
                _, project_id_from_auth = google.auth.default()
                if project_id_from_auth:
                    self.project_id = project_id_from_auth
                    print(f"已透過 Google Auth 自動偵測到 Project ID: {self.project_id}")
            except Exception as e:
                print(f"Google Auth 自動偵測失敗: {e}")

        self.db = None
        self.storage_client = None
        self.has_connection = False
        self.connection_error = ""
        
        try:
            # 初始化 Client，明確傳入 project 參數
            if self.project_id:
                self.db = firestore.Client(project=self.project_id)
                self.storage_client = storage.Client(project=self.project_id)
            else:
                # 最後嘗試：不帶參數初始化 (依賴 SDK 內部預設行為)
                self.db = firestore.Client()
                self.storage_client = storage.Client()
                
            self.has_connection = True
        except Exception as e:
            self.connection_error = str(e)
            print(f"Cloud 連線初始化失敗: {e}")

    def upload_bytes(self, file_bytes, filename, folder="uploads", content_type=None):
        if not self.storage_client: return None
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            # 確保檔名安全並唯一
            unique_name = f"{folder}/{int(datetime.datetime.now().timestamp())}_{str(uuid.uuid4())[:8]}_{filename}"
            blob = bucket.blob(unique_name)
            blob.upload_from_string(file_bytes, content_type=content_type)
            # 若 Bucket 為公開或 Uniform Access，此 URL 可直接存取
            return blob.public_url 
        except Exception as e:
            print(f"上傳 Storage 失敗: {e}")
            st.toast(f"上傳圖片失敗: {e}", icon="⚠️")
            return None

    def save_question(self, question_dict):
        if not self.db: return False
        try:
            # 處理 Base64 圖片轉 URL
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
            
            # 寫入 Firestore
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

# 初始化雲端管理員
cloud_manager = CloudManager()

# ==========================================
# 資料結構與狀態初始化
# ==========================================
class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=0, image_data=None, 
                 source="一般試題", chapter="未分類", unit="", db_id=None, 
                 parent_id=None, is_group_parent=False, sub_questions=None, image_url=None):
        self.id = db_id if db_id else str(int(time.time()*1000)) + str(random.randint(0, 999))
        self.type = q_type 
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data # 二進位 (編輯時優先使用)
        self.image_url = image_url   # 雲端連結 (顯示時使用)
        
        self.parent_id = parent_id 
        self.is_group_parent = is_group_parent 
        self.sub_questions = sub_questions if sub_questions else [] 

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
            "image_data_b64": img_str, # 暫存用，cloud_manager 會轉存成 URL
            "image_url": self.image_url,
            "parent_id": self.parent_id,
            "is_group_parent": self.is_group_parent,
            "sub_questions": subs
        }

    @staticmethod
    def from_dict(data):
        img_bytes = None
        img_url = data.get("image_url")

        # 若有 Base64 (舊資料或同步失敗殘留)，優先轉回 bytes
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
            is_group_parent=data.get("is_group_parent", False)
        )
        
        if data.get("sub_questions"):
            q.sub_questions = [Question.from_dict(sub) for sub in data["sub_questions"]]
            
        return q

# === Session State 初始化 ===
if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
    cloud_data = cloud_manager.load_questions()
    if cloud_data:
        st.session_state['question_pool'] = [Question.from_dict(d) for d in cloud_data]

if 'file_queue' not in st.session_state:
    st.session_state['file_queue'] = {}

# ==========================================
# 工具函式
# ==========================================
def get_image_bytes(q):
    """取得圖片 Bytes (優先使用記憶體中的，若無則從 URL 下載)"""
    if q.image_data:
        return q.image_data
    if q.image_url:
        try:
            response = requests.get(q.image_url)
            if response.status_code == 200:
                return response.content
        except:
            return None
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
        
        # 處理圖片顯示
        img_bytes = get_image_bytes(q)
        if img_bytes:
            try:
                img_p = doc.add_paragraph()
                run = img_p.add_run()
                run.add_picture(io.BytesIO(img_bytes), width=Inches(2.5))
            except Exception as e:
                print(f"Word 圖片寫入錯誤: {e}")

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

def process_single_file(filename, api_key):
    """處理單一檔案的 AI 辨識"""
    if filename not in st.session_state['file_queue']: return
    
    info = st.session_state['file_queue'][filename]
    info['status'] = 'processing'
    
    with st.spinner(f"正在分析 {filename}..."):
        # 呼叫 smart_importer 進行解析
        res = smart_importer.parse_with_gemini(info['data'], info['type'], api_key)
    
    if isinstance(res, dict) and "error" in res:
        info['status'] = 'error'
        info['error_msg'] = res['error']
        st.error(f"{filename} 辨識失敗: {res['error']}")
    else:
        info['status'] = 'done'
        info['result'] = res
        st.success(f"{filename} 辨識完成！")
        
    st.rerun()

# ==========================================
# 介面
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

with st.sidebar:
    st.header("設定")
    # 嘗試從環境變數讀取 API Key (部署時)，若無則顯示輸入框
    env_api_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_api_key, type="password")
    
    # 檢查 Cloud 連線狀態
    if cloud_manager.has_connection:
        st.success("☁️ Cloud: 已連線")
    else:
        st.warning(f"☁️ Cloud: 未連線")
        if cloud_manager.connection_error:
            st.caption(f"錯誤: {cloud_manager.connection_error}")
            
            # 提示使用者可能缺少的環境變數
            if "Project was not passed" in cloud_manager.connection_error:
                st.error("⚠️ 請至 Cloud Run 設定變數: GCP_PROJECT_ID")
            else:
                st.info("請確認 Cloud Run 權限或本機憑證")

    st.divider()
    st.metric("題庫總數", len(st.session_state['question_pool']))
    
    # 強制備份按鈕
    if st.button("強制儲存至雲端"):
        if cloud_manager.has_connection:
            progress_bar = st.progress(0)
            total = len(st.session_state['question_pool'])
            for i, q in enumerate(st.session_state['question_pool']):
                cloud_manager.save_question(q.to_dict())
                progress_bar.progress((i + 1) / total)
            st.success("儲存完成！")

tab1, tab2, tab3 = st.tabs(["🧠 檔案管理與辨識", "📝 匯入校對", "📚 題庫管理"])

# === Tab 1: 檔案管理與辨識 ===
with tab1:
    # 1. 上傳區
    st.markdown("### 📤 上傳檔案 (批次)")
    uploaded_files = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'], accept_multiple_files=True)
    
    if uploaded_files:
        new_count = 0
        for f in uploaded_files:
            if f.name not in st.session_state['file_queue']:
                file_bytes = f.read()
                
                # [核心功能] 自動備份原始檔案到 Cloud Storage
                backup_url = cloud_manager.upload_bytes(
                    file_bytes, 
                    f.name, 
                    folder="raw_uploads", 
                    content_type=f.type
                )
                
                status_msg = "uploaded"
                if backup_url:
                    status_msg += " (已備份)"
                
                st.session_state['file_queue'][f.name] = {
                    "status": "uploaded", 
                    "data": file_bytes,
                    "type": f.name.split('.')[-1].lower(),
                    "result": [],
                    "error_msg": "",
                    "source_tag": "未分類",
                    "backup_url": backup_url # 記錄備份連結
                }
                new_count += 1
        if new_count > 0:
            st.toast(f"已加入 {new_count} 個新檔案", icon="☁️")

    st.divider()
    
    # 2. 檔案列表 (分層顯示)
    queue = st.session_state['file_queue']
    imported_files = {} 
    ready_files = []    
    pending_files = []  
    
    for fname, info in queue.items():
        if info['status'] == 'imported':
            tag = info.get('source_tag', '未分類')
            if tag not in imported_files: imported_files[tag] = []
            imported_files[tag].append(fname)
        elif info['status'] == 'done':
            ready_files.append(fname)
        else: 
            pending_files.append(fname)

    # 2.1 已匯入區
    st.subheader("📚 已匯入檔案庫")
    if not imported_files:
        st.caption("尚無已匯入的檔案")
    else:
        for tag, fnames in imported_files.items():
            with st.expander(f"📁 {tag} ({len(fnames)} 份試卷)"):
                for fname in fnames:
                    col_f1, col_f2, col_f3 = st.columns([3, 1, 1])
                    col_f1.text(f"📄 {fname}")
                    
                    # 顯示下載備份連結
                    info = queue.get(fname)
                    if info and info.get('backup_url'):
                        col_f2.link_button("下載原始檔", info['backup_url'])
                    else:
                        col_f2.caption("無備份")

                    if col_f3.button("移除", key=f"del_imp_{fname}"):
                        del st.session_state['file_queue'][fname]
                        st.rerun()

    st.divider()

    # 2.2 待編輯區
    st.subheader("✏️ 待匯入/編輯 (辨識完成)")
    if not ready_files:
        st.caption("尚無等待編輯的檔案")
    else:
        for fname in ready_files:
            info = queue[fname]
            with st.container():
                c1, c2, c3 = st.columns([3, 2, 1])
                c1.markdown(f"**✅ {fname}** ({len(info['result'])} 題)")
                c2.info("請至「匯入校對」分頁進行編輯")
                if c3.button("🗑️", key=f"del_rdy_{fname}"):
                    del st.session_state['file_queue'][fname]
                    st.rerun()
            st.divider()

    st.divider()

    # 2.3 待辨識區
    st.subheader("⏳ 待辨識檔案 (需執行 AI)")
    if not pending_files:
        st.info("目前沒有等待辨識的檔案。")
    else:
        if st.button("🚀 全部執行辨識"):
            if not api_key_input:
                st.error("請輸入 API Key")
            else:
                progress_bar = st.progress(0)
                for idx, fname in enumerate(pending_files):
                    process_single_file(fname, api_key_input)
                st.rerun()

        for fname in pending_files:
            info = queue[fname]
            with st.container():
                c1, c2, c3 = st.columns([3, 2, 1])
                
                status_display = "等待中"
                if info.get('backup_url'): status_display += " | ☁️ 已備份"
                
                if info['status'] == 'processing': status_display = "🔄 分析中..."
                elif info['status'] == 'error': status_display = f"❌ 失敗: {info['error_msg']}"
                
                c1.markdown(f"**📄 {fname}**")
                c2.caption(status_display)
                
                if c3.button("▶️ 執行", key=f"run_{fname}", disabled=(info['status']=='processing')):
                    if not api_key_input:
                        st.error("請輸入 API Key")
                    else:
                        process_single_file(fname, api_key_input)
            st.divider()

# === Tab 2: 匯入校對 ===
with tab2:
    st.subheader("匯入校對與截圖")
    
    ready_files = [f for f, info in st.session_state['file_queue'].items() if info['status'] == 'done']
    
    if not ready_files:
        st.warning("沒有已完成辨識的檔案。請先至 Tab 1 上傳並執行。")
    else:
        selected_file = st.selectbox("選擇要處理的檔案", ready_files)
        file_info = st.session_state['file_queue'][selected_file]
        candidates = file_info['result']
        
        st.markdown(f"**正在編輯：{selected_file} (共 {len(candidates)} 題)**")
        
        col_src1, col_src2 = st.columns(2)
        with col_src1:
            default_tag = selected_file.split('.')[0]
            source_tag = st.text_input("設定此批試卷來源標籤", value=default_tag)
        
        st.divider()
        
        # 使用 Form 解決 Lag 問題
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
                    
                    if cand.image_bytes:
                        st.image(cand.image_bytes, caption="目前附圖", width=200)
                    else:
                        st.caption("🚫 目前無附圖")

                with c2:
                    st.markdown("✂️ **截圖工具**")
                    # 優先使用參考截圖，若無則使用整頁圖片 (Fallback)
                    image_to_crop = cand.ref_image_bytes if cand.ref_image_bytes else cand.full_page_bytes
                    
                    if image_to_crop:
                        try:
                            pil_ref = Image.open(io.BytesIO(image_to_crop))
                            # 每個 Cropper 需要唯一的 Key
                            st_cropper(
                                pil_ref, 
                                realtime_update=True, 
                                box_color='#FF0000',
                                key=f"{selected_file}_cropper_{i}",
                                aspect_ratio=None
                            )
                            st.caption("提示：截圖需在 Form 提交後或獨立操作")
                        except: st.error("截圖載入失敗")
                    else:
                        st.info("無法取得此題的參考圖片 (也無整頁圖片)")
                st.divider()
            
            # Form 提交按鈕
            submit_changes = st.form_submit_button("💾 暫存所有修改 (不會上傳)")
        
        # 確認匯入按鈕
        if st.button(f"✅ 確認匯入 [{selected_file}] 至雲端", type="primary"):
            progress_bar = st.progress(0)
            count = 0
            total = len(candidates)
            for i, cand in enumerate(candidates):
                ans_val = st.session_state.get(f"{selected_file}_ans_{i}", "")
                
                new_q = Question(
                    q_type=cand.q_type,
                    content=cand.content,
                    options=cand.options,
                    source=source_tag, 
                    chapter=cand.predicted_chapter,
                    image_data=cand.image_bytes,
                    answer=ans_val 
                )
                
                # 自動儲存至雲端 (含圖片轉存)
                cloud_manager.save_question(new_q.to_dict())
                
                st.session_state['question_pool'].append(new_q)
                count += 1
                progress_bar.progress((i + 1) / total)
            
            st.success(f"成功匯入 {count} 題！")
            
            st.session_state['file_queue'][selected_file]['status'] = 'imported'
            st.session_state['file_queue'][selected_file]['source_tag'] = source_tag 
            st.rerun()

# === Tab 3: 題庫管理 ===
with tab3:
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
                    
                    if q.image_url:
                        st.caption("🖼️ 雲端圖片")
                    elif q.image_data:
                        st.caption("💾 本機圖片 (未同步)")
                        
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
