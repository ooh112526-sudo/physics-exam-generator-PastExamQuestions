import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import random
import io
import pandas as pd
import time
import base64
from PIL import Image
from streamlit_cropper import st_cropper 

import smart_importer
import firebase_db

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

# ==========================================
# 資料結構與狀態初始化
# ==========================================
class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=0, image_data=None, 
                 source="一般試題", chapter="未分類", unit="", db_id=None, 
                 parent_id=None, is_group_parent=False, sub_questions=None):
        self.id = db_id if db_id else str(int(time.time()*1000)) + str(random.randint(0, 999))
        self.type = q_type 
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data 
        
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
            "image_data_b64": img_str,
            "parent_id": self.parent_id,
            "is_group_parent": self.is_group_parent,
            "sub_questions": subs
        }

    @staticmethod
    def from_dict(data):
        img_bytes = None
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
    cloud_data = firebase_db.load_questions_from_cloud()
    if cloud_data:
        st.session_state['question_pool'] = [Question.from_dict(d) for d in cloud_data]

if 'file_queue' not in st.session_state:
    st.session_state['file_queue'] = {}

# ==========================================
# 工具函式
# ==========================================
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
        
        if q.image_data:
            try:
                img_p = doc.add_paragraph()
                run = img_p.add_run()
                run.add_picture(io.BytesIO(q.image_data), width=Inches(2.5))
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

def process_single_file(filename, api_key):
    """處理單一檔案的 AI 辨識"""
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
        st.success(f"{filename} 辨識完成！")
        
    st.rerun()

# ==========================================
# 介面
# ==========================================
st.title("🧲 物理題庫系統 Pro")

with st.sidebar:
    st.header("設定")
    api_key = st.text_input("Gemini API Key", type="password")
    st.divider()
    st.metric("題庫總數", len(st.session_state['question_pool']))
    
    if st.button("強制儲存至雲端"):
        db = firebase_db.get_db()
        if db:
            for q in st.session_state['question_pool']:
                firebase_db.save_question_to_cloud(q.to_dict())
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
                st.session_state['file_queue'][f.name] = {
                    "status": "uploaded", 
                    "data": f.read(),
                    "type": f.name.split('.')[-1].lower(),
                    "result": [],
                    "error_msg": "",
                    "source_tag": "未分類" # 預設標籤
                }
                new_count += 1
        if new_count > 0:
            st.toast(f"已加入 {new_count} 個新檔案", icon="📄")

    st.divider()
    
    # 2. 檔案列表 (分層顯示)
    
    # 分類檔案狀態
    queue = st.session_state['file_queue']
    imported_files = {} # {source_tag: [filename, ...]}
    ready_files = []    # [filename]
    pending_files = []  # [filename] (uploaded or error)
    
    # 保持順序 (Python 3.7+ dict is ordered)
    for fname, info in queue.items():
        if info['status'] == 'imported':
            tag = info.get('source_tag', '未分類')
            if tag not in imported_files: imported_files[tag] = []
            imported_files[tag].append(fname)
        elif info['status'] == 'done':
            ready_files.append(fname)
        else: # uploaded, processing, error
            pending_files.append(fname)

    # 2.1 已匯入區 (分層)
    st.subheader("📚 已匯入檔案庫")
    if not imported_files:
        st.caption("尚無已匯入的檔案")
    else:
        for tag, fnames in imported_files.items():
            with st.expander(f"📁 {tag} ({len(fnames)} 份試卷)"):
                for fname in fnames:
                    col_f1, col_f2 = st.columns([4, 1])
                    col_f1.text(f"📄 {fname}")
                    if col_f2.button("移除", key=f"del_imp_{fname}"):
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

    # 2.3 待辨識區 (最下方，優先處理)
    st.subheader("⏳ 待辨識檔案 (需執行 AI)")
    if not pending_files:
        st.info("目前沒有等待辨識的檔案。")
    else:
        # 提供一鍵全部執行
        if st.button("🚀 全部執行辨識"):
            if not api_key:
                st.error("請輸入 API Key")
            else:
                progress_bar = st.progress(0)
                for idx, fname in enumerate(pending_files):
                    process_single_file(fname, api_key) # 這裡會 rerun，所以其實只會跑第一個
                    # 若要連續跑，需修改 process_single_file 不 rerun，改為迴圈控制
                    # 但為了簡單與穩定，建議使用者一個個點，或此處簡單處理
                st.rerun()

        for fname in pending_files:
            info = queue[fname]
            with st.container():
                c1, c2, c3 = st.columns([3, 2, 1])
                
                status_display = "等待中"
                if info['status'] == 'processing': status_display = "🔄 分析中..."
                elif info['status'] == 'error': status_display = f"❌ 失敗: {info['error_msg']}"
                
                c1.markdown(f"**📄 {fname}**")
                c2.caption(status_display)
                
                if c3.button("▶️ 執行", key=f"run_{fname}", disabled=(info['status']=='processing')):
                    if not api_key:
                        st.error("請輸入 API Key")
                    else:
                        process_single_file(fname, api_key)
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
        
        # 來源標籤設定
        col_src1, col_src2 = st.columns(2)
        with col_src1:
            # 預設使用檔名作為標籤
            default_tag = selected_file.split('.')[0]
            source_tag = st.text_input("設定此批試卷來源標籤", value=default_tag, help="此標籤將用於分類管理檔案")
        
        st.divider()
        
        # 題目編輯迴圈
        for i, cand in enumerate(candidates):
            with st.container():
                st.markdown(f"**第 {cand.number} 題**")
                c1, c2 = st.columns([1, 1])
                
                with c1:
                    new_content = st.text_area(f"題目內容 #{i}", cand.content, height=100, key=f"{selected_file}_c_{i}")
                    cand.content = new_content
                    
                    opts_text = "\n".join(cand.options)
                    new_opts = st.text_area(f"選項 #{i}", opts_text, height=80, key=f"{selected_file}_o_{i}")
                    cand.options = new_opts.split('\n') if new_opts else []
                    
                    type_idx = ["Single", "Multi", "Fill"].index(cand.q_type) if cand.q_type in ["Single", "Multi", "Fill"] else 0
                    cand.q_type = st.selectbox(f"題型 #{i}", ["Single", "Multi", "Fill"], index=type_idx, key=f"{selected_file}_t_{i}")

                    ans_key = f"{selected_file}_ans_{i}"
                    if ans_key not in st.session_state: st.session_state[ans_key] = ""
                    st.text_input(f"答案 (可留空) #{i}", key=ans_key)
                    
                    chap_idx = 0
                    if cand.predicted_chapter in smart_importer.PHYSICS_CHAPTERS_LIST:
                        chap_idx = smart_importer.PHYSICS_CHAPTERS_LIST.index(cand.predicted_chapter)
                    cand.predicted_chapter = st.selectbox(f"章節分類 #{i}", smart_importer.PHYSICS_CHAPTERS_LIST, index=chap_idx, key=f"{selected_file}_ch_{i}")
                    
                    if cand.image_bytes:
                        st.image(cand.image_bytes, caption="目前附圖", width=200)
                    else:
                        st.caption("🚫 目前無附圖")

                with c2:
                    if cand.ref_image_bytes:
                        st.markdown("✂️ **截圖工具**")
                        try:
                            pil_ref = Image.open(io.BytesIO(cand.ref_image_bytes))
                            cropped_img = st_cropper(
                                pil_ref, 
                                realtime_update=True, 
                                box_color='#FF0000',
                                key=f"{selected_file}_cropper_{i}",
                                aspect_ratio=None
                            )
                            col_act1, col_act2 = st.columns(2)
                            if col_act1.button(f"📷 設為附圖 #{i}", key=f"{selected_file}_btn_crop_{i}"):
                                img_byte_arr = io.BytesIO()
                                cropped_img.save(img_byte_arr, format='PNG')
                                cand.image_bytes = img_byte_arr.getvalue()
                                st.success("附圖已更新")
                                st.rerun()
                            if col_act2.button(f"🚫 不使用圖片 #{i}", key=f"{selected_file}_btn_noimg_{i}"):
                                cand.image_bytes = None
                                st.success("附圖已移除")
                                st.rerun()
                        except: st.error("截圖載入失敗")
                    else:
                        st.info("此題無參考截圖")
                st.divider()

        if st.button(f"✅ 確認匯入 [{selected_file}] 的所有題目", type="primary"):
            count = 0
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
                st.session_state['question_pool'].append(new_q)
                firebase_db.save_question_to_cloud(new_q.to_dict())
                count += 1
            
            st.success(f"成功匯入 {count} 題！")
            
            # 更新檔案狀態與標籤
            st.session_state['file_queue'][selected_file]['status'] = 'imported'
            st.session_state['file_queue'][selected_file]['source_tag'] = source_tag # 儲存標籤以便分類
            st.rerun()

