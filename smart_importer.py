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
        self.image_bytes = image_bytes      # 這是題目本身附圖 (已裁切)
        self.ref_image_bytes = ref_image_bytes # 這是題目區域截圖 (供參考)
        self.full_page_bytes = full_page_bytes # [新增] 這是整頁原始圖 (供手動裁切用)
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
    """
    裁切圖片
    force_full_width: 是否強制寬度為整張圖片 (解決右側被切掉的問題)
    """
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
        cropped.save(img_byte_arr, format='JPEG')
        return img_byte_arr.getvalue()
    except Exception as e:
        print(f"Crop failed: {e}")
        return None

def img_to_bytes(pil_img):
    """將 PIL Image 轉為 bytes"""
    if pil_img is None: return None
    img_byte_arr = io.BytesIO()
    # 轉為 RGB 避免 RGBA 存成 JPEG 報錯
    if pil_img.mode in ("RGBA", "P"): 
        pil_img = pil_img.convert("RGB")
    pil_img.save(img_byte_arr, format='JPEG')
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
            1. 'full_question_box_2d': 包含題號、題目文字、選項與圖片的完整區域。請盡量寬鬆，包含到上一題結束與下一題開始的空白處。
            2. 'box_2d': 如果該題有附圖(diagram)，請標示圖片的範圍；若無則省略。
            座標格式皆為 [ymin, xmin, ymax, xmax] (範圍 0-1000)。
            """
        
        prompt = f"""
        你是一個高中物理老師助理。這份試卷包含物理、化學、生物、地科試題。
        請詳細分析這 {len(batch_imgs)} 頁考卷圖片。
        
        【重要任務】：
        1. **僅擷取物理科試題** (Physics)。
        2. **嚴格過濾**：若題目屬於純化學 (Chemistry)、純生物 (Biology) 或純地球科學 (Earth Science)，請**絕對不要**寫入回傳的 JSON 清單中。直接忽略它們。
        3. 若為「跨科題」且包含物理概念，則可以保留。
        
        輸出格式：JSON List
        {extra_instruction}
        
        JSON 格式範例：
        [
            {{
                "number": 1,
                "subject": "Physics", 
                "type": "Single",
                "content": "題目敘述...",
                "options": ["(A)...", "(B)..."],
                "answer": "A",
                "chapter": "從此清單選擇: {chapters_str}",
                "full_question_box_2d": [100, 0, 300, 1000],
                "box_2d": [200, 500, 300, 700],
                "page_index": 0 
            }}
        ]
        注意：
        - page_index 代表該題目在「這批圖片」中的索引(從 0 開始)。
        - 即使是非物理題也要列出，但標記正確科目，方便後端過濾。
        """

        input_parts = [prompt]
        if file_type == 'docx':
            input_parts.append("請分析以下 Word 文件中的圖片與題目。")
        
        input_parts.extend(batch_imgs)

        # 2026年模型清單設定
        candidate_models = [
            "gemini-2.5-flash",    
            "gemini-2.5-pro",      
            "gemini-2.0-flash",    
            "gemini-1.5-pro"       
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
                if 'subject' in item and item['subject'].lower() not in ['physics', '物理']:
                    continue
                
                content_text = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
                physics_keywords = ["牛頓", "速度", "加速度", "電路", "磁場", "光學", "焦耳"]
                is_cross_subject = any(pk in content_text for pk in physics_keywords)
                if not is_cross_subject:
                    if any(ek in content_text for ek in EXCLUDE_KEYWORDS):
                        continue 

                diagram_bytes = None
                ref_bytes = None
                full_page_bytes = None # 儲存整頁圖片
                
                if file_type == 'pdf' and 'page_index' in item:
                    try:
                        local_idx = item['page_index']
                        absolute_idx = start_page_idx + local_idx
                        
                        if 0 <= absolute_idx < len(source_images):
                            src_img = source_images[absolute_idx]
                            
                            # 儲存整頁，供前端裁切使用
                            full_page_bytes = img_to_bytes(src_img)
                            
                            if 'box_2d' in item:
                                diagram_bytes = crop_image(src_img, item['box_2d'], force_full_width=False, padding_y=5)
                                
                            if 'full_question_box_2d' in item:
                                ref_bytes = crop_image(src_img, item['full_question_box_2d'], force_full_width=True, padding_y=50)
                            else:
                                # 如果沒有回傳範圍，預設使用整頁
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
                    full_page_bytes=full_page_bytes, # 傳遞整頁圖片
                    q_type=item.get('type', 'Single'),
                    subject='Physics' 
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
