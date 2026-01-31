import streamlit as st
import io
import time
import datetime
import requests
import os  # <--- 確保這一行必須存在
from PIL import Image

# 匯入分離後的模組
# 請確保目錄下有 cloud_service.py, models.py, exporter.py, smart_importer.py
try:
    from cloud_service import CloudManager
    from models import Question
    from exporter import generate_word_files
    import smart_importer
except ImportError as e:
    st.error(f"模組匯入失敗，請確認所有 .py 檔案皆已上傳至正確目錄: {e}")
    st.stop()

# ==========================================
# 版本控制標籤
# ==========================================
LAST_UPDATED = "2026-02-01 00:50 (CST)"

# 安全載入 streamlit_cropper
try:
    from streamlit_cropper import st_cropper 
except ImportError:
    st_cropper = None 
except Exception:
    st_cropper = None

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

# 題型對照表 (UI 使用)
TYPE_MAP_ZH_TO_EN = {"單選": "Single", "多選": "Multi", "填充": "Fill", "題組": "Group"}
TYPE_MAP_EN_TO_ZH = {v: k for k, v in TYPE_MAP_ZH_TO_EN.items()}
TYPE_OPTIONS = ["單選", "多選", "填充", "題組"]

# 初始化 Cloud Manager
cloud_manager = CloudManager()

# 初始化 Session State
if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
    try:
        data = cloud_manager.load_questions()
        if data: st.session_state['question_pool'] = [Question.from_dict(d) for d in data]
    except: pass

if 'file_queue' not in st.session_state: st.session_state['file_queue'] = {}
if 'upload_configs' not in st.session_state: st.session_state['upload_configs'] = {}

# 輔助函式：處理單一檔案辨識 (Bridge Logic)
def process_single_file(filename, api_key, file_id_in_db=None):
    if filename not in st.session_state['file_queue']: return
    info = st.session_state['file_queue'][filename]
    info['status'] = 'processing'
    
    with st.spinner(f"正在分析 {filename}... (AI 思考中，請稍候)"):
        # 下載檔案資料
        file_bytes = info.get('data')
        blob_name = info.get('blob_name')
        
        if not file_bytes and blob_name:
            file_bytes = cloud_manager.download_blob(blob_name)
            if file_bytes:
                st.session_state['file_queue'][filename]['data'] = file_bytes
        
        if not file_bytes:
            st.error("無法讀取檔案內容，請確認檔案是否已上傳。")
            info['status'] = 'error'
            return

        res = smart_importer.parse_with_gemini(file_bytes, info['type'], api_key)
    
    if isinstance(res, dict) and "error" in res:
        info['status'] = 'error'
        info['error_msg'] = res['error']
        st.error(f"{filename} 辨識失敗: {res['error']}")
    else:
        info['status'] = 'done'
        info['result'] = res
        if file_id_in_db:
            cloud_manager.update_file_status(file_id_in_db, "已辨識")
        
        st.success(f"{filename} 辨識完成！")
        st.session_state['just_processed_file'] = filename
        st.info("💡 請切換至「📝 AI匯入校對」分頁開始編輯")
        
    st.rerun()

# ==========================================
# UI 介面開始
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

# --- Sidebar ---
with st.sidebar:
    st.header("設定")
    # 這裡就是發生錯誤的地方，必須確保 os 已匯入
    env_api_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_api_key, type="password", key="sidebar_api_key")
    
    if cloud_manager.has_connection:
        st.success("☁️ Cloud: 已連線")
        if cloud_manager.bucket_name:
            st.caption(f"Bucket: {cloud_manager.bucket_name}")
    else:
        st.warning(f"☁️ Cloud: 未連線")
        if cloud_manager.connection_error:
            st.caption(f"錯誤: {cloud_manager.connection_error}")
            if "No secrets found" in cloud_manager.connection_error:
                st.info("Secrets 未設定，請改用環境變數 GCP_SERVICE_ACCOUNT_JSON")

    st.divider()
    st.metric("題庫總數", len(st.session_state['question_pool']))
    
    if cloud_manager.has_connection:
        st.divider()
        try:
            total_bytes = cloud_manager.get_storage_usage()
            total_mb = total_bytes / (1024 * 1024)
            percentage = min(total_mb / 1024.0, 1.0)
            
            st.write("📊 **雲端儲存空間**")
            st.progress(percentage)
            st.caption(f"已使用: {total_mb:.2f} MB / 1 GB")
            if percentage > 0.9: st.warning("⚠️ 容量即將額滿！")
        except: st.caption("無法取得容量資訊")

    if st.button("強制儲存至雲端", key="sidebar_force_save"):
        if cloud_manager.has_connection:
            progress_bar = st.progress(0)
            total = len(st.session_state['question_pool'])
            for i, q in enumerate(st.session_state['question_pool']):
                cloud_manager.save_question(q.to_dict())
                progress_bar.progress((i + 1) / total)
            st.success("儲存完成！")

    # === [新功能] 版本時間顯示 ===
    st.divider()
    st.caption(f"🚀 Last Updated: {LAST_UPDATED}")

