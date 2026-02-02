import streamlit as st
import io
import time
import datetime
import requests
import os
import base64
import uuid
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

# 標記修復版本
LAST_UPDATED = "2026-02-02 00:50 (CST) [CRITICAL FIX: File Type Detection]"

try:
    from streamlit_cropper import st_cropper 
except: st_cropper = None 

st.set_page_config(page_title="物理題庫系統 (Pro)", layout="wide", page_icon="🧲")

TYPE_MAP_ZH_TO_EN = {"單選": "Single", "多選": "Multi", "填充": "Fill", "題組": "Group"}
TYPE_MAP_EN_TO_ZH = {v: k for k, v in TYPE_MAP_ZH_TO_EN.items()}
TYPE_OPTIONS = ["單選", "多選", "填充", "題組"]

cloud_manager = CloudManager()

if 'question_pool' not in st.session_state:
    st.session_state['question_pool'] = []
    try:
        data = cloud_manager.load_questions()
        if data: st.session_state['question_pool'] = [Question.from_dict(d) for d in data]
    except: pass

if 'file_queue' not in st.session_state: st.session_state['file_queue'] = {}
if 'upload_configs' not in st.session_state: st.session_state['upload_configs'] = {}
if 'review_page' not in st.session_state: st.session_state['review_page'] = 0
if 'review_data_cache' not in st.session_state: st.session_state['review_data_cache'] = None
if 'current_review_file_id' not in st.session_state: st.session_state['current_review_file_id'] = None
if 'selected_export_ids' not in st.session_state: st.session_state['selected_export_ids'] = set()

def ensure_b64(item, key_prefix):
    b64_key = f"{key_prefix}_b64"
    url_key = f"{key_prefix}_url"
    blob_key = f"{key_prefix}_blob_name"
    if item.get(b64_key): return item[b64_key]
    if item.get(blob_key):
        b_data = cloud_manager.download_blob(item[blob_key])
        if b_data:
            b64 = base64.b64encode(b_data).decode('utf-8')
            item[b64_key] = b64
            return b64
    if item.get(url_key):
        try:
            resp = requests.get(item[url_key], timeout=5)
            if resp.status_code == 200:
                b64 = base64.b64encode(resp.content).decode('utf-8')
                item[b64_key] = b64
                return b64
        except: pass
    return None

# ==========================================
# Batch Job Runner (AI 處理核心 - 修復版)
# ==========================================
def run_pending_batch(file_record, api_key):
    file_id, fname = file_record['id'], file_record['filename']
    batches = cloud_manager.get_processing_status(file_id)
    if not batches: return False 
    
    # 找出第一個 pending 的批次
    pending_batch = next((b for b in batches if b['status'] == 'pending'), None)
    if not pending_batch: return False 
    
    batch_idx = pending_batch['batch_index']
    
    # 計算頁碼範圍
    BATCH_SIZE = smart_importer.BATCH_SIZE
    start_page = (batch_idx * BATCH_SIZE) + 1
    end_page = start_page + BATCH_SIZE - 1
    
    with st.spinner(f"正在處理 {fname} - 第 {batch_idx + 1} 批次 (頁數 {start_page}~{end_page})..."):
        try:
            blob_name = file_record.get('blob_name')
            file_bytes = cloud_manager.download_blob(blob_name)
            if not file_bytes:
                cloud_manager.save_batch_result(file_id, batch_idx, None, "error", "檔案下載失敗")
                return True

            # [BUG FIX] 自動偵測副檔名，而不是使用 exam_type (如: '中模')
            ftype = 'pdf'
            if fname.lower().endswith('.docx'):
                ftype = 'docx'

            # 切換頁面
            batch_imgs, err = smart_importer.convert_file_to_images(
                file_bytes, 
                ftype, # 修正這裡：傳入正確的 'pdf' 或 'docx'
                first_page=start_page, 
                last_page=end_page
            )
            
            if not batch_imgs:
                # 可能是頁數超過範圍(正常結束)或是真的失敗
                status = "done" if not err else "error"
                cloud_manager.save_batch_result(file_id, batch_idx, [], status, err or "無圖片")
                return True

            candidates, error = smart_importer.process_single_batch(batch_imgs, batch_idx, api_key, start_page)
            if error:
                cloud_manager.save_batch_result(file_id, batch_idx, None, "error", error)
            else:
                cloud_manager.save_batch_result(file_id, batch_idx, candidates, "done")
        except Exception as e:
            cloud_manager.save_batch_result(file_id, batch_idx, None, "error", f"System Error: {str(e)}")
            
    return True 

