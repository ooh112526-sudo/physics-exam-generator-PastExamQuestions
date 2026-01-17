import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import random
import io
import re
import os
import google.generativeai as genai
from google.cloud import firestore

# ==========================================
# 雲端服務初始化
# ==========================================

# 1. 設定 Gemini AI
# 嘗試從環境變數 (Cloud Run) 或 Streamlit secrets (本機開發) 讀取 Key
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY")

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    st.warning("⚠️ 未偵測到 Google API Key，AI 功能將無法使用。")

# 2. 設定 Firestore 資料庫
# 在 Cloud Run 上通常不需要額外憑證 (自動抓取專案權限)
# 若在本機執行發生錯誤，請確保已登入 gcloud auth application-default login
try:
    db = firestore.Client()
    use_firestore = True
except Exception as e:
    use_firestore = False
    print(f"Firestore 連線失敗 (可能是在本機且未設定憑證): {e}")

# ==========================================
# 設定頁面資訊
# ==========================================
st.set_page_config(page_title="物理題庫系統 (雲端版)", layout="wide", page_icon="🧲")

# ==========================================
# 常數定義
# ==========================================

SOURCES = ["一般試題", "學測題", "北模", "全模", "中模"]

PHYSICS_CHAPTERS = {
    "第一章.科學的態度與方法": ["1-1 科學的態度", "1-2 科學的方法", "1-3 國際單位制", "1-4 物理學簡介"],
    "第二章.物體的運動": ["2-1 物體的運動", "2-2 牛頓三大運動定律", "2-3 生活中常見的力", "2-4 天體運動"],
    "第三章. 物質的組成與交互作用": ["3-1 物質的組成", "3-2 原子的結構", "3-3 基本交互作用"],
    "第四章.電與磁的統一": ["4-1 電流磁效應", "4-2 電磁感應", "4-3 電與磁的整合", "4-4 光波的特性", "4-5 都卜勒效應"],
    "第五章. 能　量": ["5-1 能量的形式", "5-2 微觀尺度下的能量", "5-3 能量守恆", "5-4 質能互換"],
    "第六章.量子現象": ["6-1 量子論的誕生", "6-2 光的粒子性", "6-3 物質的波動性", "6-4 波粒二象性", "6-5 原子光譜"]
}

# ==========================================
# 核心邏輯類別與函式
# ==========================================

class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=None, image_data=None, 
                 source="一般試題", chapter="", unit=""):
        self.id = original_id # Firestore Document ID
        self.type = q_type
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data

    def to_dict(self):
        """轉換為 Firestore 儲存格式"""
        return {
            "type": self.type,
            "source": self.source,
            "chapter": self.chapter,
            "unit": self.unit,
            "content": self.content,
            "options": self.options,
            "answer": self.answer,
            # 圖片通常建議存到 Cloud Storage 並存網址，這裡為簡化先略過二進位資料儲存
            # "image_data": self.image_data 
            "created_at": firestore.SERVER_TIMESTAMP
        }

def fetch_questions_from_db():
    """從 Firestore 撈取所有題目"""
    if not use_firestore:
        return st.session_state.get('local_pool', [])
    
    questions = []
    # 讀取 'questions' 集合
    docs = db.collection('questions').order_by('created_at', direction=firestore.Query.DESCENDING).stream()
    for doc in docs:
        data = doc.to_dict()
        q = Question(
            q_type=data.get('type'),
            content=data.get('content'),
            options=data.get('options'),
            answer=data.get('answer'),
            original_id=doc.id, # 記錄文件 ID 以便刪除
            source=data.get('source'),
            chapter=data.get('chapter'),
            unit=data.get('unit')
        )
        questions.append(q)
    return questions

def save_question_to_db(question):
    """儲存題目到 Firestore"""
    if not use_firestore:
        if 'local_pool' not in st.session_state: st.session_state['local_pool'] = []
        st.session_state['local_pool'].append(question)
        return
    
    db.collection('questions').add(question.to_dict())

def delete_question_from_db(doc_id):
    """從 Firestore 刪除題目"""
    if use_firestore and doc_id:
        db.collection('questions').document(doc_id).delete()

def ai_enhance_question(content):
    """呼叫 Gemini AI 改寫或潤飾題目"""
    if not GOOGLE_API_KEY: return "請先設定 API Key"
    
    try:
        model = genai.GenerativeModel('gemini-2.0-flash') # 使用較快且便宜的模型
        prompt = f"你是高中物理老師。請幫我潤飾以下物理題目，使其敘述更精確、符合高中課綱，並保留原意。請直接輸出修改後的題目內容即可：\n\n{content}"
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI 發生錯誤: {e}"

