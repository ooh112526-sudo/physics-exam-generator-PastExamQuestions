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
                 source="一般試題", chapter="未分類", unit="", db_id=None):
        self.id = db_id if db_id else str(int(time.time()*1000)) 
        self.type = q_type 
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data 

    def to_dict(self):
        img_str = None
        if self.image_data:
            img_str = base64.b64encode(self.image_data).decode('utf-8')
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "chapter": self.chapter,
            "content": self.content,
            "options": self.options,
            "answer": self.answer,
            "image_data_b64": img_str
        }

    @staticmethod
    def from_dict(data):
        img_bytes = None
        if data.get("image_data_b64"):
            try:
                img_bytes = base64.b64decode(data["image_data_b64"])
            except: pass
        return Question(
            q_type=data.get("type", "Single"),
            content=data.get("content", ""),
            options=data.get("options", []),
            answer=data.get("answer", ""),
            original_id=0,
            image_data=img_bytes,
            source=data.get("source", ""),
            chapter=data.get("chapter", "未分類"),
            db_id=data.get("id")
        )

if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
    cloud_data = firebase_db.load_questions_from_cloud()
    if cloud_data:
        st.session_state['question_pool'] = [Question.from_dict(d) for d in cloud_data]
    else:
        if not firebase_db.get_db():
            st.warning("⚠️ 未偵測到 Firebase 設定。")

if 'imported_candidates' not in st.session_state:
    st.session_state['imported_candidates'] = []

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
    
    for idx, q in enumerate(selected_questions, 1):
        p = exam_doc.add_paragraph()
        type_label = {'Single': '【單選】', 'Multi': '【多選】', 'Fill': '【填充】'}.get(q.type, '')
        
        # 顯示來源標籤在題目中 (選用)
        src_label = f"[{q.source}] " if q.source else ""
        
        runner = p.add_run(f"{idx}. {src_label}{type_label} {q.content.strip()}")
        runner.bold = True
        
        if q.image_data:
            try:
                exam_doc.add_picture(io.BytesIO(q.image_data), width=Inches(2.5))
            except: pass

        if q.type in ['Single', 'Multi']:
            for i, opt in enumerate(q.options):
                exam_doc.add_paragraph(f"{opt}")
        elif q.type == 'Fill':
            exam_doc.add_paragraph("答：______________________")
        exam_doc.add_paragraph("") 
        
        ans_p = ans_doc.add_paragraph()
        ans_p.add_run(f"{idx}. {q.answer}")
        
    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io

# ==========================================
# 介面
# ==========================================
st.title("🧲 物理題庫系統 Pro")

with st.sidebar:
    st.header("設定")
    api_key = st.text_input("Gemini API Key", type="password")
    st.divider()
    st.metric("題庫數量", len(st.session_state['question_pool']))
    if st.button("強制儲存至雲端"):
        db = firebase_db.get_db()
        if db:
            for q in st.session_state['question_pool']:
                firebase_db.save_question_to_cloud(q.to_dict())
            st.success("儲存完成！")

tab1, tab2, tab3 = st.tabs(["🧠 智慧匯入", "📝 題庫管理 & 編輯", "🚀 組卷匯出"])

