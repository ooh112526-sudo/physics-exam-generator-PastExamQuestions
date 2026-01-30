import re
import io
import json
import time
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

# ==========================================
# 候選題目物件
# ==========================================
class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number, options=None, chapter="未分類", 
                 is_likely=True, status_reason="", image_bytes=None, q_type="Single", 
                 ref_image_bytes=None, full_page_bytes=None, subject="Physics", 
                 sub_questions=None, page_index=0, full_question_box_2d=None):
        self.raw_text = raw_text
        try:
            self.number = int(question_number)
        except:
            self.number = 0
            
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
        self.page_index = page_index
        # [關鍵新增] 保留座標資料，供後端動態裁切使用
        self.full_question_box_2d = full_question_box_2d

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
        cropped.save(img_byte_arr, format='JPEG', quality=70)
        return img_byte_arr.getvalue()
    except Exception as e:
        return None

def img_to_bytes(pil_img):
    if pil_img is None: return None
    img_byte_arr = io.BytesIO()
    if pil_img.mode in ("RGBA", "P"): pil_img = pil_img.convert("RGB")
    pil_img.save(img_byte_arr, format='JPEG', quality=70) 
    return img_byte_arr.getvalue()

# ==========================================
# Gemini AI 解析邏輯
# ==========================================
def parse_with_gemini(file_bytes, file_type, api_key, target_pages=None):
    if not HAS_GENAI or not api_key: return {"error": "API Key 錯誤"}
    try: genai.configure(api_key=api_key)
    except Exception as e: return {"error": f"API Key 設定失敗: {e}"}

    source_images = [] 
    if file_type == 'pdf':
        if not HAS_PDF2IMAGE: return {"error": "缺少 pdf2image"}
        try:
            # DPI 100 已經足夠且快速
            if target_pages:
                start_p, end_p = target_pages
                source_images = convert_from_bytes(file_bytes, dpi=100, fmt='jpeg', first_page=start_p+1, last_page=end_p)
            else:
                source_images = convert_from_bytes(file_bytes, dpi=100, fmt='jpeg')
        except Exception as e: return {"error": f"PDF 轉圖失敗: {e}"}
    elif file_type == 'docx':
        if not HAS_DOCX: return {"error": "缺少 python-docx"}
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    img_bytes = rel.target_part.blob
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    source_images.append(pil_img)
        except Exception as e: return {"error": f"Word 解析失敗: {e}"}
    
    if not source_images: return {"error": "無法提取圖片"}

    batches = [source_images]
    start_offset = target_pages[0] if target_pages else 0
    prompt_chapters = [c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"]
    chapters_str = "\n".join(prompt_chapters)
    candidate_models = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-pro"]
    
    all_candidates = []
    errors = []

    for batch_idx, batch_imgs in enumerate(batches):
        prompt = f"""
        分析考卷圖片，只擷取【高中物理】試題。
        判題規則：含「應選X項」為 Multi (多選)；無選項為 Fill (填充)；含子題為 Group (題組)。
        請回傳每題座標：
        1. full_question_box_2d: [ymin, 0, ymax, 1000] (整題範圍)
        2. box_2d: [ymin, xmin, ymax, xmax] (附圖範圍)
        
        輸出 JSON List: [{{number, type, content, options, answer, chapter, full_question_box_2d, box_2d, page_index}}]
        """
        input_parts = [prompt] + batch_imgs
        
        response = None
        last_error = None
        for m in candidate_models:
            try:
                model = genai.GenerativeModel(m)
                response = model.generate_content(input_parts, generation_config={"response_mime_type": "application/json"})
                break
            except Exception as e: last_error = e; continue
        
        if not response or not response.text:
             errors.append(f"Batch failed: {last_error}"); continue

        try:
            data = json.loads(clean_json_string(response.text))
            if isinstance(data, dict): data = [data]
            
            for item in data:
                content_text = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
                if any(ek in content_text for ek in EXCLUDE_KEYWORDS): continue 

                q_type = item.get('type', 'Single')
                if "應選" in content_text: q_type = "Multi"
                if q_type != "Group" and not item.get('options'): q_type = "Fill"

                diagram_bytes = None
                ref_bytes = None
                
                rel_idx = item.get('page_index', 0)
                if not isinstance(rel_idx, int): rel_idx = 0
                abs_idx = start_offset + rel_idx

                # 這裡只做基本附圖裁切，參考圖留給前端動態做
                if file_type == 'pdf' and 0 <= rel_idx < len(batch_imgs):
                    src_img = batch_imgs[rel_idx]
                    if 'box_2d' in item: diagram_bytes = crop_image(src_img, item['box_2d'], False, 5)
                    # ref_bytes 這裡先切一份小的備用，但主要靠後端動態切
                    if 'full_question_box_2d' in item: ref_bytes = crop_image(src_img, item['full_question_box_2d'], True, 100)

                cand = SmartQuestionCandidate(
                    raw_text=item.get('content', ''), question_number=item.get('number', 0),
                    options=item.get('options', []), chapter=item.get('chapter', '未分類'),
                    image_bytes=diagram_bytes,      
                    ref_image_bytes=ref_bytes,
                    full_page_bytes=None, # 不存大圖，省記憶體
                    q_type=q_type, subject='Physics', sub_questions=item.get('sub_questions', []),
                    page_index=abs_idx,
                    full_question_box_2d=item.get('full_question_box_2d') # 儲存座標
                )
                cand.content = item.get('content', '')
                all_candidates.append(cand)
        except Exception as e: errors.append(f"Parse error: {e}")

    if not all_candidates and errors: return {"error": "; ".join(errors)}
    try: all_candidates.sort(key=lambda x: int(x.number))
    except: pass
    return all_candidates

def parse_raw_file(file_obj, file_type, use_ocr=False): return []
