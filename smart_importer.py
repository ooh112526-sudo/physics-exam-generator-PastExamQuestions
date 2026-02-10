import re
import io
import json
import time
import base64
from PIL import Image
# ==========================================
# 依賴套件與環境檢查
# ==========================================
HAS_GENAI = False
HAS_PDF2IMAGE = False
HAS_OCR = False
HAS_DOCX = False
try:
    import google.generativeai as genai
    HAS_GENAI = True
except ImportError: pass
try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError: pass
try:
    import docx
    HAS_DOCX = True
except ImportError: pass
# ==========================================
# 常數定義
# ==========================================
PHYSICS_CHAPTERS_LIST = [
    "未分類", 
    "第一章.科學的態度與方法", 
    "第二章.物體的運動", 
    "第三章.物質的組成與交互作用",
    "第四章.電與磁的統一", 
    "第五章.能量", 
    "第六章.量子現象"
]
EXCLUDE_KEYWORDS = [
    "化學", "反應式", "有機化合物", "酸鹼", "沉澱", "氧化還原", "莫耳", "原子量",
    "生物", "細胞", "遺傳", "DNA", "染色體", "演化", "生態", "光合作用", "酵素",
    "地科", "地質", "板塊", "洋流", "大氣", "氣候", "岩石", "化石", "星系", "地層"
]
# ==========================================
# 候選題目物件 (資料結構升級)
# ==========================================
class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number, options=None, chapter="未分類", 
                 is_likely=True, status_reason="", image_bytes=None, q_type="Single", 
                 ref_image_bytes=None, full_page_bytes=None, subject="Physics", sub_questions=None,
                 solution=""): # [New] 新增 solution 參數
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text 
        self.options = options if options else []
        self.answer = "" # 預設空
        self.solution = solution # [New] 解析
        self.predicted_chapter = chapter if chapter in PHYSICS_CHAPTERS_LIST else "未分類"
        self.is_physics_likely = is_likely
        self.status_reason = status_reason
        
        # 圖片相關
        self.image_bytes = image_bytes          # 目前使用的截圖
        self.ai_crop_backup = image_bytes       # AI 原始截圖備份
        self.ref_image_bytes = ref_image_bytes  # 參考區域圖
        self.full_page_bytes = full_page_bytes  # 整頁底圖
        self.use_image = False                  # 預設不使用圖片
        
        self.q_type = q_type
        self.subject = subject
        self.sub_questions = sub_questions if sub_questions else []
# ==========================================
# 工具函式
# ==========================================
def clean_json_string(json_str):
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    
    start = json_str.find('[')
    end = json_str.rfind(']')
    if start != -1 and end != -1:
        json_str = json_str[start:end+1]
    return json_str.strip()
def check_is_group_header(text):
    """
    [新] 檢查文字是否包含題組關鍵字 (如: 16-18題為題組)
    回傳: (是否為題組, 題號範圍字串)
    """
    # 常見格式：16.~18.題為題組, 16-18為題組, 16~18 題為題組
    pattern = r'(\d+)\s*[.~～-]\s*(\d+)\s*.*題?為?題組'
    match = re.search(pattern, text)
    if match:
        return True, f"{match.group(1)}~{match.group(2)}"
    return False, ""
def crop_image(original_img, box_2d, force_full_width=False, padding_y=20):
    """
    裁切圖片函式 (已針對全寬需求優化)
    padding_y: 上下擴張的範圍 (千分比)
    force_full_width: 是否強制使用整頁寬度
    """
    if not box_2d or len(box_2d) != 4: return None
    width, height = original_img.size
    ymin, xmin, ymax, xmax = box_2d
    
    # [核心修改] 全寬與空白填充邏輯 (Universal Screenshot Rule)
    # ymin/ymax 是 0-1000 的比例座標。
    # 策略：padding_y 用於確保上下留白，避免切字。
    
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    
    # 強制全寬：左右直接切齊頁面邊緣 (0~1000)
    left, right = 0, width
    
    # 計算實際像素高度
    top = (ymin / 1000) * height
    bottom = (ymax / 1000) * height
    
    if right <= left or bottom <= top: return None
    try:
        cropped = original_img.crop((left, top, right, bottom))
        img_byte_arr = io.BytesIO()
        if cropped.mode in ("RGBA", "P"): cropped = cropped.convert("RGB")
        cropped.save(img_byte_arr, format='JPEG', quality=85) # 提高畫質至 85
        return img_byte_arr.getvalue()
    except Exception as e:
        return None
