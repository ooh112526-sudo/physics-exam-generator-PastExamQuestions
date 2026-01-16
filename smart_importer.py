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

# ==========================================
# 候選題目物件
# ==========================================
class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number, options=None, chapter="未分類", 
                 is_likely=True, status_reason="", image_bytes=None, q_type="Single", ref_image_bytes=None):
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text 
        self.options = options if options else []
        self.predicted_chapter = chapter if chapter in PHYSICS_CHAPTERS_LIST else "未分類"
        self.is_physics_likely = is_likely
        self.status_reason = status_reason
        self.image_bytes = image_bytes
        self.ref_image_bytes = ref_image_bytes
        self.q_type = q_type

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

def crop_image(original_img, box_2d):
    if not box_2d or len(box_2d) != 4: return None
    
    width, height = original_img.size
    ymin, xmin, ymax, xmax = box_2d
    
    # 增加 padding 並確保不越界
    ymin = max(0, ymin - 10)
    ymax = min(1000, ymax + 10)
    xmin = max(0, xmin - 10)
    xmax = min(1000, xmax + 10)
    
    left = (xmin / 1000) * width
    top = (ymin / 1000) * height
    right = (xmax / 1000) * width
    bottom = (ymax / 1000) * height
    
    if right <= left or bottom <= top: return None

    try:
        cropped = original_img.crop((left, top, right, bottom))
        img_byte_arr = io.BytesIO()
        if cropped.mode in ("RGBA", "P"): cropped = cropped.convert("RGB")
        cropped.save(img_byte_arr, format='JPEG')
        return img_byte_arr.getvalue()
    except Exception as e:
        print(f"Crop failed: {e}")
        return None

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
            source_images = convert_from_bytes(file_bytes, dpi=200, fmt='jpeg')
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

    # 分批處理
    BATCH_SIZE = 5 
    total_pages = len(source_images)
    all_candidates = []
    errors = []

    if file_type == 'docx':
        batches = [source_images] 
    else:
        batches = [source_images[i:i + BATCH_SIZE] for i in range(0, total_pages, BATCH_SIZE)]

    prompt_chapters = [c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"]
    chapters_str = "\n".join(prompt_chapters)
    
    for batch_idx, batch_imgs in enumerate(batches):
        start_page_idx = batch_idx * BATCH_SIZE
        
        extra_instruction = ""
        if file_type == 'pdf':
            extra_instruction = """
            對於每一題，請回傳兩個座標範圍：
            1. 'full_question_box_2d': 包含題號、題目文字、選項與圖片的完整區域。
            2. 'box_2d': 如果該題有附圖(diagram)，請標示圖片的範圍；若無則省略。
            座標格式皆為 [ymin, xmin, ymax, xmax] (範圍 0-1000)。
            """
        
        prompt = f"""
        你是一個高中物理老師助理。請詳細分析這 {len(batch_imgs)} 頁考卷圖片。
        目標：
        1. 辨識所有「物理科」試題。
        2. 判斷題型：Single (單選), Multi (多選), Fill (填充)。
        3. {extra_instruction}
        
        JSON 格式範例：
        [
            {{
                "number": 1,
                "type": "Single",
                "content": "題目敘述...",
                "options": ["(A)...", "(B)..."],
                "answer": "A",
                "chapter": "從此清單選擇: {chapters_str}",
                "full_question_box_2d": [100, 100, 300, 900],
                "box_2d": [200, 500, 300, 700],
                "page_index": 0 
            }}
        ]
        注意：
        - 若無法判斷章節，請填寫 "未分類"。
        - page_index 代表該題目在「這批圖片」中的索引(從 0 開始)。
        """

        input_parts = [prompt]
        if file_type == 'docx':
            input_parts.append("請分析以下 Word 文件中的圖片與題目。")
        
        input_parts.extend(batch_imgs)

        # === 關鍵修正：將 gemini-2.5-flash 設為首選，並加入其他可用模型 ===
        candidate_models = [
            "gemini-2.5-flash",       # 首選
            "gemini-2.5-pro",         # 次選
            "gemini-2.0-flash",       # 備用
            "gemini-2.0-flash-exp",   # 備用
            "gemini-flash-latest"     # 最後備用
        ]
        
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
                # 嘗試不使用 JSON 模式 (有些舊模型不支援)
                if "mode" in str(e).lower() or "support" in str(e).lower():
                    try:
                        response = model.generate_content(input_parts)
                        if response: break
                    except: pass
                continue

        if not response:
            error_msg = str(last_error) if last_error else "Unknown error"
            errors.append(f"Batch {batch_idx+1} failed ({error_msg})")
            continue

        try:
            if not response.text:
                errors.append(f"Batch {batch_idx+1} blocked by safety filters.")
                continue

            json_text = clean_json_string(response.text)
            data = json.loads(json_text)
            if isinstance(data, dict): data = [data]
            
            for item in data:
                diagram_bytes = None
                ref_bytes = None
                
                if file_type == 'pdf' and 'page_index' in item:
                    try:
                        local_idx = item['page_index']
                        absolute_idx = start_page_idx + local_idx
                        
                        if 0 <= absolute_idx < len(source_images):
                            src_img = source_images[absolute_idx]
                            
                            if 'box_2d' in item:
                                diagram_bytes = crop_image(src_img, item['box_2d'])
                                
                            if 'full_question_box_2d' in item:
                                ref_bytes = crop_image(src_img, item['full_question_box_2d'])
                                
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
                    q_type=item.get('type', 'Single')
                )
                cand.content = item.get('content', '')
                all_candidates.append(cand)
                
        except Exception as e:
            errors.append(f"Batch {batch_idx+1} parsing error: {str(e)}")
            
        time.sleep(1)

    if not all_candidates and errors:
        return {"error": f"分析失敗詳情: {'; '.join(errors)}"}
        
    all_candidates.sort(key=lambda x: x.number)
    return all_candidates

def parse_raw_file(file_obj, file_type, use_ocr=False):
    return []
