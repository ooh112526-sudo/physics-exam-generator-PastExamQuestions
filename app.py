import streamlit as st
import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import random
import io
import re
import pandas as pd

# 引用我們剛寫好的核心模組
import smart_importer 

# ==========================================
# 1. 頁面設定
# ==========================================
st.set_page_config(
    page_title="物理題庫系統 (Physics Exam Generator)", 
    layout="wide", 
    page_icon="🧲"
)

# ==========================================
# 2. 常數與資料結構
# ==========================================

SOURCES = ["一般試題", "學測題", "分科測驗", "北模", "全模", "中模", "OCR匯入"]

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

class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=0, image_data=None, 
                 source="一般試題", chapter="", unit=""):
        self.id = original_id
        self.type = q_type  # 'Single', 'Multi', 'Fill'
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data

# ==========================================
# 3. 輔助函式 (Word 解析與生成)
# ==========================================

def extract_images_from_paragraph(paragraph, doc_part):
    """從 Word 段落中擷取圖片 (用於舊版標籤匯入)"""
    images = []
    nsmap = {
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    }
    blips = paragraph._element.findall('.//a:blip', namespaces=nsmap)
    for blip in blips:
        embed_attr = blip.get(f"{{{nsmap['r']}}}embed")
        if embed_attr and embed_attr in doc_part.rels:
            part = doc_part.rels[embed_attr].target_part
            if "image" in part.content_type:
                images.append(part.blob)
    return images

def parse_docx_tagged(file_bytes):
    """解析含有 [Src], [Q] 等標籤的 Word 檔案 (舊有功能)"""
    doc = docx.Document(io.BytesIO(file_bytes))
    doc_part = doc.part
    
    questions = []
    current_q = None
    state = None
    opt_pattern = re.compile(r'^\s*\(?[A-Ea-e]\)?\s*[.、]?\s*')
    q_id_counter = 1

    curr_src = "一般試題"
    curr_chap = ""
    curr_unit = ""

    for para in doc.paragraphs:
        text = para.text.strip()
        found_images = extract_images_from_paragraph(para, doc_part)
        
        # 標籤偵測
        if text.startswith('[Src:'):
            curr_src = text.split(':')[1].replace(']', '').strip()
            continue
        if text.startswith('[Chap:'):
            curr_chap = text.split(':')[1].replace(']', '').strip()
            continue
        if text.startswith('[Unit:'):
            curr_unit = text.split(':')[1].replace(']', '').strip()
            continue
        
        # 新題目開始
        if text.startswith('[Type:'):
            if current_q: questions.append(current_q)
            q_type_str = text.split(':')[1].replace(']', '').strip()
            current_q = Question(
                q_type=q_type_str, content="", options=[], answer="", 
                original_id=q_id_counter, source=curr_src, 
                chapter=curr_chap, unit=curr_unit
            )
            q_id_counter += 1
            state = None
            continue

        # 狀態切換
        if text.startswith('[Q]'):
            state = 'Q'; continue
        elif text.startswith('[Opt]'):
            state = 'Opt'; continue
        elif text.startswith('[Ans]'):
            remain_text = text.replace('[Ans]', '').strip()
            if remain_text and current_q: current_q.answer = remain_text
            state = 'Ans'; continue

        # 內容填充
        if current_q:
            if found_images and state == 'Q':
                current_q.image_data = found_images[0]
            if not text: continue
            if state == 'Q': current_q.content += text + "\n"
            elif state == 'Opt':
                clean_opt = opt_pattern.sub('', text)
                current_q.options.append(clean_opt)
            elif state == 'Ans': current_q.answer += text

    if current_q: questions.append(current_q)
    return questions

