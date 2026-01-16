import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
from docx.enum.text import WD_ALIGN_PARAGRAPH
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
# 工具函式 (Word 生成優化)
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

        # === 智慧選項排版 ===
        if q.type in ['Single', 'Multi'] and q.options:
            opts = q.options
            # 計算平均長度與最大長度
            max_len = max([len(str(o)) for o in opts]) if opts else 0
            
            # 策略：
            # 1. 非常短 (< 10字)：單行並排
            # 2. 短 (< 25字)：雙欄排列 (使用表格隱藏邊框)
            # 3. 長：垂直排列
            
            if max_len < 10 and len(opts) > 0:
                # 單行顯示 (用全形空白分隔)
                doc.add_paragraph("　　".join(opts))
                
            elif max_len < 25 and len(opts) > 0 and len(opts) % 2 == 0:
                # 雙欄表格
                table = doc.add_table(rows=(len(opts) // 2), cols=2)
                table.autofit = True
                # 移除邊框 (這裡不實作複雜的XML操作，預設無邊框或細線)
                for i, opt in enumerate(opts):
                    row = i // 2
                    col = i % 2
                    table.cell(row, col).text = opt
                doc.add_paragraph("") # 表格後空行
            else:
                # 垂直排列
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
        exam_session_opts = [""] 
        if "模" in exam_type:
            exam_session_opts = ["第1次", "第2次", "第3次", "第4次"]
        exam_session = st.selectbox("場次 (僅模考)", exam_session_opts) if "模" in exam_type else ""

    final_source_tag = f"{exam_year}-{exam_type}"
    if exam_session: final_source_tag += f"-{exam_session}"
    if exam_type == "自行輸入":
        final_source_tag = st.text_input("自訂來源名稱", value=f"{exam_year}-自訂試卷")

    st.divider()
    st.markdown("### 2. 上傳試卷 (PDF / Word)")
    raw_file = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'])
    
    if raw_file and st.button("開始 AI 分析"):
        if not api_key:
            st.error("請輸入 API Key")
        else:
            file_type = raw_file.name.split('.')[-1].lower()
            with st.spinner("🤖 Gemini 正在分批閱讀試卷 (會過濾非物理題)..."):
                res = smart_importer.parse_with_gemini(raw_file.read(), file_type, api_key)
                if isinstance(res, dict) and "error" in res:
                    st.error(res["error"])
                else:
                    st.session_state['imported_candidates'] = res
                    st.success(f"成功辨識 {len(res)} 題！")

    if st.session_state['imported_candidates']:
        st.divider()
        st.subheader("3. 匯入校對與截圖")
        st.info("請在此處檢查題型與輸入答案。若未輸入，匯入後仍可編輯。")
        
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
                    
                    type_idx = ["Single", "Multi", "Fill"].index(cand.q_type) if cand.q_type in ["Single", "Multi", "Fill"] else 0
                    new_type = st.selectbox(f"題型 #{i}", ["Single", "Multi", "Fill"], index=type_idx)
                    cand.q_type = new_type

                    ans_key = f"ans_import_{i}"
                    if ans_key not in st.session_state: st.session_state[ans_key] = ""
                    new_ans = st.text_input(f"答案 (可留空) #{i}", value=st.session_state[ans_key], key=ans_key)
                    
                    current_chap_idx = 0
                    if cand.predicted_chapter in smart_importer.PHYSICS_CHAPTERS_LIST:
                        current_chap_idx = smart_importer.PHYSICS_CHAPTERS_LIST.index(cand.predicted_chapter)
                    
                    new_chap = st.selectbox(f"章節分類 #{i}", smart_importer.PHYSICS_CHAPTERS_LIST, index=current_chap_idx)
                    cand.predicted_chapter = new_chap
                    
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
                                key=f"cropper_{i}",
                                aspect_ratio=None
                            )
                            col_c1, col_c2 = st.columns(2)
                            if col_c1.button(f"📷 設為附圖 #{i}"):
                                img_byte_arr = io.BytesIO()
                                cropped_img.save(img_byte_arr, format='PNG')
                                cand.image_bytes = img_byte_arr.getvalue()
                                st.success("附圖已更新")
                                st.rerun()
                            if col_c2.button(f"🚫 不使用圖片 #{i}"):
                                cand.image_bytes = None
                                st.success("附圖已移除")
                                st.rerun()
                        except Exception as e:
                            st.error(f"無法載入截圖工具: {e}")
                    else:
                        st.info("此題無參考截圖")
                st.divider()

        col_submit, _ = st.columns([1, 3])
        if col_submit.button("✅ 確認匯入", type="primary"):
            count = 0
            for i, cand in enumerate(st.session_state['imported_candidates']):
                ans_val = st.session_state.get(f"ans_import_{i}", "")
                
                new_q = Question(
                    q_type=cand.q_type,
                    content=cand.content,
                    options=cand.options,
                    source=final_source_tag, 
                    chapter=cand.predicted_chapter,
                    image_data=cand.image_bytes,
                    answer=ans_val 
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
        filter_src = st.multiselect("篩選來源", list(set([q.source for q in st.session_state['question_pool']])))
        filtered_pool = st.session_state['question_pool']
        if filter_src:
            filtered_pool = [q for q in st.session_state['question_pool'] if q.source in filter_src]

        for i, q in enumerate(filtered_pool):
            type_badge = {'Single': '單', 'Multi': '多', 'Fill': '填', 'Group': '題組'}.get(q.type, '未知')
            if q.is_group_parent:
                type_badge = "題組"
                
            with st.expander(f"[{q.source}] [{type_badge}] {q.content[:30]}..."):
                c1, c2 = st.columns([2, 1])
                with c1:
                    q.content = st.text_area(f"題目內容 #{q.id}", q.content, height=100)
                    
                    if not q.is_group_parent:
                        opts_str = st.text_area(f"選項 #{q.id}", "\n".join(q.options), height=100)
                        q.options = opts_str.split('\n') if opts_str else []
                        
                with c2:
                    q.type = st.selectbox(f"題型 #{q.id}", ["Single", "Multi", "Fill", "Group"], index=["Single", "Multi", "Fill", "Group"].index(q.type) if q.type in ["Single", "Multi", "Fill", "Group"] else 0)
                    
                    if q.type == "Group":
                        q.is_group_parent = True
                    else:
                        q.is_group_parent = False
                    
                    chap_idx = 0
                    if q.chapter in smart_importer.PHYSICS_CHAPTERS_LIST:
                        chap_idx = smart_importer.PHYSICS_CHAPTERS_LIST.index(q.chapter)
                    q.chapter = st.selectbox(f"章節 #{q.id}", smart_importer.PHYSICS_CHAPTERS_LIST, index=chap_idx)
                    
                    if not q.is_group_parent:
                        q.answer = st.text_input(f"答案 #{q.id}", q.answer)
                    
                    if st.button(f"💾 儲存 #{q.id}"):
                        firebase_db.save_question_to_cloud(q.to_dict())
                        st.success("儲存成功")
                    if st.button(f"🗑️ 刪除 #{q.id}", type="primary"):
                        firebase_db.delete_question_from_cloud(q.id)
                        st.rerun()

                if q.is_group_parent:
                    st.markdown("---")
                    st.markdown("#### 📂 子題目管理")
                    
                    if q.sub_questions:
                        for sub_idx, sub_q in enumerate(q.sub_questions):
                            st.markdown(f"**子題 {sub_idx+1}**")
                            sc1, sc2 = st.columns([3, 1])
                            with sc1:
                                sub_q.content = st.text_input(f"子題題目 #{sub_q.id}", sub_q.content)
                                sub_opts = st.text_area(f"子題選項 #{sub_q.id}", "\n".join(sub_q.options), height=60)
                                sub_q.options = sub_opts.split('\n') if sub_opts else []
                            with sc2:
                                sub_q.type = st.selectbox(f"子題類型 #{sub_q.id}", ["Single", "Multi", "Fill"], index=["Single", "Multi", "Fill"].index(sub_q.type))
                                sub_q.answer = st.text_input(f"子題答案 #{sub_q.id}", sub_q.answer)
                                if st.button(f"移除子題 #{sub_q.id}"):
                                    q.sub_questions.pop(sub_idx)
                                    firebase_db.save_question_to_cloud(q.to_dict())
                                    st.rerun()
                            st.divider()

                    if st.button(f"➕ 新增子題至 #{q.id}"):
                        new_sub = Question(
                            q_type="Single", 
                            content="新子題...", 
                            options=["(A)", "(B)"],
                            parent_id=q.id
                        )
                        q.sub_questions.append(new_sub)
                        firebase_db.save_question_to_cloud(q.to_dict())
                        st.rerun()

# === Tab 3: 組卷匯出 ===
with tab3:
    st.subheader("生成 Word 試卷")
    if st.button("生成並下載"):
        f1, f2 = generate_word_files(st.session_state['question_pool'])
        st.download_button("下載試題卷", f1, "exam.docx")
        st.download_button("下載答案卷", f2, "ans.docx")
