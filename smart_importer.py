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
                 ref_image_bytes=None, full_page_bytes=None, subject="Physics"):
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text 
        self.options = options if options else []
        self.predicted_chapter = chapter if chapter in PHYSICS_CHAPTERS_LIST else "未分類"
        self.is_physics_likely = is_likely
        self.status_reason = status_reason
        self.image_bytes = image_bytes      # 題目附圖 (已裁切)
        self.ref_image_bytes = ref_image_bytes # 題目區域截圖 (供參考)
        self.full_page_bytes = full_page_bytes # [關鍵] 整頁原始圖 (供手動裁切用)
        self.q_type = q_type
        self.subject = subject

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
        # 壓縮裁切圖以節省空間
        cropped.save(img_byte_arr, format='JPEG', quality=85)
        return img_byte_arr.getvalue()
    except Exception as e:
        print(f"Crop failed: {e}")
        return None

def img_to_bytes(pil_img):
    """將 PIL Image 轉為 bytes"""
    if pil_img is None: return None
    img_byte_arr = io.BytesIO()
    if pil_img.mode in ("RGBA", "P"): 
        pil_img = pil_img.convert("RGB")
    # 壓縮整頁圖片以加速傳輸，避免 Session State 過大
    pil_img.save(img_byte_arr, format='JPEG', quality=80) 
    return img_byte_arr.getvalue()

# ==========================================
# Gemini AI 解析邏輯
# ==========================================
def parse_with_gemini(file_bytes, file_type, api_key):
    if not HAS_GENAI: return {"error": "缺少 google-generativeai 套件"}
    if not api_key: return {"error": "請輸入 API Key"}

    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        return {"error": f"API Key 設定失敗: {str(e)}"}

    source_images = [] 
    
    if file_type == 'pdf':
        if not HAS_PDF2IMAGE: return {"error": "缺少 pdf2image (Poppler) 未安裝"}
        try:
            # 解析度 150 dpi 對 AI 來說足夠且快速
            source_images = convert_from_bytes(file_bytes, dpi=150, fmt='jpeg')
        except Exception as e:
            return {"error": f"PDF 轉圖片失敗: {str(e)}"}
            
    elif file_type == 'docx':
        if not HAS_DOCX: return {"error": "缺少 python-docx 套件"}
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    img_bytes = rel.target_part.blob
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    source_images.append(pil_img)
        except Exception as e:
            return {"error": f"Word 解析失敗: {str(e)}"}
    else:
        return {"error": "僅支援 PDF 或 Word 檔"}

    if not source_images and file_type == 'pdf': return {"error": "PDF 頁面為空"}

    # 設定 Batch Size
    BATCH_SIZE = 10 
    total_pages = len(source_images)
    all_candidates = []
    errors = []

    if file_type == 'docx':
        batches = [source_images] 
    else:
        batches = [source_images[i:i + BATCH_SIZE] for i in range(0, total_pages, BATCH_SIZE)]

    prompt_chapters = [c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"]
    chapters_str = "\n".join(prompt_chapters)
    
    # 2026年模型清單
    candidate_models = [
        "gemini-2.5-flash",    
        "gemini-2.5-pro",      
        "gemini-2.0-flash",    
        "gemini-1.5-pro"       
    ]

    for batch_idx, batch_imgs in enumerate(batches):
        start_page_idx = batch_idx * BATCH_SIZE
        
        extra_instruction = ""
        if file_type == 'pdf':
            extra_instruction = """
            【必須】回傳每題的座標範圍：
            1. 'full_question_box_2d': 整題(含題號,文字,選項)的邊界 [ymin, xmin, ymax, xmax] (0-1000)。
            2. 'box_2d': 若有圖片，標示圖片範圍。
            3. 'page_index': 該題目位於本批次圖片的第幾頁 (0, 1, ...)。
            """
        
        prompt = f"""
        分析考卷圖片。只擷取【高中物理】試題。
        
        輸出 JSON List:
        [
            {{
                "number": 1,
                "type": "Single/Multi/Fill",
                "content": "題目文字...",
                "options": ["(A)...", "(B)..."],
                "answer": "A",
                "chapter": "從此選: {chapters_str}",
                "full_question_box_2d": [ymin, xmin, ymax, xmax],
                "box_2d": [ymin, xmin, ymax, xmax], 
                "page_index": 0 
            }}
        ]
        {extra_instruction}
        """

        input_parts = [prompt]
        input_parts.extend(batch_imgs)

        generation_config = {"response_mime_type": "application/json"}
        response = None
        last_error = None
        
        for model_name in candidate_models:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(input_parts, generation_config=generation_config)
                break
            except Exception as e:
                last_error = e
                continue
        
        if not response:
             errors.append(f"Batch {batch_idx+1} failed: {str(last_error)}")
             continue

        try:
            if not response.text:
                errors.append(f"Batch {batch_idx+1}: Empty response")
                continue

            json_text = clean_json_string(response.text)
            data = json.loads(json_text)
            if isinstance(data, dict): data = [data]
            
            for item in data:
                # 關鍵字過濾
                content_text = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
                if any(ek in content_text for ek in EXCLUDE_KEYWORDS):
                    continue 

                diagram_bytes = None
                ref_bytes = None
                full_page_bytes = None 
                
                # 針對 PDF 進行截圖
                if file_type == 'pdf':
                    try:
                        # 預設使用 page_index，若無則使用 0 (Batch 第一頁) 確保有圖
                        local_idx = item.get('page_index', 0)
                        
                        # 邊界檢查
                        if not isinstance(local_idx, int) or local_idx < 0: local_idx = 0
                        if local_idx >= len(batch_imgs): local_idx = 0

                        absolute_idx = start_page_idx + local_idx
                        
                        if 0 <= absolute_idx < len(source_images):
                            src_img = source_images[absolute_idx]
                            
                            # [修正] 無論如何都儲存整頁，作為手動截圖的底圖
                            full_page_bytes = img_to_bytes(src_img)
                            
                            if 'box_2d' in item:
                                diagram_bytes = crop_image(src_img, item['box_2d'], force_full_width=False, padding_y=5)
                                
                            if 'full_question_box_2d' in item:
                                ref_bytes = crop_image(src_img, item['full_question_box_2d'], force_full_width=True, padding_y=50)
                            else:
                                # 若 AI 沒回傳精準邊界，預設使用整頁作為參考圖
                                ref_bytes = full_page_bytes
                                
                    except Exception as e:
                        print(f"Image crop error: {e}")
                
                cand = SmartQuestionCandidate(
                    raw_text=item.get('content', ''),
                    question_number=item.get('number', 0),
                    options=item.get('options', []),
                    chapter=item.get('chapter', '未分類'),
                    is_likely=True,
                    status_reason=f"Batch {batch_idx+1}",
                    image_bytes=diagram_bytes,      
                    ref_image_bytes=ref_bytes,
                    full_page_bytes=full_page_bytes, # 確保這裡傳入了圖片資料
                    q_type=item.get('type', 'Single'),
                    subject='Physics' 
                )
                cand.content = item.get('content', '')
                all_candidates.append(cand)
                
        except Exception as e:
            errors.append(f"Batch {batch_idx+1} processing error: {str(e)}")
            
        time.sleep(1) 

    if not all_candidates and errors:
        return {"error": f"分析失敗詳情: {'; '.join(errors)}"}
        
    all_candidates.sort(key=lambda x: x.number)
    return all_candidates

def parse_raw_file(file_obj, file_type, use_ocr=False):
    return []