def shuffle_options_and_update_answer(question):
    """選項亂數重排與答案修正"""
    if question.type == 'Fill': return question

    original_opts = question.options
    original_ans = question.answer.strip().upper()
    char_to_idx = {chr(65+i): i for i in range(len(original_opts))}
    
    correct_indices = []
    for char in original_ans:
        if char in char_to_idx: correct_indices.append(char_to_idx[char])
            
    correct_contents = [original_opts[i] for i in correct_indices]
    
    shuffled_opts_data = list(enumerate(original_opts))
    random.shuffle(shuffled_opts_data)
    new_options = [data[1] for data in shuffled_opts_data]
    
    new_ans_chars = []
    for content in correct_contents:
        try:
            new_idx = new_options.index(content)
            new_ans_chars.append(chr(65 + new_idx))
        except ValueError: pass
            
    new_ans_chars.sort()
    new_answer_str = "".join(new_ans_chars)

    return Question(
        question.type, question.content, new_options, new_answer_str, 
        question.id, question.image_data, 
        question.source, question.chapter, question.unit
    )

def generate_word_files(selected_questions, shuffle=True):
    """生成 Word 試卷與詳解卷"""
    exam_doc = docx.Document()
    ans_doc = docx.Document()
    
    # 設定字型
    style = exam_doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '標楷體')
    
    exam_doc.add_heading('物理科 試題卷', 0)
    ans_doc.add_heading('物理科 答案卷', 0)
    exam_doc.add_paragraph('班級：__________  姓名：__________  座號：__________\n')
    
    for idx, q in enumerate(selected_questions, 1):
        processed_q = q
        if shuffle and q.type in ['Single', 'Multi']:
            processed_q = shuffle_options_and_update_answer(q)
        
        # --- 試題卷 ---
        p = exam_doc.add_paragraph()
        q_type_text = {'Single': '單選', 'Multi': '多選', 'Fill': '填充'}.get(q.type, '題')
        runner = p.add_run(f"{idx}. ({q_type_text}) {processed_q.content.strip()}")
        runner.bold = True
        
        if processed_q.image_data:
            try:
                img_stream = io.BytesIO(processed_q.image_data)
                exam_doc.add_picture(img_stream, width=Inches(3.0))
            except Exception as e:
                print(f"Error adding picture: {e}")

        if q.type != 'Fill':
            for i, opt in enumerate(processed_q.options):
                exam_doc.add_paragraph(f"({chr(65+i)}) {opt}")
        else:
            exam_doc.add_paragraph("______________________")
        exam_doc.add_paragraph("") 
        
        # --- 答案卷 ---
        ans_p = ans_doc.add_paragraph()
        ans_p.add_run(f"{idx}. ").bold = True
        ans_p.add_run(f"{processed_q.answer}")
        
        meta_info = []
        if processed_q.source and processed_q.source != "一般試題": meta_info.append(processed_q.source)
        if processed_q.unit: meta_info.append(processed_q.unit)
        elif processed_q.chapter: meta_info.append(processed_q.chapter)
            
        if meta_info:
            ans_p.add_run(f"  [{' / '.join(meta_info)}]").italic = True

    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io

# ==========================================
# 4. 初始化 Session State
# ==========================================
if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
if 'imported_candidates' not in st.session_state:
    st.session_state['imported_candidates'] = []

# ==========================================
# 5. Streamlit 主介面
# ==========================================

st.title("🧲 物理題庫自動組卷系統 v3.1 (OCR Pro)")
st.caption("Assistant: 整合 AI 智慧分類、OCR 影像辨識與自動排版")

# --- 側邊欄 ---
with st.sidebar:
    st.header("📦 題庫數據")
    count = len(st.session_state['question_pool'])
    st.metric("目前題庫總數", f"{count} 題")
    
    if count > 0:
        if st.button("🗑️ 清空所有題目", type="primary"):
            st.session_state['question_pool'] = []
            st.rerun()
    
    st.divider()
    st.markdown("### ⚙️ 系統狀態")
    
    # 檢查 OCR 狀態
    if smart_importer.OCR_AVAILABLE:
        st.success("✅ OCR 引擎已就緒")
        st.caption("可處理圖片型 PDF 與掃描檔")
    else:
        st.error("❌ 未偵測到 OCR 引擎")
        st.caption("請確認 packages.txt 是否包含 tesseract-ocr")
        
    st.markdown("""
    ---
    **支援匯入格式：**
    - 智慧匯入：PDF, Word (自動抓題)
    - 標記匯入：Word (需含 [Src] 標籤)
    """)