# Tabs
tab_upload_process, tab_files, tab_review, tab_bank = st.tabs(["🧠 考古題上傳", "📂 檔案管理及AI辨識", "📝 AI匯入校對", "📚 題庫管理與試卷輸出"])

# === Tab 1: 考古題上傳 ===
with tab_upload_process:
    st.markdown("### 📤 上傳新考古題")
    st.info("請先選擇檔案，設定各自的標籤後，系統將自動重新命名並上傳。")
    uploaded_files = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'], accept_multiple_files=True)
    
    if uploaded_files:
        st.divider()
        st.subheader("設定檔案資訊")
        
        with st.expander("批次設定 (一次套用給下方所有檔案)"):
            c1, c2, c3, c4 = st.columns(4)
            with c1: b_type = st.selectbox("統一類型", ["學測", "分科", "北模", "中模", "全模", "其他"], key="batch_type")
            with c2: b_year = st.text_input("統一年度", value="112", key="batch_year")
            with c3: b_exam_no = st.selectbox("統一考試次別", ["第一次", "第二次", "第三次", "正式考試"], key="batch_no")
            with c4: 
                if st.button("全部套用"):
                    for uf in uploaded_files:
                        st.session_state['upload_configs'][uf.name] = {"type": b_type, "year": b_year, "exam_no": b_exam_no}
                    st.success("已套用！")

        files_to_upload = []
        for i, f in enumerate(uploaded_files):
            current_config = st.session_state['upload_configs'].get(f.name, {"type": "學測", "year": "112", "exam_no": "正式考試"})
            
            with st.container():
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                with c1: 
                    st.markdown(f"**{i+1}. {f.name}**")
                    ext = f.name.split('.')[-1]
                    new_name = f"{current_config['year']}-{current_config['type']}-{current_config['exam_no']}.{ext}"
                    st.caption(f"➝ `{new_name}`")
                
                with c2: 
                    new_type = st.selectbox("類型", ["學測", "分科", "北模", "中模", "全模", "其他"], 
                                          index=["學測", "分科", "北模", "中模", "全模", "其他"].index(current_config['type']),
                                          key=f"type_{f.name}")
                with c3: new_year = st.text_input("年度", value=current_config['year'], key=f"year_{f.name}")
                with c4: 
                    new_no = st.selectbox("次別", ["第一次", "第二次", "第三次", "正式考試"], 
                                        index=["第一次", "第二次", "第三次", "正式考試"].index(current_config['exam_no']),
                                        key=f"no_{f.name}")
                
                st.session_state['upload_configs'][f.name] = {"type": new_type, "year": new_year, "exam_no": new_no}
                final_new_name = f"{new_year}-{new_type}-{new_no}.{f.name.split('.')[-1]}"
                files_to_upload.append({"file_obj": f, "new_filename": final_new_name, "type": new_type, "year": new_year, "exam_no": new_no})
            st.divider()

        if st.button("確認並上傳所有檔案", type="primary"):
            duplicate_warnings = []
            for item in files_to_upload:
                if cloud_manager.check_file_exists(item['new_filename']):
                    duplicate_warnings.append(f"{item['new_filename']} (原: {item['file_obj'].name})")
            
            if duplicate_warnings:
                st.error(f"發現雲端已有重複檔名，請修改年度或次別：\n" + "\n".join(duplicate_warnings))
            else:
                progress_bar = st.progress(0)
                success_count = 0
                for idx, item in enumerate(files_to_upload):
                    f = item['file_obj']
                    f.seek(0)
                    file_bytes = f.read()
                    
                    backup_url, blob_name = cloud_manager.upload_bytes(file_bytes, item['new_filename'], folder="raw_uploads", content_type=f.type)
                    
                    file_record = {
                        "filename": item['new_filename'],
                        "original_filename": f.name,
                        "url": backup_url,
                        "blob_name": blob_name,
                        "exam_type": item['type'],
                        "year": item['year'],
                        "exam_no": item['exam_no'],
                        "ai_status": "未辨識",
                        "created_at": datetime.datetime.now()
                    }
                    cloud_manager.save_file_record(file_record)
                    
                    st.session_state['file_queue'][item['new_filename']] = {
                        "status": "uploaded", "data": file_bytes, "type": f.type.split('/')[-1] if '/' in f.type else 'pdf',
                        "result": [], "error_msg": "", "source_tag": f"{item['type']}-{item['year']}",
                        "backup_url": backup_url, "blob_name": blob_name, "db_id": file_record['id'] 
                    }
                    success_count += 1
                    progress_bar.progress((idx + 1) / len(files_to_upload))
                
                if success_count > 0:
                    st.success(f"成功上傳 {success_count} 個檔案！")
                    st.session_state['upload_configs'] = {}
                    time.sleep(1)
                    st.rerun()

