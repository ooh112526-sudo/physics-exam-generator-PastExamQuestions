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
    
    # 1. 高度調整：上下擴張 padding_y，確保不切到文字
    ymin = max(0, ymin - padding_y)
    ymax = min(1000, ymax + padding_y)
    
    # 2. 寬度調整
    if force_full_width:
        # 強制全寬：左右直接切齊頁面邊緣 (0~1000)
        left, right = 0, width
    else:
        # 非全寬：左右依照座標並微調
        xmin = max(0, xmin - 10)
        xmax = min(1000, xmax + 10)
        left = (xmin / 1000) * width
        right = (xmax / 1000) * width
    
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
        
        # [核心修改] 優化 Prompt 以支援「全域題號」與「簡答題」的題組辨識
        prompt = f"""
        你是一個高中物理題庫分析專家，請分析圖片中的高中物理試題，並將其轉為 JSON 格式。
        
        【關鍵任務：精準辨識題組結構】
        1. 偵測題組 (Group)：
           - 當看到「第X-Y題為題組」或「X-Y題為題組」時，這是一個 Group 母題。
           - 題組範圍：從標題開始，包含共用文章、共用圖片，直到**第一個子題題號**出現之前，都屬於母題的 content。
        
        2. 處理子題目 (sub_questions) - 這是最容易出錯的地方，請仔細執行：
           - **情況 A (全域題號)**：若標題是「40-42題為題組」，則必須在下方尋找 **40.**, **41.**, **42.** 開頭的段落。這些就是子題目，**嚴禁**將它們合併到母題內容中。
           - **情況 B (局部題號)**：若標題無具體題號，或子題以 (1), (2) 開頭，則以 (1), (2) 為子題目。
           - **絕對禁止**遺漏子題目。若標題說 40-42，sub_questions 列表裡必須要有 3 個物件 (40, 41, 42)。

        3. 圖片歸屬規則：
           - 若圖片位於所有子題題號之前 --> 歸屬母題 (content)。
           - 若圖片位於特定子題文字之間 --> 歸屬該子題。
           - **針對並排圖片：如圖15、圖16位於文字下方、第40題上方，因此這兩張圖屬於母題 content。**

        4. 輸出格式要求 (JSON)：
           - Group 母題：type="Group", content="共用文案...", sub_questions=[...]
           - 子題目：type="Single"(有選項)/"Multi"(多選)/"Fill"(無選項/簡答/計算/作圖), number=題號(整數), content="題目敘述", options=[], answer=""
           - box_2d: 母題框選包含所有子題的大範圍；子題框選各自範圍。

        5. 章節 (chapter): 選擇最接近的: {chapters_str}

        【JSON 輸出範例 - 全域題號題組】：
        [
            {{
                "number": 40, 
                "type": "Group", 
                "content": "40-42題為題組\\n19世紀末實驗發現... (包含圖15, 圖16)", 
                "sub_questions": [
                    {{ "number": 40, "type": "Single", "content": "40. 有一紅光雷射...", "options": ["(A)...", "(E)..."], "answer": "A" }},
                    {{ "number": 41, "type": "Fill", "content": "41. 如圖15...", "options": [], "answer": "" }},
                    {{ "number": 42, "type": "Fill", "content": "42. 圖16為光電效應...", "options": [], "answer": "" }}
                ],
                "box_2d": [100, 0, 900, 1000]
            }}
        ]
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
            
            # 自動判斷
            q_type = item.get('type', 'Single')
            if "應選" in content and ("項" in content or "二" in content): q_type = "Multi"
            if q_type != "Group" and not item.get('options'): q_type = "Fill"
            
            # 圖片裁切
            img_b, ref_b, full_b = None, None, None
            try:
                idx = item.get('page_index', 0)
                if isinstance(idx, int) and 0 <= idx < len(batch_images):
                    src = batch_images[idx]
                    full_b = img_to_bytes(src)
                    
                    if 'box_2d' in item: 
                        # [確認]: 使用 force_full_width=True 確保寬度正確
                        img_b = crop_image(src, item['box_2d'], force_full_width=True, padding_y=10)
                    
                    if 'full_question_box_2d' in item:
                        # 參考圖原本就是全寬，且上下緩衝較大 (150)
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
            processed.append(cand)
            
        return processed, None

    except Exception as e: return None, str(e)