def generate_word_files(selected_questions, shuffle=True):
    """生成 Word 試卷 (維持原邏輯，略做簡化)"""
    exam_doc = docx.Document()
    ans_doc = docx.Document()
    
    style = exam_doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    
    exam_doc.add_heading('物理科 試題卷', 0)
    ans_doc.add_heading('物理科 答案卷', 0)
    
    for idx, q in enumerate(selected_questions, 1):
        # 簡單處理選項亂數
        current_opts = q.options.copy()
        current_ans = q.answer
        
        if shuffle and q.type in ['Single', 'Multi']:
            # 這裡簡化亂數邏輯，僅示範
            pass 

        # --- 試題卷 ---
        p = exam_doc.add_paragraph()
        q_type_text = {'Single': '單選', 'Multi': '多選', 'Fill': '填充'}.get(q.type, '未知')
        runner = p.add_run(f"{idx}. ({q_type_text}) {q.content}")
        runner.bold = True
        
        if q.type != 'Fill':
            for i, opt in enumerate(current_opts):
                exam_doc.add_paragraph(f"({chr(65+i)}) {opt}")
        else:
            exam_doc.add_paragraph("______________________")
        exam_doc.add_paragraph("") 
        
        # --- 答案卷 ---
        ans_p = ans_doc.add_paragraph()
        ans_p.add_run(f"{idx}. {current_ans}")

    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io

# ==========================================
# Streamlit 主介面
# ==========================================

st.title("🧲 物理題庫系統 (Cloud Ver.)")

if not use_firestore:
    st.warning("⚠️ 目前使用「本機暫存模式」，重新整理後資料將消失。請確認 Cloud Firestore 已啟用。")
else:
    st.success("☁️ 已連線至雲端資料庫")

# 讀取題庫
question_pool = fetch_questions_from_db()

# --- 側邊欄 ---
with st.sidebar:
    st.header("📦 題庫狀態")
    st.metric("雲端題庫總數", f"{len(question_pool)} 題")
    st.markdown("---")
    st.markdown("**功能說明**")
    st.markdown("- **新增題目**：可手動輸入或貼上。")
    st.markdown("- **AI 潤飾**：使用 Gemini 優化題目敘述。")
    st.markdown("- **組卷**：勾選題目後下載 Word 檔。")

# --- 主畫面 ---
tab1, tab2 = st.tabs(["✍️ 新增題目", "🚀 選題與匯出"])

# === Tab 1: 新增題目 ===
with tab1:
    st.subheader("新增單一題目")
    
    col_cat1, col_cat2, col_cat3 = st.columns(3)
    with col_cat1: new_q_source = st.selectbox("來源", SOURCES)
    with col_cat2: new_q_chap = st.selectbox("章節", list(PHYSICS_CHAPTERS.keys()))
    with col_cat3: new_q_unit = st.selectbox("單元", PHYSICS_CHAPTERS[new_q_chap])

    c1, c2 = st.columns([1, 3])
    with c1: new_q_type = st.selectbox("題型", ["Single", "Multi", "Fill"])
    with c2: new_q_ans = st.text_input("正確答案")

    new_q_content = st.text_area("題目內容", height=100)
    
    # AI 輔助按鈕
    if st.button("✨ AI 潤飾題目"):
        with st.spinner("AI 正在思考中..."):
            enhanced_text = ai_enhance_question(new_q_content)
            st.code(enhanced_text, language='text')
            st.info("請將上方優化後的文字複製回題目內容欄位。")

    new_q_options = []
    if new_q_type in ["Single", "Multi"]:
        opts_text = st.text_area("選項 (每行一個)", height=100)
        if opts_text: new_q_options = [line.strip() for line in opts_text.split('\n') if line.strip()]

    if st.button("➕ 儲存到雲端資料庫", type="primary"):
        if new_q_content:
            new_q = Question(
                new_q_type, new_q_content, new_q_options, new_q_ans, 
                source=new_q_source, chapter=new_q_chap, unit=new_q_unit
            )
            save_question_to_db(new_q)
            st.success("✅ 題目已儲存！")
            st.rerun()
        else:
            st.error("請輸入內容")

# === Tab 2: 選題與匯出 ===
with tab2:
    st.subheader("從資料庫選題")
    
    if not question_pool:
        st.info("目前資料庫是空的，請先去新增題目。")
    else:
        # 篩選器
        filter_chap = st.selectbox("篩選章節", ["全部"] + list(PHYSICS_CHAPTERS.keys()))
        
        filtered_qs = question_pool
        if filter_chap != "全部":
            filtered_qs = [q for q in question_pool if q.chapter == filter_chap]

        # 顯示列表
        selected_indices = []
        st.write(f"顯示 {len(filtered_qs)} 筆資料")
        
        for i, q in enumerate(filtered_qs):
            with st.expander(f"{q.unit} | {q.content[:20]}..."):
                st.write(q.content)
                st.caption(f"答案: {q.answer}")
                col_btn1, col_btn2 = st.columns([1, 5])
                with col_btn1:
                    if st.checkbox("選取", key=f"sel_{q.id or i}"):
                        selected_indices.append(q)
                with col_btn2:
                    if st.button("刪除", key=f"del_{q.id or i}"):
                        delete_question_from_db(q.id)
                        st.rerun()

        if st.button("🚀 生成 Word 試卷", disabled=len(selected_indices)==0):
            exam_file, ans_file = generate_word_files(selected_indices)
            st.download_button("下載試題卷", exam_file, "試題卷.docx")
            st.download_button("下載詳解卷", ans_file, "詳解卷.docx")
