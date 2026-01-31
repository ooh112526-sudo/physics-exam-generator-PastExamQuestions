import streamlit as st
import io
import time
import datetime
import requests
import os
import base64
from PIL import Image

# 匯入分離後的模組
try:
    from cloud_service import CloudManager
    from models import Question
    from exporter import generate_word_files
    import smart_importer
except ImportError as e:
    st.error(f"模組匯入失敗: {e}")
    st.stop()

# 更新版本標記
LAST_UPDATED = "2026-02-01 02:00 (CST) [Review & Edit Enhanced]"

# 嘗試載入裁切工具
try:
    from streamlit_cropper import st_cropper 
except: st_cropper = None 

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

TYPE_MAP_ZH_TO_EN = {"單選": "Single", "多選": "Multi", "填充": "Fill", "題組": "Group"}
TYPE_MAP_EN_TO_ZH = {v: k for k, v in TYPE_MAP_ZH_TO_EN.items()}
TYPE_OPTIONS = ["單選", "多選", "填充", "題組"]

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

# [新功能] 校對介面專用 State
if 'review_page' not in st.session_state: st.session_state['review_page'] = 0
if 'review_data_cache' not in st.session_state: st.session_state['review_data_cache'] = None
if 'current_review_file_id' not in st.session_state: st.session_state['current_review_file_id'] = None

# ==========================================
# 核心：批次處理控制器 (Batch Job Runner)
# ==========================================
def run_pending_batch(file_record, api_key):
    """檢查是否有待處理的批次，若有則處理一個並回傳 True (觸發 Rerun)"""
    file_id = file_record['id']
    fname = file_record['filename']
    
    batches = cloud_manager.get_processing_status(file_id)
    if not batches: return False 
    
    pending_batch = next((b for b in batches if b['status'] == 'pending'), None)
    if not pending_batch: return False 
    
    batch_idx = pending_batch['batch_index']
    with st.spinner(f"正在處理 {fname} - 第 {batch_idx + 1} 批次 (共 {len(batches)} 批)..."):
        blob_name = file_record.get('blob_name')
        file_bytes = cloud_manager.download_blob(blob_name)
        
        if not file_bytes:
            cloud_manager.save_batch_result(file_id, batch_idx, None, "error", "檔案下載失敗")
            return True

        all_images, err = smart_importer.convert_file_to_images(file_bytes, file_record.get('exam_type', 'pdf'))
        if not all_images:
            cloud_manager.save_batch_result(file_id, batch_idx, None, "error", err or "轉圖失敗")
            return True

        BATCH_SIZE = smart_importer.BATCH_SIZE
        start_idx = batch_idx * BATCH_SIZE
        end_idx = start_idx + BATCH_SIZE
        batch_imgs = all_images[start_idx:end_idx]
        
        if not batch_imgs:
            cloud_manager.save_batch_result(file_id, batch_idx, [], "done")
            return True

        candidates, error = smart_importer.process_single_batch(batch_imgs, batch_idx, api_key, start_idx)
        
        if error:
            cloud_manager.save_batch_result(file_id, batch_idx, None, "error", error)
        else:
            cloud_manager.save_batch_result(file_id, batch_idx, candidates, "done")
            
    return True 

# ==========================================
# UI 介面
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

with st.sidebar:
    st.header("設定")
    env_api_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_api_key, type="password", key="sidebar_api_key")
    
    if cloud_manager.has_connection:
        st.success("☁️ Cloud: 已連線")
    else:
        st.warning("☁️ Cloud: 未連線")
        
    st.divider()
    if st.button("強制儲存至雲端"):
        for q in st.session_state['question_pool']: cloud_manager.save_question(q.to_dict())
        st.success("已儲存")
    st.caption(f"🚀 Last Updated: {LAST_UPDATED}")

tab_upload, tab_files, tab_review, tab_bank = st.tabs(["🧠 上傳", "📂 檔案與進度", "📝 校對 (分頁版)", "📚 題庫"])