# === Tab 2: 檔案管理及AI辨識 ===
with tab_files:
    if 'just_processed_file' in st.session_state:
        st.success(f"🎉 **{st.session_state['just_processed_file']}** 辨識完成！")
        st.info("👉 請點選上方 **「📝 AI匯入校對」** 分頁進行檢查。")
        del st.session_state['just_processed_file']

    st.subheader("已上傳考古題檔案庫")
    cloud_files = cloud_manager.load_file_records()
    
    if not cloud_files:
        st.info("目前沒有已上傳的檔案記錄。")
    else:
        files_tree = {}
        for f in cloud_files:
            ftype, fyear = f.get('exam_type', '未分類'), f.get('year', '未知年份')
            if ftype not in files_tree: files_tree[ftype] = {}
            if fyear not in files_tree[ftype]: files_tree[ftype][fyear] = []
            files_tree[ftype][fyear].append(f)

        for ftype in sorted(files_tree.keys()):
            with st.expander(f"📁 {ftype}", expanded=False):
                for fyear in sorted(files_tree[ftype].keys(), key=lambda y: -int(y) if y.isdigit() else 0):
                    with st.expander(f"📁 {fyear} 年度", expanded=False):
                        sorted_files = sorted(files_tree[ftype][fyear], key=lambda x: {"第一次":1, "第二次":2, "第三次":3, "正式考試":4}.get(x.get('exam_no'), 99))
                        for f_record in sorted_files:
                            c1, c2, c3 = st.columns([5, 2, 3], vertical_alignment="center")
                            with c1: st.write(f"📄 {f_record.get('filename')}")
                            with c2: st.button("✅ 已辨識" if f_record.get('ai_status') == '已辨識' else "⬜ 未辨識", key=f"s_{f_record['id']}", disabled=True, use_container_width=True)
                            with c3:
                                b1, b2 = st.columns(2)
                                with b1:
                                    if st.button("重新辨識" if f_record.get('ai_status') == '已辨識' else "AI 辨識", key=f"ai_{f_record['id']}", use_container_width=True):
                                        fname = f_record['filename']
                                        loaded = False
                                        if fname in st.session_state['file_queue']: loaded = True
                                        else:
                                            f_bytes = cloud_manager.download_blob(f_record.get('blob_name')) or requests.get(f_record.get('url', '')).content
                                            if f_bytes:
                                                st.session_state['file_queue'][fname] = {
                                                    "status": "uploaded", "data": f_bytes, "type": fname.split('.')[-1].lower(),
                                                    "result": [], "error_msg": "", "source_tag": f"{ftype}-{fyear}",
                                                    "backup_url": f_record.get('url'), "blob_name": f_record.get('blob_name'), "db_id": f_record['id']
                                                }
                                                loaded = True
                                        if loaded: process_single_file(fname, api_key_input, f_record['id'])
                                        else: st.error("檔案讀取失敗")
                                with b2:
                                    if st.button("🗑️", key=f"d_{f_record['id']}", type="primary", use_container_width=True):
                                        cloud_manager.delete_file_record(f_record['id'])
                                        st.rerun()

