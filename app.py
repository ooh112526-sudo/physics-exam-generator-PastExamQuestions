import streamlit as st
import docx
from docx.shared import Pt
import io
import os
import google.generativeai as genai
from google.cloud import firestore

# 引入我們獨立出來的模組
from smart_importer import Question, parse_docx

# ==========================================
# 雲端服務初始化
# ==========================================

# 1. 設定 Gemini AI
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY")

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    # 為了不讓畫面太亂，只在真的要用 AI 時顯示警告
    pass

# 2. 設定 Firestore 資料庫
try:
    db = firestore.Client()
    use_firestore = True
except Exception as e:
    use_firestore = False
    print(f"Firestore 連線失敗: {e}")

# ==========================================
# 設定頁面資訊
# ==========================================
st.set_page_config(page_title="物理題庫系統 (雲端版)", layout="wide", page_icon="🧲")

# ==========================================
# 常數與輔助函式
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

def fetch_questions_from_db():
    """從 Firestore 撈取所有題目"""
    if not use_firestore:
        return st.session_state.get('local_pool', [])
    
    questions = []
    # 讀取 'questions' 集合
    docs = db.collection('questions').order_by('created_at', direction=firestore.Query.DESCENDING).stream()
    for doc in docs:
        q = Question.from_dict(doc.id, doc.to_dict())
        questions.append(q)
    return questions

def save_question_to_db(question):
    """儲存題目到 Firestore"""
    if not use_firestore:
        if 'local_pool' not in st.session_state: st.session_state['local_pool'] = []
        st.session_state['local_pool'].append(question)
        return
    
    db.collection('questions').add(question.to_firestore_dict())

def delete_question_from_db(doc_id):
    """從 Firestore 刪除題目"""
    if use_firestore and doc_id:
        db.collection('questions').document(doc_id).delete()

def ai_enhance_question(content):
    """呼叫 Gemini AI 改寫或潤飾題目"""
    if not GOOGLE_API_KEY: return "請先設定 API Key"
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        prompt = f"你是高中物理老師。請幫我潤飾以下物理題目，使其敘述更精確、符合高中課綱。請直接輸出修改後的題目內容：\n\n{content}"
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI 發生錯誤: {e}"

def generate_word_files(selected_questions, shuffle=True):
    """生成 Word 試卷"""
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
        current_ans = q.answer # 暫時不實作複雜的選項重排對應答案功能，保留給使用者擴充
        
        # --- 試題卷 ---
        p = exam_doc.add_paragraph()
        q_type_text = {'Single': '單選', 'Multi': '多選', 'Fill': '填充'}.get(q.type, '未知')
        runner = p.add_run(f"{idx}. ({q_type_text}) {q.content}")
        runner.bold = True
        
        # 如果有二進位圖片 (來自手動上傳或 Word 匯入的暫存)，可以嘗試寫入 Word
        # 注意：若從 Firestore 讀回，因為我們沒存圖，這裡會是 None
        if q.image_data:
            try:
                img_stream = io.BytesIO(q.image_data)
                exam_doc.add_picture(img_stream, width=docx.shared.Inches(3.0))
            except:
                pass

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
    st.warning("⚠️ 目前使用「本機暫存模式」，資料不會永久保存。")
else:
    st.success("☁️ 已連線至雲端資料庫")

# 讀取題庫
question_pool = fetch_questions_from_db()

# --- 側邊欄 ---
with st.sidebar:
    st.header("📦 題庫狀態")
    st.metric("雲端題庫總數", f"{len(question_pool)} 題")
    st.divider()
    
    # 下載範本功能
    st.subheader("Word 匯入範本")
    st.caption("請依照範本格式編寫 Word 檔以便系統解析。")
    sample_doc = docx.Document()
    sample_doc.add_paragraph("[Src:北模]")
    sample_doc.add_paragraph("[Chap:第四章.電與磁的統一]")
    sample_doc.add_paragraph("[Unit:4-1 電流磁效應]")
    sample_doc.add_paragraph("[Type:Single]\n[Q]\n(範例) 一載流長直導線...\n[Opt]\n(A)選項一\n(B)選項二\n[Ans] A")
    sample_io = io.BytesIO()
    sample_doc.save(sample_io)
    sample_io.seek(0)
    st.download_button("📥 下載 Word 範本", sample_io, "template.docx")