# === Tab 1: 上傳 ===
with tab_upload:
    st.markdown("### 📤 上傳新考古題")
    uploaded_files = st.file_uploader("支援 .pdf, .docx", type=['pdf', 'docx'], accept_multiple_files=True)
    if uploaded_files:
        if st.button("確認上傳"):
            progress = st.progress(0)
            for i, f in enumerate(uploaded_files):
                bytes_data = f.read()
                new_name = f"{int(time.time())}_{f.name}"
                url, blob_name = cloud_manager.upload_bytes(bytes_data, new_name, "raw_uploads", f.type)
                
                cloud_manager.save_file_record({
                    "filename": new_name, "original_filename": f.name,
                    "url": url, "blob_name": blob_name,
                    "ai_status": "未辨識", "created_at": datetime.datetime.now()
                })
                progress.progress((i+1)/len(uploaded_files))
            st.success("上傳完成")
            time.sleep(1)
            st.rerun()

# === Tab 2: 檔案管理 ===
with tab_files:
    st.subheader("檔案庫與 AI 處理進度")
    files = cloud_manager.load_file_records()
    
    for f in files:
        with st.expander(f"📄 {f.get('filename')} ({f.get('ai_status')})"):
            c1, c2, c3 = st.columns([2, 3, 2])
            
            with c1:
                status = f.get('ai_status')
                if status == "未辨識":
                    if st.button("🚀 開始 AI 辨識", key=f"start_{f['id']}"):
                        with st.spinner("準備中..."):
                            f_bytes = cloud_manager.download_blob(f.get('blob_name'))
                            if f_bytes:
                                imgs, _ = smart_importer.convert_file_to_images(f_bytes, 'pdf')
                                if imgs:
                                    total_pages = len(imgs)
                                    total_batches = (total_pages + smart_importer.BATCH_SIZE - 1) // smart_importer.BATCH_SIZE
                                    cloud_manager.init_batch_process(f['id'], total_batches)
                                    st.rerun()
                                else: st.error("轉檔失敗")
                            else: st.error("下載失敗")
                elif status == "處理中":
                    st.info("⚡ AI 正在處理中，請勿關閉視窗...")
                    if run_pending_batch(f, api_key_input):
                        st.rerun()
                    else:
                        batch_stats = cloud_manager.get_processing_status(f['id'])
                        if any(b['status'] == 'error' for b in batch_stats):
                            cloud_manager.update_file_status(f['id'], "部分失敗")
                        else:
                            cloud_manager.update_file_status(f['id'], "已辨識")
                            # [自動引導] 提示使用者前往校對
                            st.toast(f"檔案 {f['filename']} 辨識完成！請切換至「校對」分頁。", icon="✅")
                        st.rerun()

                elif status in ["已辨識", "部分失敗"]:
                    if st.button("重新辨識整份", key=f"reset_{f['id']}"):
                        cloud_manager.update_file_status(f['id'], "未辨識")
                        st.rerun()

            with c2:
                batches = cloud_manager.get_processing_status(f['id'])
                if batches:
                    st.write(f"批次進度: {len([b for b in batches if b['status']=='done'])} / {len(batches)}")
                    for b in batches:
                        if b['status'] == 'error': 
                            st.error(f"Batch {b['batch_index']+1}: 失敗 ({b.get('last_error')})")
                            if st.button("重試此批次", key=f"retry_{f['id']}_{b['batch_index']}"):
                                cloud_manager.reset_batch_status(f['id'], b['batch_index'])
                                cloud_manager.update_file_status(f['id'], "處理中")
                                st.rerun()

            with c3:
                if st.button("🗑️ 刪除檔案", key=f"del_{f['id']}", type="primary"):
                    cloud_manager.delete_file_record(f['id'])
                    st.rerun()

