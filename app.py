import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import random
import io
import re
import pandas as pd
import os

# 引用您的 smart_importer.py
import smart_importer

st.set_page_config(page_title="物理題庫系統 (Gemini AI)", layout="wide", page_icon="🧲")

# ==========================================
# 常數與資料結構
# ==========================================
SOURCES = ["一般試題", "學測題", "分科測驗", "北模", "全模", "中模", "AI匯入"]
PHYSICS_CHAPTERS = smart_importer.PHYSICS_CHAPTERS_LIST 

class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=0, image_data=None, 
                 source="一般試題", chapter="", unit=""):
        self.id = original_id
        self.type = q_type
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data

def generate_word_files(selected_questions, shuffle=True):
    exam_doc = docx.Document()
    ans_doc = docx.Document()
    
    # 設定字型
    style = exam_doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '標楷體')
    
    exam_doc.add_heading('物理科 試題卷', 0)
    ans_doc.add_heading('物理科 答案卷', 0)
    
    for idx, q in enumerate(selected_questions, 1):
        processed_q = q
        # 簡單的打亂選項邏輯
        if shuffle and q.type in ['Single', 'Multi'] and not q.answer:
             # 若沒有答案對照，僅打亂選項顯示 (有答案時需複雜邏輯，此處簡化)
             # 若要完整打亂且保留答案正確性，需搭配 smart_importer 的完整結構
             pass

        p = exam_doc.add_paragraph()
        q_type_text = {'Single': '單選', 'Multi': '多選', 'Fill': '填充'}.get(q.type, '題')
        runner = p.add_run(f"{idx}. ({q_type_text}) {processed_q.content.strip()}")
        runner.bold = True
        
        # 處理圖片 (若有)
        if hasattr(q, 'image_data') and q.image_data:
            try:
                img_stream = io.BytesIO(q.image_data)
                exam_doc.add_picture(img_stream, width=Inches(3.0))
            except: pass

        if q.type != 'Fill':
            for i, opt in enumerate(processed_q.options):
                exam_doc.add_paragraph(f"{opt}") 
        else:
            exam_doc.add_paragraph("______________________")
        exam_doc.add_paragraph("") 
        
        ans_p = ans_doc.add_paragraph()
        ans_p.add_run(f"{idx}. {processed_q.answer if processed_q.answer else '無'}")
        
    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io

def parse_docx_tagged(file_bytes):
    # 這裡保留一個空函式或舊邏輯以避免錯誤
    return []

# ==========================================
# Session State
# ==========================================
if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
if 'imported_candidates' not in st.session_state:
    st.session_state['imported_candidates'] = []

# ==========================================
# Streamlit 主介面
# ==========================================

st.title("🧲 物理題庫自動組卷系統 v4.0 (Gemini AI)")
st.caption("Assistant: 使用 Google Gemini Vision 進行精準試卷辨識")

# --- 側邊欄 ---
with st.sidebar:
    st.header("🔑 AI 設定")
    api_key_input = st.text_input("Gemini API Key", type="password", help="請輸入 Google AI Studio 申請的 API Key")
    
    st.divider()
    st.header("📦 題庫數據")
    st.metric("題庫總數", f"{len(st.session_state['question_pool'])} 題")
    if st.button("🗑️ 清空題庫"):
        st.session_state['question_pool'] = []
        st.rerun()

# --- 分頁 ---
tab1, tab2, tab3 = st.tabs(["🧠 智慧匯入", "✍️ 手動輸入", "🚀 組卷匯出"])

