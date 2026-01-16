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
    "第一章.科學的態度與方法", "第二章.物體的運動", "第三章. 物質的組成與交互作用",
    "第四章.電與磁的統一", "第五章. 能　量", "第六章.量子現象"
]

# ==========================================
# 候選題目物件
# ==========================================
class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number, options=None, chapter="未分類", is_likely=True, status_reason="", image_bytes=None):
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text 
        self.options = options if options else []
        self.predicted_chapter = chapter
        self.is_physics_likely = is_likely
        self.status_reason = status_reason
        self.image_bytes = image_bytes

# ==========================================
# 工具函式
# ==========================================
def clean_json_string(json_str):
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    
    # 尋找 JSON Array 的起點與終點
    start = json_str.find('[')
    end = json_str.rfind(']')
    if start != -1 and end != -1:
        json_str = json_str[start:end+1]
    return json_str.strip()

def crop_image(original_img, box_2d):
    """
    根據 Gemini 回傳的 [ymin, xmin, ymax, xmax] (0-1000) 裁切圖片
    """
    if not box_2d or len(box_2d) != 4: return None
    
    width, height = original_img.size
    ymin, xmin, ymax, xmax = box_2d
    
    left = (xmin / 1000) * width
    top = (ymin / 1000) * height
    right = (xmax / 1000) * width
    bottom = (ymax / 1000) * height
    
    try:
        cropped = original_img.crop((left, top, right, bottom))
        img_byte_arr = io.BytesIO()
        # 轉為 RGB 以防 RGBA 存為 JPEG 出錯
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

    input_parts = []
    source_images = []

    # === 1. 處理輸入檔案 ===
    if file_type == 'pdf':
        if not HAS_PDF2IMAGE: return {"error": "缺少 pdf2image (Poppler) 未安裝"}
        try:
            # 轉圖片 (取前 10 頁)
            pil_images = convert_from_bytes(file_bytes, dpi=200, fmt='jpeg')[:10]
            source_images = pil_images
            input_parts.extend(pil_images)
        except Exception as e:
            return {"error": f"PDF 轉圖片失敗: {str(e)}"}
            
    elif file_type == 'docx':
        if not HAS_DOCX: return {"error": "缺少 python-docx 套件"}
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            full_text = "\n".join([p.text for p in doc.paragraphs])
            input_parts.append(f"這是 Word 文件的純文字內容:\n{full_text}\n\n")
            
            # 提取 Word 內嵌圖片
            for rel in doc.part.rels.values():
                if "image" in rel.target_ref:
                    img_bytes = rel.target_part.blob
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    source_images.append(pil_img)
                    input_parts.append(pil_img)
        except Exception as e:
            return {"error": f"Word 解析失敗: {str(e)}"}
    else:
        return {"error": "僅支援 PDF 或 Word 檔"}

    if not input_parts: return {"error": "檔案內容為空"}

    # === 2. 建構 Prompt ===
    chapters_str = "\n".join(PHYSICS_CHAPTERS_LIST)
    
    extra_instruction = ""
    if file_type == 'pdf':
        extra_instruction = "如果題目附有圖片，請提供該圖片在頁面中的座標 'box_2d': [ymin, xmin, ymax, xmax] (範圍 0-1000)。"
    
    prompt = f"""
    你是一個高中物理老師助理。請分析輸入的考卷內容。
    目標：
    1. 辨識所有「物理科」試題。
    2. 回傳 JSON List。
    3. {extra_instruction}
    
    JSON 格式範例：
    [
        {{
            "number": 1,
            "content": "題目敘述...",
            "options": ["(A)...", "(B)..."],
            "answer": "A",
            "chapter": "從此清單選擇: {chapters_str}",
            "box_2d": [0, 0, 500, 500],
            "page_index": 0 
        }}
    ]
    注意：page_index 代表該題目所在的圖片索引(從 0 開始)。
    """
    
    full_prompt = [prompt] + input_parts

    # === 3. 呼叫模型 (使用您帳號實際可用的清單) ===
    candidate_models = [
        "gemini-2.5-flash",       # 首選
        "gemini-2.5-pro",         # 次選
        "gemini-2.0-flash",       # 備用
        "gemini-flash-latest",    # 通用別名
        "gemini-1.5-flash"        # 最後手段
    ]
    
    # 強制 JSON 模式
    generation_config = {"response_mime_type": "application/json"}
    
    response = None
    last_error = None
    used_model = "Unknown"

    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(full_prompt, generation_config=generation_config)
            used_model = model_name
            break
        except Exception as e:
            last_error = e
            continue

    if not response:
        return {"error": f"所有 AI 模型嘗試皆失敗。最後錯誤: {str(last_error)}"}

    # === 4. 解析結果 ===
    try:
        json_text = clean_json_string(response.text)
        data = json.loads(json_text)
        if isinstance(data, dict): data = [data]
        
        candidates = []
        for item in data:
            # 處理 PDF 截圖
            cropped_bytes = None
            if file_type == 'pdf' and 'box_2d' in item and 'page_index' in item:
                try:
                    idx = item['page_index']
                    bbox = item['box_2d']
                    if 0 <= idx < len(source_images):
                        cropped_bytes = crop_image(source_images[idx], bbox)
                except: pass
            
            cand = SmartQuestionCandidate(
                raw_text=item.get('content', ''),
                question_number=item.get('number', 0),
                options=item.get('options', []),
                chapter=item.get('chapter', '未分類'),
                is_likely=True,
                status_reason=f"Gemini AI ({used_model})",
                image_bytes=cropped_bytes
            )
            cand.content = item.get('content', '')
            candidates.append(cand)
        return candidates

    except Exception as e:
        return {"error": f"結果解析失敗: {str(e)}。Model: {used_model}"}

def parse_raw_file(file_obj, file_type, use_ocr=False):
    # 保留舊邏輯介面
    return []