def img_to_bytes(pil_img):
    if pil_img is None: return None
    img_byte_arr = io.BytesIO()
    if pil_img.mode in ("RGBA", "P"): pil_img = pil_img.convert("RGB")
    pil_img.save(img_byte_arr, format='JPEG', quality=85)
    return img_byte_arr.getvalue()
# ==========================================
# 核心邏輯
# ==========================================
def get_pdf_page_count(file_bytes):
    if not HAS_PDF2IMAGE: return 0
    try:
        from pdf2image.pdf2image import pdfinfo_from_bytes
        info = pdfinfo_from_bytes(file_bytes)
        return info.get("Pages", 0)
    except: return 0
def convert_file_to_images(file_bytes, file_type, first_page=None, last_page=None):
    if file_type == 'pdf':
        if not HAS_PDF2IMAGE: return None, "缺少 pdf2image"
        try:
            # 畫質設定 DPI 100
            return convert_from_bytes(file_bytes, dpi=100, fmt='jpeg', first_page=first_page, last_page=last_page), None
        except Exception as e:
            return None, f"PDF 轉圖失敗: {str(e)}"
    elif file_type == 'docx':
        if not HAS_DOCX: return None, "缺少 python-docx"
        try:
            images = []
            doc = docx.Document(io.BytesIO(file_bytes))
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    img_bytes = rel.target_part.blob
                    images.append(Image.open(io.BytesIO(img_bytes)))
            # 簡單模擬分頁
            if first_page and last_page:
                start, end = first_page - 1, last_page
                return (images[start:end] if start < len(images) else []), None
            return images, None
        except Exception as e:
            return None, f"Word 解析失敗: {str(e)}"
    return None, "不支援的格式"
