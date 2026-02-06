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
                 ref_image_bytes=None, full_page_bytes=None, subject="Physics", sub_questions=None):
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text 
        self.options = options if options else []
        self.predicted_chapter = chapter if chapter in PHYSICS_CHAPTERS_LIST else "未分類"
        self.is_physics_likely = is_likely
        self.status_reason = status_reason
        
        # 圖片相關
        self.image_bytes = image_bytes          # 目前使用的截圖 (可能是 AI 的，也可能是手動裁切後的)
        self.ai_crop_backup = image_bytes       # [新] AI 原始截圖備份 (永遠不變，除非整題刪除)
        self.ref_image_bytes = ref_image_bytes  # 參考區域圖
        self.full_page_bytes = full_page_bytes  # 整頁底圖
        self.use_image = False                  # [新] 預設不使用圖片，需手動勾選
        
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

def crop_image(original_img, box_2d, force_full_width=False, padding_y=10):
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
    # 對於題組，此 box_2d 已經是包含母題+所有子題的大範圍。
    
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    
    # 強制全寬：左右直接切齊頁面邊緣 (0~1000)
    # 這是您 "所有的AI截圖都是全寬" 的要求
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
        
        # [核心 Prompt V3]: 整合四種模式判斷與全域大圖截圖邏輯
        prompt = f"""
        你是一個高中物理題庫分析專家，請分析圖片中的高中物理試題，並將其轉為 JSON 格式。
        
        【任務：物理題型精準辨識與結構化】
        請依照以下優先順序判斷每一題的模式：
        
        **優先層級 1：題組偵測 (Group Mode)**
        - 觸發：標題含有「題組」、「X-Y題」、「閱讀下文」。
        - 結構：必須包含母題(標題/文章/圖片)與所有子題。
        - **截圖規則 (重要)**：回傳一個包含「母題+所有子題」的全域大範圍 box_2d。母題與所有子題共用此 box_2d，不需切分。
        - **子題邏輯**：題組內的每一個子題，都必須獨立判斷是 Single/Multi/Fill。
        
        **優先層級 2：單一題型判斷 (Standalone Mode)**
        - 若非題組，則依以下特徵分類：
        - **A. 複選題 (Multi)**：內容含「應選 X 項」、「哪些正確」、「多選」。
        - **B. 單選題 (Single)**：內容含標準選項 (A)~(E) 且無複選關鍵字。
        - **C. 填充/簡答 (Fill)**：無選項列表，或為問答、作圖、計算形式。
        
        【通用截圖規則 (Universal Screenshot Rule)】
        - **X 軸**：強制全寬 (0-1000)。
        - **Y 軸**：必須包含「上一題結束後的空白」到「下一題開始前的空白」。
        - **題組**：box_2d 必須包覆整個題組區塊 (母題+子題)。
        
        【JSON 輸出範例 (Ground Truth)】
        
        // 案例 1: 單選題
        {{
            "number": 3,
            "type": "Single",
            "content": "3. 將ㄇ字型的光滑金屬軌道...",
            "options": ["(A) ...", "(B) ..."],
            "answer": "A",
            "box_2d": [0, 0, 350, 1000] // 全寬，含上下空白
        }}
        
        // 案例 2: 複選題 (Multi)
        {{
            "number": 5,
            "type": "Multi",
            "content": "5. 某分子最外層...有幾種可能的譜線？ (應選2項)",
            "options": ["(A) 10", "(B) 6", "(C) 4", "(D) 3", "(E) 1"],
            "answer": "AB",
            "box_2d": [350, 0, 500, 1000] // 全寬，含上下空白
        }}
        
        // 案例 3: 填充/簡答題 (Fill)
        {{
            "number": 41,
            "type": "Fill",
            "content": "41. 請說明...意義為何？(2分)",
            "options": [], // 無選項
            "answer": "",
            "box_2d": [500, 0, 650, 1000] // 全寬，含上下空白
        }}
        
        // 案例 4: 題組 (Group) - 混合子題型
        {{
            "number": 40,
            "type": "Group",
            "content": "40-42題為題組\\n19世紀末實驗發現... (共用文章)",
            "box_2d": [650, 0, 950, 1000], // [重點] 母題+子題的大範圍
            "sub_questions": [
                {{
                    "number": 40,
                    "type": "Single", // 子題1: 單選
                    "content": "40. 有一紅光雷射...",
                    "options": ["(A)..."],
                    "answer": "A",
                    "box_2d": [650, 0, 950, 1000] // 繼承母題 box_2d
                }},
                {{
                    "number": 41,
                    "type": "Fill",   // 子題2: 簡答
                    "content": "41. 請說明...",
                    "options": [],
                    "answer": "",
                    "box_2d": [650, 0, 950, 1000] // 繼承母題 box_2d
                }}
            ]
        }}
        
        5. 章節 (chapter): 選擇最接近的: {chapters_str}
        請直接回傳 JSON 陣列。
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
            if any(k in content for k in EXCLUDE_KEYWORDS): continue
            
            # 自動判斷 (Fallback logic if AI misses type)
            q_type = item.get('type', 'Single')
            if q_type != "Group":
                if "應選" in content and ("項" in content or "二" in content): q_type = "Multi"
                if not item.get('options'): q_type = "Fill"
            
            # 圖片裁切
            img_b, ref_b, full_b = None, None, None
            try:
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
                     # 雖然 JSON 中已經要求 box_2d 相同，但後端處理時 AI 可能只傳回母題圖片
                     # 這裡不需額外處理，因為前端顯示時，子題會使用自己的 content/options
                     # 而截圖顯示會依賴母題的 image_b64 (如果是共用大圖模式)
                     # 但為了資料完整性，若 AI 有回傳子題的 box_2d (通常與母題同)，我們也可以在前端處理
                     pass

            processed.append(cand)
            
        return processed, None

    except Exception as e: return None, str(e)
