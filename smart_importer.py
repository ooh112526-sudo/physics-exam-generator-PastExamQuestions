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
# 常數定義 (符合技術規格)
# ==========================================
# 批次大小：每 5 頁一批，避免 Token Limit 或 Timeout
BATCH_SIZE = 5

# 圖片處理參數
IMG_DPI = 100        # 平衡清晰度與記憶體 (規格: 100-150)
IMG_QUALITY = 85     # JPEG 壓縮品質 (規格: 85)

PHYSICS_CHAPTERS_LIST = [
    "未分類", "第一章.科學的態度與方法", "第二章.物體的運動", 
    "第三章. 物質的組成與交互作用", "第四章.電與磁的統一", 
    "第五章. 能　量", "第六章.量子現象"
]

# 排除非物理科的關鍵字
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

def crop_image(original_img, box_2d, force_full_width=False, padding_y=10):
    """
    智慧截圖邏輯：只儲存題目區域，不重複儲存整頁
    """
    if not box_2d or len(box_2d) != 4: return None
    width, height = original_img.size
    ymin, xmin, ymax, xmax = box_2d
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    
    if force_full_width:
        left, right = 0, width
    else:
        # 左右保留一點 padding
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
    # 符合規格: JPEG Quality 85
    pil_img.save(img_byte_arr, format='JPEG', quality=IMG_QUALITY)
    return img_byte_arr.getvalue()

# ==========================================
# 核心邏輯
# ==========================================
def convert_file_to_images(file_bytes, file_type):
    """
    OOM 防護：將 PDF 轉為圖片列表 (PIL Images)
    """
    if file_type == 'pdf':
        if not HAS_PDF2IMAGE: return None, "缺少 pdf2image (Poppler)"
        try:
            # 符合規格: DPI 100-150
            return convert_from_bytes(file_bytes, dpi=IMG_DPI, fmt='jpeg'), None
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
            return images, None
        except Exception as e:
            return None, f"Word 解析失敗: {str(e)}"
    return None, "不支援的格式"

def process_single_batch(batch_images, batch_index, api_key, start_page_idx):
    """
    處理單一批次 (5張圖)
    """
    if not HAS_GENAI: return None, "缺少 google-generativeai"
    if not api_key: return None, "缺少 API Key"

    try:
        genai.configure(api_key=api_key)
        
        prompt_chapters = [c for c in PHYSICS_CHAPTERS_LIST if c != "未分類"]
        chapters_str = "\n".join(prompt_chapters)
        
        # 智慧判題規則 Prompt
        prompt = f"""
        你是一個高中物理題庫分析專家。請分析圖片中的試題。
        
        【判題規則】
        1. 題型 (type): 
           - 內容含 "應選x項" 或 "(A)(B)(C)(D)(E)" 且暗示多選 -> "Multi"
           - 無選項或只有底線填空 -> "Fill"
           - 題組共用敘述 -> "Group"
           - 否則 -> "Single"
        2. 座標 (box_2d): [ymin, xmin, ymax, xmax] (0-1000)
        3. 章節 (chapter): 從以下列表選擇最接近的: {chapters_str}

        【輸出格式】JSON List:
        [
            {{
                "number": 1, "type": "Single", "content": "題目文字...", "options": ["(A)..", "(B).."], "answer": "A",
                "chapter": "...", "full_question_box_2d": [y1, 0, y2, 1000], "page_index": 0 
            }},
            {{
                "number": 2, "type": "Group", "content": "題組文章...",
                "sub_questions": [{{ "number": 2, "content": "...", "type": "Single", "answer": "C" }}],
                "full_question_box_2d": [y1, 0, y2, 1000], "page_index": 0
            }}
        ]
        """
        
        input_parts = [prompt]
        input_parts.extend(batch_images)
        
        generation_config = {"response_mime_type": "application/json"}
        
        # 模型策略：優先 Gemini 2.5 -> 2.0 -> 1.5
        response = None
        last_error = None
        model_priority = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"]
        
        for model_name in model_priority:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(input_parts, generation_config=generation_config)
                break # 成功則跳出
            except Exception as e:
                last_error = e
                print(f"Model {model_name} failed: {e}")
                continue
        
        if not response: return None, f"AI 連線失敗 (所有模型皆嘗試過): {last_error}"
        if not response.text: return None, "AI 回傳空內容"

        json_text = clean_json_string(response.text)
        data = json.loads(json_text)
        if isinstance(data, dict): data = [data]
        
        processed_candidates = []
        for item in data:
            # 關鍵字過濾
            content_text = (item.get('content', '') + " " + " ".join(item.get('options', []))).lower()
            if any(ek in content_text for ek in EXCLUDE_KEYWORDS): continue 

            # 二次確認題型 (補強 Prompt 判斷)
            q_type = item.get('type', 'Single')
            if "應選" in content_text and ("項" in content_text or "二" in content_text): q_type = "Multi"
            if q_type != "Group" and not item.get('options'): q_type = "Fill"
            item['type'] = q_type

            # 圖片裁切處理
            try:
                local_idx = item.get('page_index', 0)
                if isinstance(local_idx, int) and 0 <= local_idx < len(batch_images):
                    src_img = batch_images[local_idx]
                    
                    # 1. 整頁圖 (轉 Base64 儲存) - 這裡僅存 B64，CloudManager 會轉 URL
                    full_page_bytes = img_to_bytes(src_img)
                    if full_page_bytes:
                        item['full_page_b64'] = base64.b64encode(full_page_bytes).decode('utf-8')
                    
                    # 2. 附圖 (box_2d)
                    if 'box_2d' in item:
                        c_bytes = crop_image(src_img, item['box_2d'])
                        if c_bytes: item['image_b64'] = base64.b64encode(c_bytes).decode('utf-8')
                    
                    # 3. 截圖範圍 (full_question_box_2d)
                    if 'full_question_box_2d' in item:
                        r_bytes = crop_image(src_img, item['full_question_box_2d'], force_full_width=True, padding_y=150)
                        if r_bytes: item['ref_image_b64'] = base64.b64encode(r_bytes).decode('utf-8')
                    else:
                        # Fallback
                        if 'full_page_b64' in item: item['ref_image_b64'] = item['full_page_b64']

            except Exception as e: print(f"Crop Error: {e}")

            processed_candidates.append(item)
            
        return processed_candidates, None

    except Exception as e:
        return None, str(e)
