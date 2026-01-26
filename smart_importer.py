import re
import io
import json
import time
from PIL import Image

# ... (依賴與常數定義保持不變) ...
# ==========================================
# 依賴套件與環境檢查
# ==========================================
HAS_GENAI = False
HAS_PDF2IMAGE = False
HAS_OCR = False
HAS_DOCX = False

try:
    import google.generativeai as genai
    from google.ai.generativelanguage_v1beta.types import content
    HAS_GENAI = True
except ImportError: pass

try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError: pass

try:
    import pytesseract
    HAS_OCR = True
except ImportError: pass

try:
    import docx
    HAS_DOCX = True
except ImportError: pass

def is_ocr_available():
    return HAS_PDF2IMAGE and HAS_OCR

# ==========================================
# 常數定義
# ==========================================
PHYSICS_CHAPTERS_LIST = [
    "未分類", 
    "第一章.科學的態度與方法", 
    "第二章.物體的運動", 
    "第三章. 物質的組成與交互作用",
    "第四章.電與磁的統一", 
    "第五章. 能　量", 
    "第六章.量子現象"
]

EXCLUDE_KEYWORDS = [
    "化學", "反應式", "有機化合物", "酸鹼", "沉澱", "氧化還原", "莫耳", "原子量",
    "生物", "細胞", "遺傳", "DNA", "染色體", "演化", "生態", "光合作用", "酵素",
    "地科", "地質", "板塊", "洋流", "大氣", "氣候", "岩石", "化石", "星系", "地層"
]

# ... (SmartQuestionCandidate 類別) ...
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
        self.image_bytes = image_bytes      
        self.ref_image_bytes = ref_image_bytes 
        self.full_page_bytes = full_page_bytes 
        self.q_type = q_type
        self.subject = subject
        self.sub_questions = sub_questions if sub_questions else [] 

# ... (工具函式) ...
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

def crop_image(original_img, box_2d, force_full_width=False, padding_y=10):
    if not box_2d or len(box_2d) != 4: return None
    width, height = original_img.size
    ymin, xmin, ymax, xmax = box_2d
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    if force_full_width:
        left = 0
        right = width
    else:
        xmin = max(0, xmin - 10)
        xmax = min(1000, xmax + 10)
        left = (xmin / 1000) * width
        right = (xmax / 1000) * width
    top = (ymin / 1000) * height
    bottom = (ymax / 1000) * height
    if right <= left or bottom <= top: return None
    try:
        cropped = original_img.crop((left, top, right, bottom))
        img_byte_arr = io.BytesIO()
        if cropped.mode in ("RGBA", "P"): cropped = cropped.convert("RGB")
        cropped.save(img_byte_arr, format='JPEG', quality=85)
        return img_byte_arr.getvalue()
    except Exception as e:
        print(f"Crop failed: {e}")
        return None

def img_to_bytes(pil_img):
    if pil_img is None: return None
    img_byte_arr = io.BytesIO()
    if pil_img.mode in ("RGBA", "P"): pil_img = pil_img.convert("RGB")
    pil_img.save(img_byte_arr, format='JPEG', quality=85) 
    return img_byte_arr.getvalue()

# [核心修改] 支援 target_pages 參數
def parse_with_gemini(file_bytes, file_type, api_key, target_pages=None):
    if not HAS_GENAI: return {"error": "缺少 google-generativeai 套件"}
    if not api_key: return {"error": "請輸入 API Key"}

    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        return {"error": f"API Key 設定失敗: {str(e)}"}

    source_images = [] 
    
    if file_type == 'pdf':
        if not HAS_PDF2IMAGE: return {"error": "缺少 pdf2image"}
        try:
            source_images = convert_from_bytes(file_bytes, dpi=150, fmt='jpeg')
        except Exception as e:
            return {"error": f"PDF 轉圖片失敗: {str(e)}"}
    elif file_type == 'docx':
        if not HAS_DOCX: return {"error": "缺少 python-docx"}
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    img_bytes = rel.target_part.blob
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    source_images.append(pil_img)
        except Exception as e:
            return {"error": f"Word 解析失敗: {str(e)}"}
    
    if not source_images: return {"error": "無法提取圖片"}

    # 處理指定頁數範圍
    images_to_process = source_images
    if target_pages and file_type == 'pdf':
        start_p, end_p = target_pages
        # 邊界檢查
        start_p = max(0, start_p)
        end_p = min(len(source_images), end_p)
        if start_p < end_p:
            images_to_process = source_images[start_p:end_p]
        else:
            return {"error": "指定的頁數範圍無效"}

    # ... (後續 Gemini 呼叫邏輯同前，但只處理 images_to_process) ...
    # 這裡為了簡潔省略了中間的 Prompt 建構與 API 呼叫，請確保這部分與之前版本一致
    # 只是 input_parts.extend(batch_imgs) 這裡的 batch_imgs 就是 images_to_process

    # ... (回傳 all_candidates) ...
    return [] # Placeholder, 請填回完整邏輯