# --- 主畫面 Tab ---
tab1, tab2, tab3 = st.tabs(["✍️ 新增題目", "📁 從 Word 匯入", "🚀 選題與匯出"])

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

    if st.button("➕ 儲存到雲端", type="primary"):
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

# === Tab 2: 從 Word 匯入 (Restore) ===
with tab2:
    st.subheader("批次匯入題目")
    st.info("請上傳符合格式的 .docx 檔案，系統將自動解析並準備上傳至雲端。")
    
    uploaded_file = st.file_uploader("上傳 Word 檔案", type=['docx'])
    
    if uploaded_file:
        try:
            # 使用 smart_importer 解析
            imported_qs = parse_docx(uploaded_file.read())
            
            if imported_qs:
                st.success(f"成功解析出 {len(imported_qs)} 題！")
                
                # 預覽區塊
                with st.expander("點此預覽解析結果"):
                    for i, q in enumerate(imported_qs[:5]): # 只預覽前 5 題
                        st.markdown(f"**{i+1}. [{q.type}] {q.content[:30]}...** (Ans: {q.answer})")
                
                # 確認上傳按鈕
                if st.button(f"☁️ 確認上傳 {len(imported_qs)} 題至雲端資料庫", type="primary"):
                    progress_bar = st.progress(0)
                    for idx, q in enumerate(imported_qs):
                        save_question_to_db(q)
                        progress_bar.progress((idx + 1) / len(imported_qs))
                    
                    st.success("全數上傳完成！")
                    st.balloons()
                    # 延遲後重整頁面
                    import time
                    time.sleep(1)
                    st.rerun()
            else:
                st.warning("檔案中未偵測到題目，請檢查標籤格式 (如 [Type:Single], [Q], [Ans])。")
                
        except Exception as e:
            st.error(f"解析檔案時發生錯誤：{e}")

# === Tab 3: 選題與匯出 ===
with tab3:
    st.subheader("從資料庫選題")
    
    if not question_pool:
        st.info("目前資料庫是空的，請先去新增題目。")
    else:
        # 篩選器
        filter_col1, filter_col2 = st.columns(2)
        with filter_col1:
            filter_chap = st.selectbox("篩選章節", ["全部"] + list(PHYSICS_CHAPTERS.keys()))
        with filter_col2:
            filter_source = st.selectbox("篩選來源", ["全部"] + SOURCES)
        
        filtered_qs = question_pool
        if filter_chap != "全部":
            filtered_qs = [q for q in filtered_qs if q.chapter == filter_chap]
        if filter_source != "全部":
            filtered_qs = [q for q in filtered_qs if q.source == filter_source]

        # 顯示列表
        selected_indices = []
        st.write(f"符合條件： {len(filtered_qs)} 筆")
        
        # 全選功能
        if st.checkbox("全選顯示的題目"):
            selected_indices = filtered_qs
        
        # 列表顯示
        for i, q in enumerate(filtered_qs):
            col_check, col_content = st.columns([0.5, 9.5])
            with col_check:
                # 如果已經全選，就預設勾選，否則手動勾選
                is_selected = q in selected_indices
                if st.checkbox("", key=f"chk_{q.id or i}", value=is_selected):
                    if q not in selected_indices:
                        selected_indices.append(q)
            
            with col_content:
                with st.expander(f"[{q.source}] {q.unit} | {q.content[:30]}..."):
                    st.write(q.content)
                    if q.options:
                        st.write("選項:", q.options)
                    st.caption(f"答案: {q.answer}")
                    if st.button("刪除此題", key=f"del_{q.id or i}"):
                        delete_question_from_db(q.id)
                        st.rerun()

        st.divider()
        st.write(f"已選擇 **{len(selected_indices)}** 題")

        if st.button("🚀 生成 Word 試卷", disabled=len(selected_indices)==0):
            exam_file, ans_file = generate_word_files(selected_indices)
            col_d1, col_d2 = st.columns(2)
            with col_d1:
                st.download_button("📄 下載試題卷", exam_file, "試題卷.docx")
            with col_d2:
                st.download_button("🔑 下載詳解卷", ans_file, "詳解卷.docx")