# === Tab 3: AI匯入校對 ===
with tab_review:
    st.subheader("匯入校對與截圖")
    ready_files = [f for f, info in st.session_state['file_queue'].items() if info['status'] == 'done']
    
    if not ready_files:
        st.warning("沒有已完成辨識的檔案。請先至「檔案管理及AI辨識」點擊辨識。")
    else:
        default_idx = ready_files.index(st.session_state['just_processed_file']) if 'just_processed_file' in st.session_state and st.session_state['just_processed_file'] in ready_files else 0
        selected_file = st.selectbox("選擇要處理的檔案", ready_files, index=default_idx)
        file_info = st.session_state['file_queue'][selected_file]
        candidates = file_info['result']
        
        col1, col2 = st.columns(2)
        with col1: source_tag = st.text_input("設定此批試卷來源標籤", value=file_info.get("source_tag", "未分類"))
        
        st.divider()
        with st.form(key=f"edit_{selected_file}"):
            for i, cand in enumerate(candidates):
                st.markdown(f"**第 {cand.number} 題**")
                if cand.q_type == "Group": st.info("📖 題組共用敘述")
                
                c1, c2 = st.columns([1, 1])
                with c1:
                    cand.content = st.text_area(f"題目 #{i}", cand.content, height=100, key=f"{selected_file}_c_{i}")
                    if cand.q_type != "Group":
                        opts_text = "\n".join(cand.options)
                        new_opts = st.text_area(f"選項 #{i}", opts_text, height=80, key=f"{selected_file}_o_{i}")
                        cand.options = new_opts.split('\n') if new_opts else []
                    
                    new_type_zh = st.selectbox(f"題型 #{i}", TYPE_OPTIONS, index=TYPE_OPTIONS.index(TYPE_MAP_EN_TO_ZH.get(cand.q_type, "單選")), key=f"{selected_file}_t_{i}")
                    cand.q_type = TYPE_MAP_ZH_TO_EN[new_type_zh]

                    if cand.q_type == "Group" and cand.sub_questions:
                        with st.expander("編輯子題目"):
                            for sub_q in cand.sub_questions:
                                st.text_area(f"子題 {sub_q.get('number')} 內容", sub_q.get('content', ''), key=f"sub_{selected_file}_{i}_{sub_q.get('number')}")

                    st.text_input(f"答案 #{i}", key=f"{selected_file}_ans_{i}")
                    cand.predicted_chapter = st.selectbox(f"章節 #{i}", smart_importer.PHYSICS_CHAPTERS_LIST, index=smart_importer.PHYSICS_CHAPTERS_LIST.index(cand.predicted_chapter) if cand.predicted_chapter in smart_importer.PHYSICS_CHAPTERS_LIST else 0, key=f"{selected_file}_ch_{i}")
                    if cand.image_bytes: st.image(cand.image_bytes, caption="目前附圖", width=200)

                with c2:
                    st.markdown("✂️ **截圖工具**")
                    image_to_crop = cand.ref_image_bytes if cand.ref_image_bytes else cand.full_page_bytes
                    if image_to_crop and st_cropper:
                        st_cropper(Image.open(io.BytesIO(image_to_crop)), realtime_update=True, box_color='#FF0000', key=f"crop_{selected_file}_{i}")
                    elif image_to_crop:
                        st.image(image_to_crop, caption="預覽 (Cropper 未載入)")
                    else:
                        st.info("無參考圖片")
                st.divider()
            st.form_submit_button("💾 暫存修改")

        if st.button(f"✅ 確認匯入 [{selected_file}] 至雲端", type="primary"):
            progress = st.progress(0)
            count = 0
            for i, cand in enumerate(candidates):
                new_q = Question(
                    q_type=cand.q_type, content=cand.content, options=cand.options,
                    source=source_tag, chapter=cand.predicted_chapter, image_data=cand.image_bytes,
                    answer=st.session_state.get(f"{selected_file}_ans_{i}", ""),
                    source_file_id=file_info.get("db_id"),
                    sub_questions=[Question.from_dict(sq) for sq in cand.sub_questions] if cand.sub_questions else []
                )
                cloud_manager.save_question(new_q.to_dict())
                st.session_state['question_pool'].append(new_q)
                count += 1
                progress.progress((i + 1) / len(candidates))
            st.success(f"成功匯入 {count} 題！")
            st.session_state['file_queue'][selected_file]['status'] = 'imported'
            if file_info.get("db_id"): cloud_manager.update_file_status(file_info["db_id"], "已匯入")
            st.rerun()

# === Tab 4: 題庫管理與試卷輸出 ===
with tab_bank:
    st.subheader("題庫總覽與試卷輸出")
    if not st.session_state['question_pool']:
        st.info("目前沒有題目。")
    else:
        all_sources = sorted(list(set([q.source for q in st.session_state['question_pool']])))
        export_qs = []
        for src in all_sources:
            qs_in_src = [q for q in st.session_state['question_pool'] if q.source == src]
            with st.expander(f"📁 {src} ({len(qs_in_src)} 題)"):
                if st.checkbox(f"全選 [{src}]", key=f"src_{src}"): export_qs.extend(qs_in_src)
                for q in qs_in_src:
                    if q.parent_id: continue
                    st.markdown(f"**【{TYPE_MAP_EN_TO_ZH.get(q.type, q.type)}】 {q.content[:30]}...**")
                    if st.button("🗑️", key=f"del_{q.id}"):
                        cloud_manager.delete_question(q.id)
                        st.rerun()
                    st.divider()

        st.divider()
        if st.button(f"生成 Word 試卷 ({len(export_qs)} 題)"):
            f1, f2 = generate_word_files(export_qs)
            st.download_button("下載試題卷", f1, "exam.docx")
            st.download_button("下載答案卷", f2, "ans.docx")