# === Tab 3: AI匯入校對 (分頁 + 完整編輯器) ===
with tab_review:
    st.subheader("📝 匯入校對與編輯")
    
    # 1. 檔案選擇區
    ready_files = [f for f in files if f.get('ai_status') in ["已辨識", "部分失敗"]]
    
    if not ready_files:
        st.info("暫無可校對的檔案，請先至前一分頁進行辨識。")
    else:
        # 簡單的檔案選擇邏輯
        file_names = [f['filename'] for f in ready_files]
        sel_file_name = st.selectbox("選擇要校對的檔案", file_names)
        sel_file = next(f for f in ready_files if f['filename'] == sel_file_name)
        
        # 2. 資料載入與快取 (避免每次切換分頁都重讀 DB)
        if st.session_state['current_review_file_id'] != sel_file['id']:
            with st.spinner("載入題目資料中..."):
                results_data = cloud_manager.load_all_ai_results(sel_file['id'])
                # 為每個結果加上唯一 key 以便 Streamlit 追蹤
                for idx, res in enumerate(results_data):
                    if 'ui_key' not in res: res['ui_key'] = str(uuid.uuid4())
                
                st.session_state['review_data_cache'] = results_data
                st.session_state['current_review_file_id'] = sel_file['id']
                st.session_state['review_page'] = 0 # 重置頁碼
        
        all_results = st.session_state['review_data_cache']
        
        if not all_results:
            st.warning("此檔案沒有資料。")
        else:
            # 3. 分頁控制器
            ITEMS_PER_PAGE = 5
            total_pages = (len(all_results) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            
            c_page_info, c_prev, c_next = st.columns([6, 1, 1])
            with c_page_info:
                st.caption(f"共 {len(all_results)} 題，目前顯示第 {st.session_state['review_page'] + 1} / {total_pages} 頁")
            with c_prev:
                if st.button("◀ 上一頁", disabled=(st.session_state['review_page'] == 0)):
                    st.session_state['review_page'] -= 1
                    st.rerun()
            with c_next:
                if st.button("下一頁 ▶", disabled=(st.session_state['review_page'] >= total_pages - 1)):
                    st.session_state['review_page'] += 1
                    st.rerun()
            
            st.divider()

            # 4. 顯示當前頁面的題目
            start_idx = st.session_state['review_page'] * ITEMS_PER_PAGE
            end_idx = start_idx + ITEMS_PER_PAGE
            current_page_items = all_results[start_idx:end_idx]
            
            # 暫存修改的容器 (表單外使用 session state 或直接修改 cache)
            # 這裡我們直接修改 session_state['review_data_cache'] 中的物件
            
            for i, res in enumerate(current_page_items):
                real_idx = start_idx + i
                
                with st.container():
                    col_edit, col_img = st.columns([1, 1])
                    
                    # --- 左側：編輯區 ---
                    with col_edit:
                        st.markdown(f"#### 第 {res.get('number', real_idx+1)} 題")
                        
                        # 題型
                        curr_type_zh = TYPE_MAP_EN_TO_ZH.get(res.get('type'), "單選")
                        new_type_zh = st.selectbox(f"題型 (Idx: {real_idx})", TYPE_OPTIONS, index=TYPE_OPTIONS.index(curr_type_zh) if curr_type_zh in TYPE_OPTIONS else 0, key=f"type_{res['ui_key']}")
                        res['type'] = TYPE_MAP_ZH_TO_EN[new_type_zh]

                        # 內容
                        res['content'] = st.text_area(f"題目內容", res.get('content', ''), height=120, key=f"content_{res['ui_key']}")
                        
                        # 選項 (非題組時顯示)
                        if res['type'] != "Group":
                            opts_str = "\n".join(res.get('options', []))
                            new_opts_str = st.text_area("選項 (每行一個)", opts_str, height=100, key=f"opts_{res['ui_key']}")
                            res['options'] = new_opts_str.split('\n') if new_opts_str.strip() else []
                            
                            res['answer'] = st.text_input("答案", res.get('answer', ''), key=f"ans_{res['ui_key']}")
                        
                        # 章節
                        curr_chap = res.get('chapter', '未分類')
                        if curr_chap not in smart_importer.PHYSICS_CHAPTERS_LIST: curr_chap = '未分類'
                        res['chapter'] = st.selectbox("章節", smart_importer.PHYSICS_CHAPTERS_LIST, index=smart_importer.PHYSICS_CHAPTERS_LIST.index(curr_chap), key=f"chap_{res['ui_key']}")

                        # 題組子題編輯區
                        if res['type'] == "Group" and 'sub_questions' in res:
                            with st.expander("編輯子題目", expanded=True):
                                for sub_idx, sub_q in enumerate(res['sub_questions']):
                                    st.caption(f"子題 {sub_q.get('number')}")
                                    sub_q['content'] = st.text_area(f"子題內容", sub_q.get('content',''), key=f"sub_c_{res['ui_key']}_{sub_idx}")
                                    sub_q['answer'] = st.text_input(f"子題答案", sub_q.get('answer',''), key=f"sub_a_{res['ui_key']}_{sub_idx}")

                    # --- 右側：圖片與裁切區 ---
                    with col_img:
                        st.markdown("🖼️ **圖片校對**")
                        
                        # 圖片來源選擇
                        img_mode = st.radio("圖片模式", ["AI 預截圖", "整頁手動裁切"], horizontal=True, key=f"img_mode_{res['ui_key']}")
                        
                        if img_mode == "AI 預截圖":
                            # 優先顯示 ref_image (AI 建議範圍)，沒有則顯示 image_b64 (僅附圖)
                            target_b64 = res.get('ref_image_b64') or res.get('image_b64')
                            if target_b64:
                                st.image(base64.b64decode(target_b64), caption="AI 自動截取範圍")
                            else:
                                st.info("此題無 AI 截圖，請切換至「整頁手動裁切」")
                                
                        else: # 手動裁切模式
                            full_page = res.get('full_page_b64')
                            if full_page and st_cropper:
                                st.caption("請拖曳紅框選擇題目範圍，確認後系統將使用此範圍作為題目圖片。")
                                cropped_img = st_cropper(
                                    Image.open(io.BytesIO(base64.b64decode(full_page))),
                                    realtime_update=True,
                                    box_color='#FF0000',
                                    key=f"crop_{res['ui_key']}"
                                )
                                # 這裡僅做即時預覽，實際儲存需按下方按鈕 (Streamlit 限制)
                                if cropped_img:
                                    st.image(cropped_img, caption="裁切預覽", width=150)
                                    # 將裁切結果寫回物件 (注意：這會即時更新 cache)
                                    img_byte_arr = io.BytesIO()
                                    cropped_img.save(img_byte_arr, format='PNG')
                                    res['image_b64'] = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                                    # 清除 ref_image 以確保優先使用手動裁切結果
                                    res['ref_image_b64'] = None
                            elif not full_page:
                                st.warning("無法載入整頁原始圖 (可能未開啟 full_page 儲存功能)")
                            else:
                                st.error("Cropper 元件未載入")

                st.divider()

            # 5. 底部操作區
            c_save_page, c_import_all = st.columns([2, 2])
            with c_save_page:
                if st.button("💾 儲存本頁修改 (暫存)", use_container_width=True):
                    # 因為我們直接操作 st.session_state['review_data_cache']，
                    # 這裡其實不需要做什麼，只要 rerun 刷新畫面即可確認
                    st.success("已更新暫存資料！")
            
            with c_import_all:
                if st.button("🚀 確認無誤，正式匯入題庫", type="primary", use_container_width=True):
                    with st.spinner("正在寫入資料庫並上傳圖片..."):
                        count = 0
                        total = len(all_results)
                        progress_bar = st.progress(0)
                        
                        for idx, item in enumerate(all_results):
                            # 判斷要使用哪張圖 (手動裁切 > AI Ref > AI Crop)
                            final_img_b64 = item.get('image_b64') or item.get('ref_image_b64')
                            img_data = base64.b64decode(final_img_b64) if final_img_b64 else None
                            
                            # 建構 Question 物件
                            q = Question(
                                q_type=item.get('type'),
                                content=item.get('content'),
                                options=item.get('options', []),
                                answer=item.get('answer', ''),
                                chapter=item.get('chapter', '未分類'),
                                source=sel_file['filename'], # 來源標籤
                                image_data=img_data,
                                sub_questions=[Question.from_dict(sq) for sq in item.get('sub_questions', [])] if item.get('sub_questions') else []
                            )
                            cloud_manager.save_question(q.to_dict())
                            count += 1
                            progress_bar.progress((idx + 1) / total)
                        
                        # 標記檔案為已匯入
                        cloud_manager.update_file_status(sel_file['id'], "已匯入")
                        
                        # [重要] 清除暫存
                        del st.session_state['review_data_cache']
                        st.session_state['current_review_file_id'] = None
                        
                        st.success(f"成功匯入 {count} 題！")
                        time.sleep(2)
                        st.rerun()

# === Tab 4: 題庫 (維持不變) ===
with tab_bank:
    st.subheader("題庫總覽")
    if not st.session_state['question_pool']:
        # 嘗試重新載入
        qs = cloud_manager.load_questions()
        if qs: st.session_state['question_pool'] = [Question.from_dict(d) for d in qs]
    
    st.write(f"目前題庫共有 {len(st.session_state['question_pool'])} 題")
    if st.button("重新整理題庫列表"):
        qs = cloud_manager.load_questions()
        st.session_state['question_pool'] = [Question.from_dict(d) for d in qs]
        st.rerun()