# === Tab 3: 題庫管理 (分層顯示) ===
with tab3:
    st.subheader("題庫總覽與試卷輸出")
    if not st.session_state['question_pool']:
        st.info("目前沒有題目。")
    else:
        # 1. 取得所有來源
        all_sources = sorted(list(set([q.source for q in st.session_state['question_pool']])))
        
        # 2. 顯示分層列表
        selected_questions_for_export = []
        
        for src in all_sources:
            # 篩選該來源的題目
            qs_in_src = [q for q in st.session_state['question_pool'] if q.source == src]
            
            with st.expander(f"📁 {src} ({len(qs_in_src)} 題)"):
                # 讓使用者選擇是否全選這個來源
                if st.checkbox(f"選取全套 [{src}] 進行匯出", key=f"sel_src_{src}"):
                    selected_questions_for_export.extend(qs_in_src)

                for i, q in enumerate(qs_in_src):
                    type_badge = {'Single': '單', 'Multi': '多', 'Fill': '填', 'Group': '題組'}.get(q.type, '未知')
                    # 子題不顯示，因為會跟著母題
                    if q.parent_id: continue 
                    
                    st.markdown(f"**[{type_badge}] {q.content[:30]}...**")
                    
                    # 簡易編輯按鈕 (若要詳細編輯可點開)
                    with st.popover("編輯"):
                        q.content = st.text_area("題目", q.content, key=f"edt_c_{q.id}")
                        q.answer = st.text_input("答案", q.answer, key=f"edt_a_{q.id}")
                        if st.button("儲存", key=f"save_{q.id}"):
                            firebase_db.save_question_to_cloud(q.to_dict())
                            st.rerun()
                        if st.button("刪除", key=f"del_{q.id}", type="primary"):
                            firebase_db.delete_question_from_cloud(q.id)
                            st.rerun()
                    st.divider()

        st.divider()
        st.subheader(f"已選取 {len(selected_questions_for_export)} 題準備匯出")
        if st.button("生成 Word 試卷"):
            f1, f2 = generate_word_files(selected_questions_for_export)
            st.download_button("下載試題卷", f1, "exam.docx")
            st.download_button("下載答案卷", f2, "ans.docx")
