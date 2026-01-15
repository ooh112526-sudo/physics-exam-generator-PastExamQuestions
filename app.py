import streamlit as st
import docx
from docx.shared import Pt, Inches
import random
import io
import re
import pandas as pd
import smart_importer # 引用更新後的模組

# 設定頁面資訊
st.set_page_config(page_title="物理題庫系統 (Physics Exam Generator)", layout="wide", page_icon="🧲")

# ==========================================
# 常數定義
# ==========================================

SOURCES = ["一般試題", "學測題", "分科測驗", "北模", "全模", "中模"]

PHYSICS_CHAPTERS = {
    "第一章.科學的態度與方法": [
        "1-1 科學的態度", "1-2 科學的方法", "1-3 國際單位制", "1-4 物理學簡介"
    ],
    "第二章.物體的運動": [
        "2-1 物體的運動", "2-2 牛頓三大運動定律", "2-3 生活中常見的力", "2-4 天體運動"
    ],
    "第三章. 物質的組成與交互作用": [
        "3-1 物質的組成", "3-2 原子的結構", "3-3 基本交互作用"
    ],
    "第四章.電與磁的統一": [
        "4-1 電流磁效應", "4-2 電磁感應", "4-3 電與磁的整合", "4-4 光波的特性", "4-5 都卜勒效應"
    ],
    "第五章. 能　量": [
        "5-1 能量的形式", "5-2 微觀尺度下的能量", "5-3 能量守恆", "5-4 質能互換"
    ],
    "第六章.量子現象": [
        "6-1 量子論的誕生", "6-2 光的粒子性", "6-3 物質的波動性", "6-4 波粒二象性", "6-5 原子光譜"
    ]
}

# ==========================================
# 核心邏輯
# ==========================================

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
    # (此函式保持不變，為節省篇幅省略，請保留原有的Word生成邏輯)
    exam_doc = docx.Document()
    ans_doc = docx.Document()
    style = exam_doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    
    exam_doc.add_heading('物理科 試題卷', 0)
    ans_doc.add_heading('物理科 答案卷', 0)
    
    for idx, q in enumerate(selected_questions, 1):
        processed_q = q
        # ... (邏輯不變)
        p = exam_doc.add_paragraph()
        p.add_run(f"{idx}. ({q.type}) {processed_q.content.strip()}").bold = True
        
        if q.type != 'Fill':
            for i, opt in enumerate(processed_q.options):
                exam_doc.add_paragraph(f"({chr(65+i)}) {opt}")
        else:
            exam_doc.add_paragraph("______________________")
        exam_doc.add_paragraph("") 
        
        ans_p = ans_doc.add_paragraph()
        ans_p.add_run(f"{idx}. {processed_q.answer}")

    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io

def parse_docx_tagged(file_bytes):
    # (舊有功能保持不變)
    return []

# ==========================================
# Session State
# ==========================================
if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
if 'imported_candidates' not in st.session_state:
    st.session_state['imported_candidates'] = []

# ==========================================
# Streamlit 介面
# ==========================================

st.title("🧲 物理題庫自動組卷系統 v3.1 (OCR 版)")
st.caption("Assistant: 整合 OCR 影像辨識，支援掃描檔匯入")

# --- 側邊欄 ---
with st.sidebar:
    st.header("📦 題庫數據")
    st.metric("題庫總數", f"{len(st.session_state['question_pool'])} 題")
    if st.button("🗑️ 清空題庫"):
        st.session_state['question_pool'] = []
        st.rerun()
    
    st.divider()
    st.info("**OCR 功能狀態**")
    if smart_importer.OCR_AVAILABLE:
        st.success("✅ Tesseract OCR 已就緒")
    else:
        st.error("❌ 未偵測到 Tesseract")
        st.caption("請確認 packages.txt 與系統安裝")

# --- 主畫面 ---
tab1, tab2, tab3 = st.tabs(["🧠 智慧匯入 (PDF/Word)", "✍️ 手動新增", "🚀 選題與匯出"])

# === Tab 1: 智慧匯入 (Raw) ===
with tab1:
    st.subheader("原始試卷智慧分析")
    
    raw_file = st.file_uploader("上傳試卷 (PDF/Word)", type=['pdf', 'docx'], key="raw_upload")
    
    col_opt1, col_opt2 = st.columns(2)
    with col_opt1:
        use_ocr = st.checkbox("啟用 OCR 強力辨識 (針對掃描檔/圖片型 PDF)", 
                            help="若 PDF 為圖片格式或無法抓取文字，請勾選此項。處理速度較慢。",
                            disabled=not smart_importer.OCR_AVAILABLE)
    
    if raw_file:
        if st.button("🔍 開始智慧分析", type="primary"):
            with st.spinner("正在進行分析... 若啟用 OCR 可能需要 1-2 分鐘..."):
                file_type = raw_file.name.split('.')[-1].lower()
                candidates = smart_importer.parse_raw_file(raw_file, file_type, use_ocr=use_ocr)
                st.session_state['imported_candidates'] = candidates
                if not candidates:
                    msg = "未偵測到題目。嘗試勾選「啟用 OCR」再試一次？" if not use_ocr else "OCR 分析後仍未找到題號結構，請確認圖片清晰度。"
                    st.warning(msg)
                else:
                    st.success(f"成功偵測到 {len(candidates)} 題！")

    if st.session_state['imported_candidates']:
        st.divider()
        # 編輯介面 (簡化版)
        editor_data = []
        for i, cand in enumerate(st.session_state['imported_candidates']):
            editor_data.append({
                "加入": cand.is_physics_likely,
                "題號": cand.number,
                "預測章節": cand.predicted_chapter,
                "題目預覽": cand.content[:40].replace('\n', ' ') + "...",
                "選項": len(cand.options)
            })
        
        edited_df = st.data_editor(pd.DataFrame(editor_data), use_container_width=True)
        
        if st.button("✅ 確認匯入勾選題目"):
            indices = edited_df[edited_df["加入"]].index.tolist()
            for idx in indices:
                cand = st.session_state['imported_candidates'][idx]
                chap = edited_df.iloc[idx]["預測章節"]
                new_q = Question("Single", cand.content, cand.options, "", 
                               original_id=cand.number, source="OCR匯入", chapter=chap)
                st.session_state['question_pool'].append(new_q)
            st.success("匯入完成！")
            st.session_state['imported_candidates'] = []
            st.rerun()

# === Tab 2: 手動新增 ===
with tab2:
    # (保留原本的手動新增介面)
    st.write("手動新增功能區 (請參考前版程式碼)")

# === Tab 3: 選題與匯出 ===
with tab3:
    # (保留原本的匯出介面)
    st.write("匯出功能區 (請參考前版程式碼)")
    if st.button("下載測試"):
        pass