# --- 頁籤分頁 ---
tab1, tab2, tab3 = st.tabs(["🧠 智慧匯入 (PDF/Word)", "✍️ 手動與標記匯入", "🚀 選題與匯出"])

# === Tab 1: 智慧匯入 (Raw) ===
with tab1:
    st.subheader("原始試卷智慧分析")
    st.markdown("直接上傳未整理的 **PDF** 或 **Word** 試卷，系統將自動識別題目結構。")
    
    col_upload, col_ocr = st.columns([0.7, 0.3])
    with col_upload:
        raw_file = st.file_uploader("上傳試卷 (支援拖與放)", type=['pdf', 'docx'], key="raw_upload")
    
    with col_ocr:
        st.write("") # Spacer
        st.write("")
        use_ocr = st.checkbox("啟用 OCR 強力辨識", 
                            help="若 PDF 為圖片格式或掃描檔，請勾選此項。處理時間較長。",
                            disabled=not smart_importer.OCR_AVAILABLE)
    
    if raw_file:
        if st.button("🔍 開始智慧分析", type="primary"):
            with st.spinner("正在進行深度分析... 若啟用 OCR 可能需要 1-2 分鐘..."):
                file_type = raw_file.name.split('.')[-1].lower()
                candidates = smart_importer.parse_raw_file(raw_file, file_type, use_ocr=use_ocr)
                st.session_state['imported_candidates'] = candidates
                
                if not candidates:
                    msg = "OCR 模式也未找到題目，請確認圖片是否清晰。" if use_ocr else "未偵測到題目結構。若為掃描檔，請勾選「啟用 OCR」再試一次。"
                    st.warning(msg)
                else:
                    st.success(f"成功識別出 {len(candidates)} 題！")

    # 顯示分析結果編輯器
    if st.session_state['imported_candidates']:
        st.divider()
        st.subheader("📋 分析結果審核")
        st.info("請勾選要加入的題目，並可直接修正預測的章節。")
        
        # 準備資料給 DataEditor
        editor_data = []
        for i, cand in enumerate(st.session_state['imported_candidates']):
            editor_data.append({
                "加入": cand.is_physics_likely, # 預設勾選
                "原始題號": cand.number,
                "預測章節": cand.predicted_chapter,
                "題目摘要": cand.content[:40].replace('\n', ' ') + "...",
                "選項數": len(cand.options),
                "系統註記": cand.status_reason
            })
        
        edited_df = st.data_editor(
            pd.DataFrame(editor_data),
            column_config={
                "加入": st.column_config.CheckboxColumn("加入?", width="small"),
                "預測章節": st.column_config.SelectboxColumn("章節分類", options=list(PHYSICS_CHAPTERS.keys()) + ["未分類"], width="medium"),
                "題目摘要": st.column_config.TextColumn("題目摘要", disabled=True),
                "原始題號": st.column_config.NumberColumn("題號", disabled=True),
            },
            use_container_width=True,
            height=400
        )

        col_act1, col_act2 = st.columns([2, 8])
        with col_act1:
            batch_source = st.text_input("設定這批題目的來源", value="考古題匯入")
        
        if st.button("✅ 確認並匯入勾選題目", type="primary"):
            added_count = 0
            indices_to_add = edited_df[edited_df["加入"]].index.tolist()
            
            for idx in indices_to_add:
                cand = st.session_state['imported_candidates'][idx]
                final_chap = edited_df.iloc[idx]["預測章節"]
                
                # 建立正式題目物件
                q_id = len(st.session_state['question_pool']) + 1
                q_obj = Question(
                    q_type="Single" if len(cand.options) > 0 else "Fill",
                    content=cand.content,
                    options=cand.options,
                    answer="", # 原始匯入無答案
                    original_id=cand.number, # 紀錄原始題號方便對照
                    source=batch_source,
                    chapter=final_chap
                )
                st.session_state['question_pool'].append(q_obj)
                added_count += 1
            
            st.balloons()
            st.success(f"已成功匯入 {added_count} 題！請至「選題與匯出」分頁查看。")
            st.session_state['imported_candidates'] = [] # 清空暫存
            st.rerun()