# === Tab 1: 智慧匯入 ===
with tab1:
    st.markdown("### 1. 設定試卷來源標籤")
    
    col_src1, col_src2, col_src3 = st.columns(3)
    with col_src1:
        exam_type = st.selectbox("考試類型", ["學測", "分科", "北模", "中模", "全模", "自行輸入"])
    with col_src2:
        exam_year = st.text_input("年度 (例如 112)", value="113")
    with col_src3:
        # 如果是模擬考，才顯示場次選擇
        exam_session_opts = [""] 
        if "模" in exam_type:
            exam_session_opts = ["第1次", "第2次", "第3次", "第4次"]
        elif exam_type == "自行輸入":
            exam_session_opts = [""]
        
        exam_session = st.selectbox("場次 (僅模考)", exam_session_opts) if "模" in exam_type else ""

    # 組合來源字串
    final_source_tag = f"{exam_year}-{exam_type}"
    if exam_session:
        final_source_tag += f"-{exam_session}"
    
    if exam_type == "自行輸入":
        final_source_tag = st.text_input("自訂來源名稱", value=f"{exam_year}-自訂試卷")

    st.markdown(f"**預覽標籤：** `{final_source_tag}`")
    st.divider()

    st.markdown("### 2. 上傳試卷 (PDF / Word)")
    raw_file = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'])
    
    if raw_file and st.button("開始 AI 分析"):
        if not api_key:
            st.error("請輸入 API Key")
        else:
            file_type = raw_file.name.split('.')[-1].lower()
            with st.spinner("🤖 Gemini 正在分析中..."):
                res = smart_importer.parse_with_gemini(raw_file.read(), file_type, api_key)
                if isinstance(res, dict) and "error" in res:
                    st.error(res["error"])
                else:
                    st.session_state['imported_candidates'] = res
                    st.success(f"成功辨識 {len(res)} 題！")

    if st.session_state['imported_candidates']:
        st.divider()
        st.subheader("3. 匯入校對與截圖")
        st.info("💡 請檢查「章節分類」，若 AI 判斷錯誤可在此修正為「未分類」或其他章節。")
        
        for i, cand in enumerate(st.session_state['imported_candidates']):
            with st.container():
                st.markdown(f"**第 {cand.number} 題**")
                c1, c2 = st.columns([1, 1])
                
                with c1:
                    new_content = st.text_area(f"題目內容 #{i}", cand.content, height=100)
                    cand.content = new_content
                    
                    opts_text = "\n".join(cand.options)
                    new_opts = st.text_area(f"選項 #{i}", opts_text, height=80)
                    cand.options = new_opts.split('\n') if new_opts else []
                    
                    # 章節選擇器 (包含 '未分類')
                    current_chap_idx = 0
                    if cand.predicted_chapter in smart_importer.PHYSICS_CHAPTERS_LIST:
                        current_chap_idx = smart_importer.PHYSICS_CHAPTERS_LIST.index(cand.predicted_chapter)
                    
                    new_chap = st.selectbox(
                        f"章節分類 #{i}", 
                        smart_importer.PHYSICS_CHAPTERS_LIST, 
                        index=current_chap_idx
                    )
                    cand.predicted_chapter = new_chap
                    
                    if cand.image_bytes:
                        st.image(cand.image_bytes, caption="目前附圖", width=200)
                        if st.button(f"清除附圖 #{i}"):
                            cand.image_bytes = None
                            st.rerun()

                with c2:
                    if cand.ref_image_bytes:
                        st.markdown("✂️ **截圖工具**")
                        try:
                            pil_ref = Image.open(io.BytesIO(cand.ref_image_bytes))
                            cropped_img = st_cropper(
                                pil_ref, 
                                realtime_update=True, 
                                box_color='#FF0000',
                                key=f"cropper_{i}",
                                aspect_ratio=None
                            )
                            if st.button(f"📷 使用此範圍為附圖 #{i}"):
                                img_byte_arr = io.BytesIO()
                                cropped_img.save(img_byte_arr, format='PNG')
                                cand.image_bytes = img_byte_arr.getvalue()
                                st.success("附圖已更新！")
                                st.rerun()
                        except Exception as e:
                            st.error(f"無法載入截圖工具: {e}")
                    else:
                        st.info("此題無原始截圖")
                st.divider()

        col_submit, _ = st.columns([1, 3])
        if col_submit.button("✅ 確認將所有題目匯入題庫", type="primary"):
            count = 0
            for cand in st.session_state['imported_candidates']:
                new_q = Question(
                    q_type=cand.q_type,
                    content=cand.content,
                    options=cand.options,
                    source=final_source_tag, # 使用剛設定好的標籤
                    chapter=cand.predicted_chapter,
                    image_data=cand.image_bytes 
                )
                st.session_state['question_pool'].append(new_q)
                firebase_db.save_question_to_cloud(new_q.to_dict())
                count += 1
            
            st.success(f"匯入 {count} 題！")
            st.session_state['imported_candidates'] = []
            st.rerun()

# === Tab 2: 題庫管理 ===
with tab2:
    st.subheader("題庫列表")
    if not st.session_state['question_pool']:
        st.info("目前沒有題目。")
    else:
        # 提供簡單的篩選器
        filter_src = st.multiselect("篩選來源", list(set([q.source for q in st.session_state['question_pool']])))
        
        filtered_pool = st.session_state['question_pool']
        if filter_src:
            filtered_pool = [q for q in st.session_state['question_pool'] if q.source in filter_src]

        for i, q in enumerate(filtered_pool):
            type_badge = {'Single': '單', 'Multi': '多', 'Fill': '填'}.get(q.type, '未知')
            with st.expander(f"[{q.source}] [{type_badge}] {q.content[:30]}..."):
                c1, c2 = st.columns([2, 1])
                with c1:
                    q.content = st.text_area(f"題目 #{q.id}", q.content, height=100)
                    opts_str = st.text_area(f"選項 #{q.id}", "\n".join(q.options), height=100)
                    q.options = opts_str.split('\n') if opts_str else []
                with c2:
                    q.type = st.selectbox(f"題型 #{q.id}", ["Single", "Multi", "Fill"], index=["Single", "Multi", "Fill"].index(q.type) if q.type in ["Single", "Multi", "Fill"] else 0)
                    
                    # 這裡也能修改章節
                    chap_idx = 0
                    if q.chapter in smart_importer.PHYSICS_CHAPTERS_LIST:
                        chap_idx = smart_importer.PHYSICS_CHAPTERS_LIST.index(q.chapter)
                    q.chapter = st.selectbox(f"章節 #{q.id}", smart_importer.PHYSICS_CHAPTERS_LIST, index=chap_idx)
                    
                    q.answer = st.text_input(f"答案 #{q.id}", q.answer)
                    
                    if st.button(f"💾 儲存 #{q.id}"):
                        firebase_db.save_question_to_cloud(q.to_dict())
                        st.success("儲存成功")
                    
                    if st.button(f"🗑️ 刪除 #{q.id}", type="primary"):
                        firebase_db.delete_question_from_cloud(q.id)
                        # 需重新整理頁面以更新列表
                        st.rerun()

# === Tab 3: 組卷匯出 ===
with tab3:
    st.subheader("生成 Word 試卷")
    if st.button("生成並下載"):
        f1, f2 = generate_word_files(st.session_state['question_pool'])
        st.download_button("下載試題卷", f1, "exam.docx")
        st.download_button("下載答案卷", f2, "ans.docx")