# 批次大小
BATCH_SIZE = 5
def process_single_batch(batch_images, batch_index, api_key, start_page_idx):
    if not HAS_GENAI: return None, "缺少 google-generativeai"
    if not api_key: return None, "缺少 API Key"
    try:
        genai.configure(api_key=api_key)
        chapters_str = "\n".join([c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"])
        
        # [核心 Prompt V7]: 強化視覺邊界裁切規則
        prompt = f"""
        你是一個高中物理題庫分析專家，請分析圖片中的高中物理試題，並將其轉為 JSON 格式。
 【最高指導原則：解題與解析】
 - 對於每一題（單選、複選、填充），你**必須**進行解題，並在 "answer" 欄位填入答案，在 "solution" 欄位填入詳細解析。
        - **solution 欄位為必填**：請詳細列出解題思路、關鍵公式（如 F=ma, V=IR）與步驟。
        - 只有「題組母題 (Group Parent)」的 solution 可以留空。
        【任務 1：科目過濾 (Filtering)】
        - 僅提取「物理科」題目。
        - 若遇到化學、生物、地科題目，請直接跳過，不要輸出到 JSON。
        
        【任務 2：物理題型結構化 (Structure)】
        請依照以下優先順序判斷每一題的模式：
        **優先層級 1：題組偵測 (Group Mode)**
        - 觸發：標題含有「題組」、「X.~Y.題為題組」、「閱讀下文」。
        - 結構：必須包含母題(標題/文章/圖片)與所有子題。
       - **截圖規則(重要)**：回傳一個包含「母題+所有子題」的全域大範圍 box_2d。母題與所有子題共用此 box_2d，不需切分。
       - **子題邏輯**：題組內的每一個子題，都必須獨立判斷是 Single/Multi/Fill。
        
        **優先層級 2：單一題型判斷 (Standalone Mode)**
        - 若非題組，則依以下特徵分類：
        - **A. 複選題 (Multi)**：內容含「應選 X 項」、「哪些正確」、「多選」。
        - **B. 單選題 (Single)**：內容含標準選項 (A)~(E) 且無複選關鍵字。
        - **C. 填充/簡答 (Fill)**：無選項列表，或為問答、作圖、計算形式。
        
        【任務 3：全寬截圖規則 (Universal Screenshot Rule - 視覺錨點策略)】
        - **X 軸**：強制全寬 (0-1000)。
        - **Y 軸 (box_2d) 判定邏輯**：
          - 請給出該題目在頁面上的「絕對座標」。
          - 請將頁面視為由上而下堆疊的區塊。
            1. **上界 (ymin)**：
               - 向上延伸至「上一個視覺區塊」(上一題或分隔線) 的結束處。
               - 確保包含本題上方的標題或空白。
            2. **下界 (ymax) - 重要修正**：
               - 請包含本題所有內容（含選項、下方的圖表、註腳）。
               - **截止點 (Stop Point)**：請延伸至**「視覺上緊鄰的下一題 (Next Question)」**的第一行文字上方停止。
               - **關鍵指令**：不管下一題是物理、化學或生物，一旦看到**新的題號**或**分隔線**出現，截圖範圍必須在此終止。
               - **禁止**：絕對不要包含下一題的文字內容。若本題與下一題之間有大量空白，可包含該空白，但在下一題文字開始前務必停止。
         - **題組**：box_2d 必須包覆整個題組區塊 (母題+子題)。
        【任務 4：圖片索引 (Page Index) - 關鍵】
        - 因為一次輸入多張圖片，請務必標示該題目位於哪一張圖。
        - **page_index**: 0 代表第一張圖, 1 代表第二張圖...以此類推。
        - 若無此欄位，截圖將會錯誤。
        
        【任務 5：智慧解題 (Intelligent Solving) - 重點】
        - 若圖片中有明顯標示答案（如紅筆圈選、詳解本），優先使用該答案。
        - **若圖片中無答案，請根據物理知識自行推導解題，並填入 "answer" 欄位。**
        - **solution 欄位**：請提供簡短的解題思路、關鍵公式或步驟（例如："利用 F=ma，得知..."）。若為題組母題可留空。
        【任務6：章節 (chapter): 選擇最接近的: {chapters_str}】
        【JSON 輸出範例 (Ground Truth)】
        
        // 案例 1: 單選題 (Single)
        {{
            "number": 3,
            "type": "Single",
            "content": "3. 將ㄇ字型的光滑金屬軌道...",
            "options": ["(A) ...", "(B) ..."],
            "answer": "A",
            "solution": "根據法拉第電磁感應定律 ε = LvB，且電流方向...", // [New] AI 自動解析
            "box_2d": [100, 0, 350, 1000], // 絕對座標，含上下空白
            "page_index": 0 // 位於第1張圖
        }}
        
        // 案例 2: 複選題 (Multi) - 假設第4題是化學題被跳過，故第5題從較下方開始
        {{
            "number": 5,
            "type": "Multi",
            "content": "5. 某分子最外層...有幾種可能的譜線？ (應選2項)",
            "options": ["(A) 10", "(B) 6", "(C) 4", "(D) 3", "(E) 1"],
            "answer": "AB",
            "box_2d": [550, 0, 700, 1000], // 絕對座標，即使與上一題不連續
            "page_index": 0 // 位於第1張圖
        }}
        
        // 案例 3: 填充/簡答題 (Fill) - 假設在下一頁
        {{
            "number": 41,
            "type": "Fill",
            "content": "41. 請說明...意義為何？(2分)",
            "options": [], // 無選項
            "answer": "",
            "box_2d": [100, 0, 250, 1000],
            "page_index": 1 // 位於第2張圖
        }}
        
        // 案例 4: 題組 (Group) - 混合子題型
        {{
            "number": 40 ~ 42,
            "type": "Group",
            "content": "40-42題為題組\\n19世紀末實驗發現... (共用文章)",
            "box_2d": [100, 0, 600, 1000], // [重點] 母題+子題的大範圍
            "page_index": 1, // 位於第2張圖
            "sub_questions": [
                {{
                    "number": 40,
                    "type": "Single", // 子題1: 單選
                    "content": "40. 有一紅光雷射...",
                    "options": ["(A)..."],
                    "answer": "A",
                    "box_2d": [100, 0, 600, 1000], // 繼承母題 box_2d
                    "page_index": 1
                }},
                {{
                    "number": 41,
                    "type": "Fill",   // 子題2: 簡答
                    "content": "41. 請說明...",
                    "options": [],
                    "answer": "",
                    "box_2d": [100, 0, 600, 1000], // 繼承母題 box_2d
                    "page_index": 1
                }}
            ]
        }}
        請直接回傳 JSON 陣列，不要使用 Markdown 標記或其他文字。。
        """
        
        # 指定使用 Gemini 3.0 系列與 2.5 系列
        models = ["gemini-3.0-pro", "gemini-3.0-flash", "gemini-2.5-pro", "gemini-2.5-flash"]
        response = None
        last_err = None
        
        for m in models:
            try:
                model = genai.GenerativeModel(m)
                response = model.generate_content([prompt] + batch_images, generation_config={"response_mime_type": "application/json"})
                break
            except Exception as e:
                last_err = e
                # 簡單的重試延遲，避免瞬間打死所有 quota
                time.sleep(1)
                continue
        
        if not response: return None, f"AI Error: {last_err}"
        
        data = json.loads(clean_json_string(response.text))
        if isinstance(data, dict): data = [data]
        
        processed = []
        for item in data:
            content = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
            
            # [Python 端雙重過濾] 雖然 Prompt 已要求過濾，這裡再做一次保險
            if any(k in content for k in EXCLUDE_KEYWORDS): continue
            
            # 自動判斷 (Fallback logic if AI misses type)
            q_type = item.get('type', 'Single')
            if q_type != "Group":
                if "應選" in content and ("項" in content or "二" in content): q_type = "Multi"
                if not item.get('options'): q_type = "Fill"
            
            # 圖片裁切
            img_b, ref_b, full_b = None, None, None
            try:
                # [關鍵修正]: 優先使用 AI 回傳的 page_index，否則才預設為 0
                idx = item.get('page_index', 0)
                
                if isinstance(idx, int) and 0 <= idx < len(batch_images):
                    src = batch_images[idx]
                    full_b = img_to_bytes(src)
                    
                    if 'box_2d' in item: 
                        # [重要修正]: 使用 force_full_width=True 確保寬度為 0-1000
                        # padding_y=20 給予更寬鬆的上下空白
                        img_b = crop_image(src, item['box_2d'], force_full_width=True, padding_y=20)
                    
                    # 題組全圖通常就等於 ref_b
                    if q_type == "Group":
                        ref_b = img_b
                    elif 'full_question_box_2d' in item:
                         ref_b = crop_image(src, item['full_question_box_2d'], True, 150)
                    else:
                        ref_b = full_b
            except: pass
            
            cand = {
                "number": item.get('number', 0),
                "content": item.get('content', ''),
                "options": item.get('options', []),
                "answer": item.get('answer', ''),
                "solution": item.get('solution', ''), # [New] 接收 AI 的解析
                "chapter": item.get('chapter', '未分類'),
                "type": q_type,
                "image_b64": base64.b64encode(img_b).decode() if img_b else None,
                "ai_crop_backup_b64": base64.b64encode(img_b).decode() if img_b else None, # 備份
                "ref_image_b64": base64.b64encode(ref_b).decode() if ref_b else None,
                "full_page_b64": base64.b64encode(full_b).decode() if full_b else None,
                "sub_questions": item.get('sub_questions', []),
                "use_image": False # 預設不使用
            }
            
            # [重要]: 若為題組，需確保子題也繼承截圖資料
            if q_type == "Group" and cand["sub_questions"]:
                 for sub in cand["sub_questions"]:
                     pass
            processed.append(cand)
            
        return processed, None
    except Exception as e: return None, str(e)