# ==========================================
# UI 介面
# ==========================================
st.title("🧲 物理題庫系統 Pro (Cloud Storage)")

with st.sidebar:
    st.header("設定")
    env_api_key = os.getenv("GOOGLE_API_KEY", "")
    api_key_input = st.text_input("Gemini API Key", value=env_api_key, type="password", key="sidebar_api_key")
    if cloud_manager.has_connection: st.success("☁️ Cloud: 已連線")
    else: st.warning("☁️ Cloud: 未連線")
    st.divider()
    if st.button("強制儲存至雲端"):
        for q in st.session_state['question_pool']: cloud_manager.save_question(q.to_dict())
        st.success("已儲存")
    st.caption(f"🚀 Last Updated: {LAST_UPDATED}")

tab_upload, tab_files, tab_review, tab_bank = st.tabs(["🧠 考古題上傳", "📂 檔案管理與辨識", "📝 AI匯入校對", "📚 題庫管理與輸出"])

# === Tab 1: 考古題上傳 ===
with tab_upload:
    st.markdown("### 📤 上傳新考古題")
    uploaded_files = st.file_uploader("選擇 PDF 或 Word 檔案", type=['pdf', 'docx'], accept_multiple_files=True)
    if uploaded_files:
        st.divider()
        st.subheader("設定檔案資訊")
        for f in uploaded_files:
            if f.name not in st.session_state['upload_configs']:
                st.session_state['upload_configs'][f.name] = {"type": "學測", "year": "112", "exam_no": "正式考試"}

        with st.expander("⚡ 批次設定"):
            c1, c2, c3, c4 = st.columns(4)
            with c1: b_type = st.selectbox("統一類型", ["學測", "分科", "北模", "中模", "全模", "其他"], key="batch_type")
            with c2: b_year = st.text_input("統一年度", value="112", key="batch_year")
            with c3: b_exam_no = st.selectbox("統一考試次別", ["第一次", "第二次", "第三次", "正式考試"], key="batch_no")
            with c4: 
                if st.button("全部套用"):
                    for f in uploaded_files:
                        st.session_state['upload_configs'][f.name] = {"type": b_type, "year": b_year, "exam_no": b_exam_no}
                    st.toast("已套用！")

        files_to_process = []
        has_dup = False
        st.markdown("---")
        for i, f in enumerate(uploaded_files):
            config = st.session_state['upload_configs'][f.name]
            ext = f.name.split('.')[-1]
            new_fname = f"{config['year']}-{config['type']}-{config['exam_no']}.{ext}"
            dup_rec = cloud_manager.check_file_exists(new_fname)
            if dup_rec: has_dup = True
            
            with st.container():
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                with c1: 
                    st.markdown(f"**{i+1}. {f.name}**")
                    if dup_rec: st.error(f"⚠️ 覆蓋: `{new_fname}`")
                    else: st.caption(f"➝ `{new_fname}`")
                with c2: 
                    new_type = st.selectbox("類型", ["學測", "分科", "北模", "中模", "全模", "其他"], index=["學測", "分科", "北模", "中模", "全模", "其他"].index(config['type']), key=f"t_{f.name}")
                    st.session_state['upload_configs'][f.name]['type'] = new_type
                with c3: 
                    new_year = st.text_input("年度", value=config['year'], key=f"y_{f.name}")
                    st.session_state['upload_configs'][f.name]['year'] = new_year
                with c4: 
                    new_no = st.selectbox("次別", ["第一次", "第二次", "第三次", "正式考試"], index=["第一次", "第二次", "第三次", "正式考試"].index(config['exam_no']), key=f"n_{f.name}")
                    st.session_state['upload_configs'][f.name]['exam_no'] = new_no
            files_to_process.append({"file": f, "name": new_fname, "config": st.session_state['upload_configs'][f.name]})
            st.divider()

        btn_txt = "確認並覆蓋上傳" if has_dup else "確認上傳"
        if st.button(btn_txt, type="primary"):
            prog = st.progress(0)
            for idx, item in enumerate(files_to_process):
                f_obj = item['file']
                f_obj.seek(0)
                url, blob_name = cloud_manager.upload_bytes(f_obj.read(), item['name'], "raw_uploads", f_obj.type)
                old = cloud_manager.check_file_exists(item['name'])
                rec = {
                    "filename": item['name'], "original_filename": f_obj.name,
                    "url": url, "blob_name": blob_name,
                    "exam_type": item['config']['type'], "year": item['config']['year'], "exam_no": item['config']['exam_no'],
                    "ai_status": "未辨識", "created_at": datetime.datetime.now()
                }
                cloud_manager.save_file_record(rec, overwrite_id=old['id'] if old else None)
                prog.progress((idx+1)/len(files_to_process))
            st.success("上傳完成！"); st.session_state['upload_configs'] = {}; time.sleep(1); st.rerun()