# === Tab 2: 手動與標記匯入 ===
with tab2:
    st.subheader("方式一：手動新增題目")
    with st.expander("展開手動輸入介面"):
        col_cat1, col_cat2, col_cat3 = st.columns(3)
        with col_cat1: new_q_source = st.selectbox("來源", SOURCES)
        with col_cat2: new_q_chap = st.selectbox("章節", list(PHYSICS_CHAPTERS.keys()))
        with col_cat3: new_q_unit = st.selectbox("單元", PHYSICS_CHAPTERS[new_q_chap])

        c1, c2 = st.columns([1, 3])
        with c1: new_q_type = st.selectbox("題型", ["Single", "Multi", "Fill"], format_func=lambda x: {'Single':'單選', 'Multi':'多選', 'Fill':'填充'}[x])
        with c2: new_q_ans = st.text_input("正確答案", placeholder="例如: A")

        new_q_content = st.text_area("題目內容", height=100)
        new_q_image = st.file_uploader("上傳圖片 (選用)", type=['png', 'jpg', 'jpeg'], key="manual_img")
        
        new_q_options = []
        if new_q_type in ["Single", "Multi"]:
            opts_text = st.text_area("選項 (一行一個)", height=100)
            if opts_text: new_q_options = [x.strip() for x in opts_text.split('\n') if x.strip()]

        if st.button("➕ 加入題庫"):
            if new_q_content:
                q_id = len(st.session_state['question_pool']) + 1
                img_bytes = new_q_image.getvalue() if new_q_image else None
                new_q = Question(new_q_type, new_q_content, new_q_options, new_q_ans, q_id, img_bytes, new_q_source, new_q_chap, new_q_unit)
                st.session_state['question_pool'].append(new_q)
                st.success("已加入！")

    st.divider()
    st.subheader("方式二：標記版 Word 匯入")
    st.caption("適用於已人工整理好，含有 `[Src]`, `[Q]`, `[Ans]` 標籤的 Word 檔。")
    uploaded_tagged = st.file_uploader("上傳 .docx", type=['docx'], key="tagged_upload")
    
    if uploaded_tagged and st.button("解析標記檔"):
        try:
            imported = parse_docx_tagged(uploaded_tagged.read())
            st.session_state['question_pool'].extend(imported)
            st.success(f"成功匯入 {len(imported)} 題！")
        except Exception as e:
            st.error(f"解析失敗: {e}")

# === Tab 3: 選題與匯出 ===
with tab3:
    st.subheader("組卷與輸出")
    
    if not st.session_state['question_pool']:
        st.warning("題庫目前是空的，請先匯入題目。")
    else:
        col_ctrl, _ = st.columns([2, 8])
        with col_ctrl:
            select_all = st.checkbox("全選所有題目", value=True)
            do_shuffle = st.checkbox("選項亂數重排", value=True)
        
        st.write("---")
        selected_indices = []
        
        # 題目列表展示
        for i, q in enumerate(st.session_state['question_pool']):
            cols = st.columns([0.5, 9.5])
            with cols[0]:
                if st.checkbox("選", value=select_all, key=f"sel_{i}", label_visibility="collapsed"):
                    selected_indices.append(i)
            with cols[1]:
                with st.expander(f"{i+1}. [{q.source}] {q.chapter} - {q.content[:30]}..."):
                    st.text(q.content)
                    if q.options: st.code("\n".join(q.options))
                    st.caption(f"答案: {q.answer}")
                    if st.button("刪除此題", key=f"del_{i}"):
                        st.session_state['question_pool'].pop(i)
                        st.rerun()

        st.divider()
        st.write(f"已選取 **{len(selected_indices)}** 題")
        
        if st.button("🚀 生成 Word 試卷", type="primary", disabled=not selected_indices):
            final_qs = [st.session_state['question_pool'][i] for i in selected_indices]
            f_exam, f_ans = generate_word_files(final_qs, shuffle=do_shuffle)
            
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("📄 下載試題卷", f_exam, "物理試題.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            with c2:
                st.download_button("🔑 下載詳解卷", f_ans, "物理詳解.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
