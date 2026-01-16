import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import random
import io
import pandas as pd
import time
import base64

# 引用模組
import smart_importer
import firebase_db

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

# ==========================================
# 資料結構與狀態初始化
# ==========================================
class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=0, image_data=None, 
                 source="一般試題", chapter="", unit="", db_id=None):
        self.id = db_id if db_id else str(int(time.time()*1000)) # 使用時間戳當 ID
        self.type = q_type
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data # bytes

    def to_dict(self):
        """序列化為字典 (存 Firestore 用)"""
        # image_data bytes 需轉為 base64 字串才能存 JSON/Firestore
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
        """從字典還原"""
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
            chapter=data.get("chapter", ""),
            db_id=data.get("id")
        )

# 初始化 Session State
if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
    # 嘗試從雲端載入
    cloud_data = firebase_db.load_questions_from_cloud()
    if cloud_data:
        st.session_state['question_pool'] = [Question.from_dict(d) for d in cloud_data]
        st.toast(f"已從雲端載入 {len(cloud_data)} 題", icon="☁️")
    else:
        # 如果沒有雲端資料，也沒有設定檔
        if not firebase_db.get_db():
            st.warning("⚠️ 未偵測到 Firebase 設定。題目將只保留在本次操作中 (重新整理後消失)。")

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
        runner = p.add_run(f"{idx}. {q.content.strip()}")
        runner.bold = True
        
        # 插入圖片
        if q.image_data:
            try:
                exam_doc.add_picture(io.BytesIO(q.image_data), width=Inches(2.5))
            except: pass

        if q.type != 'Fill':
            for i, opt in enumerate(q.options):
                exam_doc.add_paragraph(f"{opt}")
        else:
            exam_doc.add_paragraph("______________________")
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
st.caption("功能：AI 圖文辨識 (PDF/Word) | 雲端儲存 | 題庫編輯")

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
        else:
            st.error("未設定 Firebase secrets")

tab1, tab2, tab3 = st.tabs(["🧠 智慧匯入", "📝 題庫管理 & 編輯", "🚀 組卷匯出"])

# === Tab 1: 智慧匯入 (PDF/Word) ===
with tab1:
    st.markdown("### 上傳試卷 (PDF / Word)")
    raw_file = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'])
    
    if raw_file and st.button("開始 AI 分析"):
        if not api_key:
            st.error("請輸入 API Key")
        else:
            file_type = raw_file.name.split('.')[-1].lower()
            with st.spinner("🤖 Gemini 正在分析題目與擷取圖片..."):
                res = smart_importer.parse_with_gemini(raw_file.read(), file_type, api_key)
                if isinstance(res, dict) and "error" in res:
                    st.error(res["error"])
                else:
                    st.session_state['imported_candidates'] = res
                    st.success(f"成功辨識 {len(res)} 題！")

    # 匯入預覽區
    if st.session_state['imported_candidates']:
        st.divider()
        st.subheader("預覽與勾選")
        
        # 轉換為 DataFrame 供編輯
        preview_list = []
        for i, cand in enumerate(st.session_state['imported_candidates']):
            preview_list.append({
                "加入": True,
                "內容": cand.content,
                "選項": "\n".join(cand.options) if cand.options else "",
                "章節": cand.predicted_chapter,
                "有圖片": "✅" if cand.image_bytes else ""
            })
            
        edited = st.data_editor(
            pd.DataFrame(preview_list),
            column_config={
                "加入": st.column_config.CheckboxColumn(width="small"),
                "內容": st.column_config.TextColumn(width="large"),
                "章節": st.column_config.SelectboxColumn(options=smart_importer.PHYSICS_CHAPTERS_LIST)
            },
            use_container_width=True
        )
        
        if st.button("確認匯入"):
            count = 0
            for idx, row in edited.iterrows():
                if row["加入"]:
                    cand = st.session_state['imported_candidates'][idx]
                    # 使用使用者編輯過的資料
                    opts = row["選項"].split('\n') if row["選項"] else []
                    
                    new_q = Question(
                        q_type="Single" if opts else "Fill",
                        content=row["內容"],
                        options=opts,
                        source="AI匯入",
                        chapter=row["章節"],
                        image_data=cand.image_bytes # 帶入自動截圖的圖片
                    )
                    
                    st.session_state['question_pool'].append(new_q)
                    # 同步存雲端
                    firebase_db.save_question_to_cloud(new_q.to_dict())
                    count += 1
            st.success(f"匯入 {count} 題並已嘗試儲存至雲端！")
            st.session_state['imported_candidates'] = []
            st.rerun()

# === Tab 2: 題庫管理 & 編輯 ===
with tab2:
    st.subheader("題庫列表 (可編輯)")
    
    if not st.session_state['question_pool']:
        st.info("目前沒有題目。")
    else:
        # 顯示題目列表，每一個題目一個 Expander
        for i, q in enumerate(st.session_state['question_pool']):
            with st.expander(f"{i+1}. [{q.chapter}] {q.content[:30]}..."):
                # 編輯模式
                c1, c2 = st.columns([2, 1])
                with c1:
                    new_content = st.text_area(f"題目內容 #{i}", q.content, height=100)
                    new_opts_str = st.text_area(f"選項 (換行分隔) #{i}", "\n".join(q.options), height=100)
                with c2:
                    new_chap = st.selectbox(f"章節 #{i}", smart_importer.PHYSICS_CHAPTERS_LIST, index=smart_importer.PHYSICS_CHAPTERS_LIST.index(q.chapter) if q.chapter in smart_importer.PHYSICS_CHAPTERS_LIST else 0)
                    new_ans = st.text_input(f"答案 #{i}", q.answer)
                    
                    # 圖片管理
                    if q.image_data:
                        st.image(q.image_data, caption="目前附圖", width=200)
                        if st.button(f"刪除圖片 #{i}"):
                            q.image_data = None
                            st.rerun()
                    else:
                        uploaded_img = st.file_uploader(f"上傳圖片 #{i}", type=["png", "jpg"], key=f"up_{i}")
                        if uploaded_img:
                            q.image_data = uploaded_img.read()
                            st.rerun()

                col_save, col_del = st.columns(2)
                if col_save.button(f"💾 儲存修改 #{i}"):
                    q.content = new_content
                    q.options = new_opts_str.split('\n') if new_opts_str else []
                    q.chapter = new_chap
                    q.answer = new_ans
                    # 同步更新雲端
                    firebase_db.save_question_to_cloud(q.to_dict())
                    st.success("已更新！")
                
                if col_del.button(f"🗑️ 刪除題目 #{i}", type="primary"):
                    firebase_db.delete_question_from_cloud(q.id)
                    st.session_state['question_pool'].pop(i)
                    st.rerun()

# === Tab 3: 組卷匯出 ===
with tab3:
    st.subheader("生成 Word 試卷")
    # (保留原功能)
    if st.button("生成並下載"):
        f1, f2 = generate_word_files(st.session_state['question_pool'])
        st.download_button("下載試題卷", f1, "exam.docx")
        st.download_button("下載答案卷", f2, "ans.docx")