# === Tab 2: 檔案管理 (修復: 卡死與單獨重試) ===
with tab_files:
    st.subheader("📂 檔案庫與 AI 處理狀態")
    files = cloud_manager.load_file_records()
    if not files: st.info("目前沒有檔案。")
    else:
        tree = {}
        for f in files:
            ftype = f.get('exam_type', '未分類')
            fyear = f.get('year', '未知年份')
            if ftype not in tree: tree[ftype] = {}
            if fyear not in tree[ftype]: tree[ftype][fyear] = []
            tree[ftype][fyear].append(f)
        
        type_order = ["學測", "分科", "北模", "中模", "全模", "其他"]
        exam_order = {"第一次": 1, "第二次": 2, "第三次": 3, "正式考試": 4}
        sorted_types = sorted(tree.keys(), key=lambda x: type_order.index(x) if x in type_order else 99)
        
        for ftype in sorted_types:
            with st.expander(f"📁 **{ftype}**", expanded=False):
                sorted_years = sorted(tree[ftype].keys(), key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
                for fyear in sorted_years:
                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;📂 **{fyear} 年度**", unsafe_allow_html=True)
                    files_in_year = tree[ftype][fyear]
                    files_in_year.sort(key=lambda f: exam_order.get(f.get('exam_no'), 99))

                    for f in files_in_year:
                        status = f.get('ai_status', '未辨識')
                        
                        # [修復] 自動檢查狀態邏輯
                        if status == "處理中":
                            batches = cloud_manager.get_processing_status(f['id'])
                            if batches: 
                                pending_count = sum(1 for b in batches if b['status'] == 'pending')
                                if pending_count == 0:
                                    error_count = sum(1 for b in batches if b['status'] == 'error')
                                    new_status = "部分失敗" if error_count > 0 else "已辨識"
                                    cloud_manager.update_file_status(f['id'], new_status)
                                    status = new_status
                                    st.rerun()

                        icon = {"已辨識": "✅", "處理中": "🔄", "部分失敗": "⚠️"}.get(status, "⬜")
                        
                        col_file, col_batches, col_action = st.columns([3, 4, 2])
                        
                        with col_file:
                            st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;📄 {icon} **{f.get('filename')}**")
                            st.caption(f"狀態: {status}")

                        with col_batches:
                            if status != "未辨識":
                                batches = cloud_manager.get_processing_status(f['id'])
                                if batches:
                                    done_cnt = sum(1 for b in batches if b['status'] == 'done')
                                    total = len(batches)
                                    st.progress(done_cnt / total if total > 0 else 0)
                                    
                                    with st.expander(f"查看 {total} 個批次詳情"):
                                        for b in batches:
                                            b_idx = b['batch_index']
                                            b_stat = b['status']
                                            b_msg = b.get('last_error', '')
                                            
                                            bc1, bc2 = st.columns([3, 1])
                                            with bc1:
                                                if b_stat == 'done': st.write(f"Batch {b_idx+1}: ✅ 完成")
                                                elif b_stat == 'pending': st.write(f"Batch {b_idx+1}: ⏳ 等待中")
                                                elif b_stat == 'error': st.error(f"Batch {b_idx+1}: ❌ 失敗 ({b_msg})")
                                            
                                            with bc2:
                                                # [功能確認] 這是個別重試按鈕，請確認是否能點擊
                                                if b_stat == 'error':
                                                    if st.button("🔄", key=f"r_b_{f['id']}_{b_idx}", help="重試此批次"):
                                                        cloud_manager.reset_batch_status(f['id'], b_idx)
                                                        cloud_manager.update_file_status(f['id'], "處理中")
                                                        st.rerun()

                        with col_action:
                            btn_col_main, btn_col_del = st.columns([3, 1])
                            with btn_col_main:
                                if status == "未辨識":
                                    if st.button("🚀 辨識", key=f"s_{f['id']}", use_container_width=True):
                                        status_ph = st.empty()
                                        try:
                                            status_ph.info("⏳ 下載中...")
                                            f_bytes = cloud_manager.download_blob(f.get('blob_name'))
                                            if f_bytes:
                                                status_ph.info("⏳ 分析頁數...")
                                                # [BUG FIX] 這裡也要修正檔案格式判斷
                                                ftype = 'pdf'
                                                if f.get('filename', '').lower().endswith('.docx'):
                                                    ftype = 'docx'
                                                    
                                                total_pages = smart_importer.get_pdf_page_count(f_bytes)
                                                if total_pages == 0:
                                                    imgs, _ = smart_importer.convert_file_to_images(f_bytes, ftype)
                                                    total_pages = len(imgs) if imgs else 0
                                                
                                                if total_pages > 0:
                                                    total_batches = (total_pages + smart_importer.BATCH_SIZE - 1) // smart_importer.BATCH_SIZE
                                                    cloud_manager.init_batch_process(f['id'], total_batches)
                                                    status_ph.success("✅ 初始化完成")
                                                    st.rerun()
                                                else: status_ph.error("❌ 頁數錯誤")
                                            else: status_ph.error("❌ 下載失敗")
                                        except Exception as e: status_ph.error(f"❌ {e}")

                                elif status == "處理中":
                                    st.caption("⚡ 處理中...") 
                                    if run_pending_batch(f, api_key_input): 
                                        st.rerun()
                                    
                                elif status == "部分失敗":
                                    if st.button("重試全部", key=f"re_all_{f['id']}", use_container_width=True):
                                        stats = cloud_manager.get_processing_status(f['id'])
                                        for b in stats:
                                            if b['status'] == 'error': cloud_manager.reset_batch_status(f['id'], b['batch_index'])
                                        cloud_manager.update_file_status(f['id'], "處理中")
                                        st.rerun()
                                        
                                elif status == "已辨識":
                                    if st.button("重設", key=f"rst_{f['id']}", use_container_width=True):
                                        cloud_manager.update_file_status(f['id'], "未辨識"); st.rerun()
                            
                            with btn_col_del:
                                if st.button("🗑️", key=f"d_{f['id']}", use_container_width=True):
                                    cloud_manager.delete_file_record(f['id']); st.rerun()

                    st.markdown("<hr style='margin: 5px 0; border-top: 1px dashed #ddd;'>", unsafe_allow_html=True)

# === Tab 3: 校對 ===
with tab_review:
    st.subheader("📝 匯入校對與編輯")
    ready_files = [f for f in files if f.get('ai_status') in ["已辨識", "部分失敗"]]
    if not ready_files: st.info("暫無可校對的檔案。")
    else:
        file_names = [f['filename'] for f in ready_files]
        sel_file_name = st.selectbox("選擇檔案", file_names)
        sel_file = next(f for f in ready_files if f['filename'] == sel_file_name)
        
        if st.session_state['current_review_file_id'] != sel_file['id']:
            with st.spinner("載入題目資料中..."):
                results_data = cloud_manager.load_all_ai_results(sel_file['id'])
                for res in results_data:
                    if 'ui_key' not in res: res['ui_key'] = str(uuid.uuid4())
                st.session_state['review_data_cache'] = results_data
                st.session_state['current_review_file_id'] = sel_file['id']
                st.session_state['review_page'] = 0
        
        all_results = st.session_state['review_data_cache']
        if not all_results: st.warning("無資料")
        else:
            ITEMS_PER_PAGE = 5
            total_pages = (len(all_results) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            c_info, c_prev, c_next = st.columns([6, 1, 1])
            with c_info: st.caption(f"第 {st.session_state['review_page'] + 1} / {total_pages} 頁")
            with c_prev: 
                if st.button("◀", disabled=(st.session_state['review_page']==0)): st.session_state['review_page'] -= 1; st.rerun()
            with c_next: 
                if st.button("▶", disabled=(st.session_state['review_page']>=total_pages-1)): st.session_state['review_page'] += 1; st.rerun()
            st.divider()

            start_idx = st.session_state['review_page'] * ITEMS_PER_PAGE
            current_page_items = all_results[start_idx:start_idx+ITEMS_PER_PAGE]
            
            for i, res in enumerate(current_page_items):
                real_idx = start_idx + i
                with st.container():
                    col_edit, col_img = st.columns([1, 1])
                    with col_edit:
                        st.markdown(f"#### 第 {res.get('number', real_idx+1)} 題")
                        curr_type = TYPE_MAP_EN_TO_ZH.get(res.get('type'), "單選")
                        new_type = st.selectbox(f"題型", TYPE_OPTIONS, index=TYPE_OPTIONS.index(curr_type) if curr_type in TYPE_OPTIONS else 0, key=f"t_{res['ui_key']}")
                        res['type'] = TYPE_MAP_ZH_TO_EN[new_type]
                        res['content'] = st.text_area("題目內容", res.get('content', ''), height=100, key=f"c_{res['ui_key']}")
                        if res['type'] != "Group":
                            opts = st.text_area("選項", "\n".join(res.get('options', [])), height=80, key=f"o_{res['ui_key']}")
                            res['options'] = opts.split('\n') if opts.strip() else []
                            res['answer'] = st.text_input("答案", res.get('answer', ''), key=f"a_{res['ui_key']}")
                        curr_chap = res.get('chapter', '未分類')
                        res['chapter'] = st.selectbox("章節", smart_importer.PHYSICS_CHAPTERS_LIST, index=smart_importer.PHYSICS_CHAPTERS_LIST.index(curr_chap) if curr_chap in smart_importer.PHYSICS_CHAPTERS_LIST else 0, key=f"ch_{res['ui_key']}")

                    with col_img:
                        img_mode = st.radio("圖片模式", ["AI 預截圖", "整頁手動裁切"], horizontal=True, key=f"im_{res['ui_key']}")
                        if img_mode == "AI 預截圖":
                            target_url = res.get('ref_image_url') or res.get('image_url')
                            if target_url: st.image(target_url, caption="AI 自動截取範圍")
                            else: st.info("無 AI 截圖")
                        else:
                            full_page_b64 = ensure_b64(res, 'full_page')
                            if full_page_b64 and st_cropper:
                                cropped = st_cropper(Image.open(io.BytesIO(base64.b64decode(full_page_b64))), key=f"cr_{res['ui_key']}", box_color='#FF0000')
                                if cropped:
                                    st.image(cropped, width=150)
                                    buf = io.BytesIO(); cropped.save(buf, format='PNG')
                                    res['image_b64'] = base64.b64encode(buf.getvalue()).decode('utf-8')
                                    res['ref_image_url'] = None 
                            else: st.warning("無法載入整頁圖")
                st.divider()

            if st.button("🚀 確認匯入", type="primary", use_container_width=True):
                with st.spinner("匯入中..."):
                    count = 0
                    total = len(all_results)
                    progress = st.progress(0)
                    for idx, item in enumerate(all_results):
                        final_img = item.get('image_b64')
                        final_url = item.get('ref_image_url') or item.get('image_url')
                        img_data = base64.b64decode(final_img) if final_img else None
                        
                        q = Question(
                            q_type=item.get('type'), content=item.get('content'),
                            options=item.get('options', []), answer=item.get('answer', ''),
                            chapter=item.get('chapter', '未分類'), source=sel_file['filename'],
                            image_data=img_data, image_url=final_url if not img_data else None,
                            sub_questions=[Question.from_dict(sq) for sq in item.get('sub_questions', [])] if item.get('sub_questions') else []
                        )
                        cloud_manager.save_question(q.to_dict())
                        count += 1
                        progress.progress((idx+1)/total)
                    cloud_manager.update_file_status(sel_file['id'], "已匯入")
                    del st.session_state['review_data_cache']
                    st.session_state['current_review_file_id'] = None
                    st.success(f"匯入 {count} 題！"); time.sleep(2); st.rerun()

# === Tab 4: 題庫 ===
with tab_bank:
    st.subheader("📚 題庫管理與試卷輸出")
    all_qs = st.session_state['question_pool']
    if not all_qs:
        st.info("目前題庫為空。")
        if st.button("重新載入"): st.session_state['question_pool'] = [Question.from_dict(d) for d in cloud_manager.load_questions()]; st.rerun()
    else:
        c_stat, c_export = st.columns([1, 1])
        with c_stat: st.metric("總題數", len(all_qs))
        with c_export:
            sel_cnt = len(st.session_state['selected_export_ids'])
            if st.button(f"📥 生成 Word (已選 {sel_cnt} 題)", type="primary", disabled=(sel_cnt==0)):
                ex_qs = [q for q in all_qs if q.id in st.session_state['selected_export_ids']]
                ex_qs.sort(key=lambda x: (x.source, x.id))
                with st.spinner("生成中..."):
                    d_ex, d_ans = generate_word_files(ex_qs)
                    b1, b2 = st.columns(2)
                    with b1: st.download_button("📄 下載試題", d_ex, "exam.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                    with b2: st.download_button("📝 下載詳解", d_ans, "ans.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        st.divider()
        qs_by_source = {}
        for q in all_qs:
            if q.parent_id: continue 
            src = q.source if q.source else "未分類"
            if src not in qs_by_source: qs_by_source[src] = []
            qs_by_source[src].append(q)
            
        sorted_srcs = sorted(qs_by_source.keys(), reverse=True)
        for src in sorted_srcs:
            qlist = qs_by_source[src]
            qlist.sort(key=lambda q: q.id)
            with st.expander(f"📁 {src} ({len(qlist)} 題)"):
                all_ids = [q.id for q in qlist]
                is_all = all(qid in st.session_state['selected_export_ids'] for qid in all_ids)
                
                select_all_key = f"sa_{src}"
                new_all_state = st.checkbox(f"全選", value=is_all, key=select_all_key)
                
                if new_all_state and not is_all: 
                    for qid in all_ids: st.session_state['selected_export_ids'].add(qid)
                    st.rerun()
                elif not new_all_state and is_all: 
                    for qid in all_ids: st.session_state['selected_export_ids'].discard(qid)
                    st.rerun()
                
                for q in qlist:
                    c1, c2, c3 = st.columns([0.5, 8, 1.5])
                    with c1:
                        if st.checkbox("", value=(q.id in st.session_state['selected_export_ids']), key=f"ck_{q.id}"): st.session_state['selected_export_ids'].add(q.id)
                        else: st.session_state['selected_export_ids'].discard(q.id)
                    with c2:
                        bdg = TYPE_MAP_EN_TO_ZH.get(q.type, q.type)
                        st.markdown(f"**【{bdg}】** {q.content[:50]}...")
                    with c3:
                        with st.popover("⚙️"):
                            nc = st.text_area("內容", q.content, key=f"ec_{q.id}")
                            na = st.text_input("答案", q.answer, key=f"ea_{q.id}")
                            if st.button("儲存", key=f"sv_{q.id}"):
                                q.content, q.answer = nc, na
                                cloud_manager.save_question(q.to_dict()); st.success("OK"); time.sleep(0.5); st.rerun()
                            if st.button("刪除", key=f"rm_{q.id}", type="primary"):
                                cloud_manager.delete_question(q.id)
                                st.session_state['question_pool'] = [x for x in st.session_state['question_pool'] if x.id != q.id]
                                st.rerun()
                    st.divider()