# === Tab 1: 智慧匯入 ===
with tab1:
    st.subheader("試卷影像分析")
    st.markdown("支援 **PDF 掃描檔**。建議使用 **Gemini AI** 以獲得最佳效果。")
    
    raw_file = st.file_uploader("上傳 PDF 試卷", type=['pdf'], key="raw_upload")
    
    col_method, col_action = st.columns([1, 1])
    with col_method:
        # 檢查 smart_importer 是否有 OCR 可用
        ocr_status = " (可用)" if smart_importer.OCR_AVAILABLE else " (未安裝)"
        parse_method = st.radio("選擇辨識核心", ["Gemini AI (雲端)", f"本機 Regex/OCR{ocr_status}"], index=0)
    
    if raw_file:
        if st.button("🔍 開始分析", type="primary"):
            file_bytes = raw_file.read()
            candidates = []
            
            with st.spinner("正在讀取試卷..."):
                if "Gemini" in parse_method:
                    if not api_key_input:
                        st.error("請先在側邊欄輸入 Gemini API Key！")
                    else:
                        with st.spinner("🤖 Gemini 正在閱讀考卷... (約需 10-20 秒)"):
                            result = smart_importer.parse_with_gemini(file_bytes, 'pdf', api_key_input)
                            if isinstance(result, dict) and "error" in result:
                                st.error(result["error"])
                            else:
                                candidates = result
                else:
                    # 使用舊版邏輯 (需確認 smart_importer 有此函式)
                    candidates = smart_importer.parse_raw_file(io.BytesIO(file_bytes), 'pdf', use_ocr=True)
            
            st.session_state['imported_candidates'] = candidates
            
            if candidates:
                st.success(f"成功辨識出 {len(candidates)} 題！")
            elif not candidates and "Gemini" not in parse_method:
                 st.warning("本機模式未偵測到題目。請嘗試使用 Gemini AI 模式。")

    # 顯示分析結果
    if st.session_state['imported_candidates']:
        st.divider()
        st.subheader("📋 辨識結果確認")
        
        editor_data = []
        for cand in st.session_state['imported_candidates']:
            # 處理選項顯示
            opt_display = cand.options
            if isinstance(opt_display, list):
                opt_display = "\n".join(opt_display)
                
            editor_data.append({
                "加入": True,
                "題號": cand.number,
                "章節": cand.predicted_chapter,
                "題目內容": cand.content,
                "選項": opt_display
            })
            
        edited_df = st.data_editor(
            pd.DataFrame(editor_data),
            column_config={
                "加入": st.column_config.CheckboxColumn("加入", width="small"),
                "題目內容": st.column_config.TextColumn("題目內容", width="large"),
                "章節": st.column_config.SelectboxColumn("章節", options=smart_importer.PHYSICS_CHAPTERS_LIST + ["未分類"]),
                "選項": st.column_config.TextColumn("選項", width="medium"),
            },
            use_container_width=True
        )
        
        if st.button("✅ 確認匯入題庫"):
            count = 0
            # 取得編輯後的資料
            # Streamlit data_editor 回傳的是使用者修改後的 DataFrame
            # 我們需要遍歷這個 DataFrame
            
            # 注意：data_editor 回傳的索引可能與原始 list 對應
            # 但若使用者排序過，index 會亂掉，建議直接使用 edited_df 的資料建立新題目
            
            for index, row in edited_df.iterrows():
                if row["加入"]:
                    # 解析選項字串回列表
                    opts_str = row["選項"]
                    opts_list = []
                    if isinstance(opts_str, str):
                        opts_list = opts_str.split('\n')
                    elif isinstance(opts_str, list):
                        opts_list = opts_str
                    
                    new_q = Question(
                        q_type="Single" if opts_list else "Fill",
                        content=row["題目內容"],
                        options=opts_list,
                        answer="",
                        original_id=row["題號"],
                        source="Gemini匯入",
                        chapter=row["章節"]
                    )
                    st.session_state['question_pool'].append(new_q)
                    count += 1
            
            st.success(f"已匯入 {count} 題！")
            st.session_state['imported_candidates'] = [] # 清空暫存
            time.sleep(1) # 讓使用者看到成功訊息
            st.rerun()

# === Tab 2: 手動輸入 ===
with tab2:
    st.subheader("手動輸入題目")
    # 簡易手動輸入介面
    m_source = st.selectbox("來源", SOURCES)
    m_chap = st.selectbox("章節", PHYSICS_CHAPTERS)
    m_content = st.text_area("題目")
    m_opts = st.text_area("選項 (一行一個)")
    if st.button("新增"):
        opts = m_opts.split('\n') if m_opts else []
        q = Question("Single", m_content, opts, source=m_source, chapter=m_chap)
        st.session_state['question_pool'].append(q)
        st.success("已新增")

# === Tab 3: 組卷匯出 ===
with tab3:
    st.subheader("下載試卷")
    if st.session_state['question_pool']:
        st.write(f"目前已選 {len(st.session_state['question_pool'])} 題")
        if st.button("生成 Word 檔"):
            f1, f2 = generate_word_files(st.session_state['question_pool'])
            col1, col2 = st.columns(2)
            col1.download_button("下載試題卷", f1, "exam.docx")
            col2.download_button("下載答案卷", f2, "ans.docx")
    else:
        st.info("題庫是空的，請先匯入題目")
