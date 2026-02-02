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
BATCH_SIZE = 5
IMG_DPI = 100        # 降低 DPI 以節省記憶體
IMG_QUALITY = 85

PHYSICS_CHAPTERS_LIST = [
    "未分類", "第一章.科學的態度與方法", "第二章.物體的運動", 
    "第三章. 物質的組成與交互作用", "第四章.電與磁的統一", 
    "第五章. 能　量", "第六章.量子現象"
]

EXCLUDE_KEYWORDS = [
    "化學", "反應式", "有機化合物", "酸鹼", "沉澱", "氧化還原", "莫耳", "原子量",
    "生物", "細胞", "遺傳", "DNA", "染色體", "演化", "生態", "光合作用", "酵素",
    "地科", "地質", "板塊", "洋流", "大氣", "氣候", "岩石", "化石", "星系", "地層"
]

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
    [新功能] 檢查文字是否包含題組關鍵字 (如: 16-18題為題組)
    回傳: (是否為題組, 題號範圍字串)
    """
    # 常見格式：16.~18.題為題組, 16-18為題組, 16~18 題為題組
    # Regex 抓取: 數字 + (波浪/連折) + 數字 + 任意字 + 題組
    pattern = r'(\d+)\s*[.~～-]\s*(\d+)\s*.*題?為?題組'
    match = re.search(pattern, text)
    if match:
        return True, f"{match.group(1)}~{match.group(2)}"
    return False, ""

def crop_image(original_img, box_2d, force_full_width=False, padding_y=10):
    if not box_2d or len(box_2d) != 4: return None
    width, height = original_img.size
    ymin, xmin, ymax, xmax = box_2d
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    
    if force_full_width:
        left, right = 0, width
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
        return img_to_bytes(cropped)
    except: return None

def img_to_bytes(pil_img):
    if pil_img is None: return None
    img_byte_arr = io.BytesIO()
    if pil_img.mode in ("RGBA", "P"): pil_img = pil_img.convert("RGB")
    pil_img.save(img_byte_arr, format='JPEG', quality=IMG_QUALITY)
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
            return convert_from_bytes(
                file_bytes, 
                dpi=IMG_DPI, 
                fmt='jpeg',
                first_page=first_page,
                last_page=last_page
            ), None
        except Exception as e:
            return None, f"PDF 轉圖失敗: {str(e)}"
    elif file_type == 'docx':
        if not HAS_DOCX: return None, "缺少 python-docx"
        images = []
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    img_bytes = rel.target_part.blob
                    images.append(Image.open(io.BytesIO(img_bytes)))
            
            if first_page and last_page:
                start = first_page - 1
                end = last_page
                if start < len(images):
                    return images[start:end], None
                else:
                    return [], None
            return images, None
        except Exception as e:
            return None, f"Word 解析失敗: {str(e)}"
    return None, "不支援的格式"

def process_single_batch(batch_images, batch_index, api_key, start_page_idx):
    if not HAS_GENAI: return None, "缺少 google-generativeai"
    if not api_key: return None, "缺少 API Key"

    try:
        genai.configure(api_key=api_key)
        prompt_chapters = [c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"]
        chapters_str = "\n".join(prompt_chapters)
        
        prompt = f"""
        你是一個高中物理題庫分析專家。請分析圖片中的試題。
        【判題規則】
        1. 題型 (type): 含 "應選x項" -> "Multi"; 無選項 -> "Fill"; 題組 -> "Group"; 否則 "Single"
        2. 座標 (box_2d): [ymin, xmin, ymax, xmax] (0-1000)
        3. 章節 (chapter): 選擇最接近的: {chapters_str}

        【輸出格式】JSON List:
        [
            {{
                "number": 1, "type": "Single", "content": "...", "options": ["(A).."], "answer": "A",
                "chapter": "...", "full_question_box_2d": [y1,0,y2,1000], "page_index": 0 
            }}
        ]
        """
        
        input_parts = [prompt] + batch_images
        generation_config = {"response_mime_type": "application/json"}
        
        models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"]
        response = None
        last_error = None
        
        for model_name in models:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(input_parts, generation_config=generation_config)
                break
            except Exception as e:
                last_error = e
                continue
        
        if not response: return None, f"AI Error: {last_error}"
        
        data = json.loads(clean_json_string(response.text))
        if isinstance(data, dict): data = [data]
        
        processed_candidates = []
        for item in data:
            content_text = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
            if any(ek in content_text for ek in EXCLUDE_KEYWORDS): continue 

            q_type = item.get('type', 'Single')
            if "應選" in content_text and ("項" in content_text or "二" in content_text): q_type = "Multi"
            if q_type != "Group" and not item.get('options'): q_type = "Fill"
            item['type'] = q_type

            # 圖片處理
            img_b64 = None
            ref_b64 = None
            full_b64 = None
            
            try:
                local_idx = item.get('page_index', 0)
                if isinstance(local_idx, int) and 0 <= local_idx < len(batch_images):
                    src_img = batch_images[local_idx]
                    
                    full_page_bytes = img_to_bytes(src_img)
                    if full_page_bytes: full_b64 = base64.b64encode(full_page_bytes).decode('utf-8')
                    
                    if 'box_2d' in item:
                        c_bytes = crop_image(src_img, item['box_2d'])
                        if c_bytes: img_b64 = base64.b64encode(c_bytes).decode('utf-8')
                    
                    if 'full_question_box_2d' in item:
                        r_bytes = crop_image(src_img, item['full_question_box_2d'], force_full_width=True, padding_y=150)
                        if r_bytes: ref_b64 = base64.b64encode(r_bytes).decode('utf-8')
                    else:
                        ref_b64 = full_b64 # Fallback
            except: pass
            
            # 建構回傳物件 (Dict)
            # [新] 加入 ai_crop_backup_b64 與 use_image 欄位
            cand = {
                "number": item.get('number', 0),
                "content": item.get('content', ''),
                "options": item.get('options', []),
                "answer": item.get('answer', ''),
                "chapter": item.get('chapter', '未分類'),
                "type": item['type'],
                "image_b64": img_b64,
                "ai_crop_backup_b64": img_b64, # 備份原始裁切
                "ref_image_b64": ref_b64,
                "full_page_b64": full_b64,
                "use_image": False, # 預設不使用
                "sub_questions": item.get('sub_questions', [])
            }
            processed_candidates.append(cand)
            
        return processed_candidates, None

    except Exception as e:
        return None, str(e)
