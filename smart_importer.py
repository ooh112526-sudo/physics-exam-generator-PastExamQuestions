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
# 候選題目物件 (擴充子題支援)
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
        self.image_bytes = image_bytes      # 題目附圖 (已裁切)
        self.ref_image_bytes = ref_image_bytes # 題目區域截圖 (全寬度)
        self.full_page_bytes = full_page_bytes # 整頁原始圖 (供手動裁切用)
        self.q_type = q_type
        self.subject = subject
        self.sub_questions = sub_questions if sub_questions else [] # 支援題組

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
    
    # 應用 padding (上下擴展)
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    
    # 決定左右範圍
    if force_full_width:
        left = 0
        right = width
    else:
        # 一般附圖裁切，稍微加點 padding
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
        # 壓縮裁切圖 (70% 品質可大幅減少記憶體，且文字仍清晰)
        cropped.save(img_byte_arr, format='JPEG', quality=70)
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
    pil_img.save(img_byte_arr, format='JPEG', quality=70) 
    return img_byte_arr.getvalue()

# ==========================================
# Gemini AI 解析邏輯 (支援指定頁數)
# ==========================================
def parse_with_gemini(file_bytes, file_type, api_key, target_pages=None):
    """
    target_pages: tuple (start_page_idx, end_page_idx) 
                  例如 (0, 5) 代表處理第 0 到 4 頁 (不含 5)。若為 None 則處理全部。
    """
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
            # DPI 150
            source_images = convert_from_bytes(file_bytes, dpi=150, fmt='jpeg')
        except Exception as e:
            return {"error": f"PDF 轉圖片失敗: {str(e)}"}
            
    elif file_type == 'docx':
        # Docx 暫時不支援分頁邏輯，視為一整批
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

    # 處理指定頁數範圍
    start_offset = 0
    images_to_process = source_images
    
    if target_pages and file_type == 'pdf':
        start_p, end_p = target_pages
        # 邊界檢查
        start_p = max(0, start_p)
        end_p = min(len(source_images), end_p)
        
        if start_p < end_p:
            images_to_process = source_images[start_p:end_p]
            start_offset = start_p
        else:
            return {"error": "指定的頁數範圍無效"}

    # 這裡我們不再分批，因為 caller 會負責分批 (每次呼叫只傳 5 頁進來)
    batches = [images_to_process]
    
    prompt_chapters = [c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"]
    chapters_str = "\n".join(prompt_chapters)
    
    # 2026年模型清單
    candidate_models = [
        "gemini-2.5-flash",    
        "gemini-2.5-pro",      
        "gemini-2.0-flash",    
        "gemini-1.5-pro"       
    ]
    
    all_candidates = []
    errors = []

    for batch_idx, batch_imgs in enumerate(batches):
        
        extra_instruction = ""
        if file_type == 'pdf':
            extra_instruction = """
            【座標要求】：
            1. 'full_question_box_2d': 請框選該題目(含題號、文字、選項)的垂直範圍。x軸必須是全寬 [ymin, 0, ymax, 1000]。
            2. 'box_2d': 若有圖片，標示圖片範圍。
            3. 'page_index': 該題目位於本批次圖片的第幾頁 (0, 1, ...)。
            """
        
        prompt = f"""
        分析考卷圖片，只擷取【高中物理】試題。
        
        【判題規則】：
        1. 若題目包含「應選X項」或「應選x項」，type 請設為 "Multi" (多選)。
        2. 若題目沒有選項 (A,B,C,D...)，type 請設為 "Fill" (填充)。
        3. 若為題組題 (Group Question)，包含一段共用敘述與多個小題：
           - type 設為 "Group"。
           - 將共用敘述放在 "content"。
           - 將子題目放在 "sub_questions" 列表中 (格式同一般題目)。
        
        輸出 JSON List 格式範例:
        [
            {{
                "number": 1,
                "type": "Single", 
                "content": "題目文字...",
                "options": ["(A)...", "(B)..."],
                "answer": "A",
                "chapter": "從此選: {chapters_str}",
                "full_question_box_2d": [ymin, 0, ymax, 1000],
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
             errors.append(f"Batch failed: {str(last_error)}")
             continue

        try:
            if not response.text:
                errors.append("Batch: Empty response")
                continue

            json_text = clean_json_string(response.text)
            data = json.loads(json_text)
            if isinstance(data, dict): data = [data]
            
            for item in data:
                content_text = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
                if any(ek in content_text for ek in EXCLUDE_KEYWORDS):
                    continue 

                q_type = item.get('type', 'Single')
                if "應選" in content_text and ("項" in content_text or "二" in content_text or "三" in content_text):
                    q_type = "Multi"
                if q_type != "Group" and not item.get('options'):
                    q_type = "Fill"

                diagram_bytes = None
                ref_bytes = None
                full_page_bytes = None
                
                if file_type == 'pdf':
                    try:
                        local_idx = item.get('page_index', 0)
                        # 邊界檢查
                        if not isinstance(local_idx, int) or local_idx < 0 or local_idx >= len(batch_imgs):
                            local_idx = 0
                            
                        # 對應到原始圖片列表中的正確圖片 (因為 batch_imgs 是切片後的)
                        src_img = batch_imgs[local_idx] 
                        
                        full_page_bytes = img_to_bytes(src_img)
                        
                        if 'box_2d' in item:
                            diagram_bytes = crop_image(src_img, item['box_2d'], False, 5)
                        
                        if 'full_question_box_2d' in item:
                            ref_bytes = crop_image(src_img, item['full_question_box_2d'], True, 100)
                        else:
                            ref_bytes = full_page_bytes
                            
                    except Exception as e:
                        print(f"Crop error: {e}")

                cand = SmartQuestionCandidate(
                    raw_text=item.get('content', ''),
                    question_number=item.get('number', 0),
                    options=item.get('options', []),
                    chapter=item.get('chapter', '未分類'),
                    is_likely=True,
                    status_reason="AI",
                    image_bytes=diagram_bytes,      
                    ref_image_bytes=ref_bytes,
                    full_page_bytes=full_page_bytes,
                    q_type=q_type,
                    subject='Physics',
                    sub_questions=item.get('sub_questions', [])
                )
                cand.content = item.get('content', '')
                all_candidates.append(cand)
                
        except Exception as e:
            errors.append(f"解析錯誤: {str(e)}")

    if not all_candidates and errors:
        return {"error": "; ".join(errors)}
        
    # 確保 number 為整數後排序
    try:
        all_candidates.sort(key=lambda x: int(x.number))
    except: pass
    
    return all_candidates

def parse_raw_file(file_obj, file_type, use_ocr=False):
    return []
