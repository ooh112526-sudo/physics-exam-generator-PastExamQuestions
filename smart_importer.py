import re
import io
import json
import time

# ==========================================
# 依賴套件與環境檢查
# ==========================================
HAS_GENAI = False
HAS_PDF2IMAGE = False
HAS_OCR = False

try:
    import google.generativeai as genai
    HAS_GENAI = True
except ImportError:
    pass

try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError:
    pass

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    pass

# ==========================================
# 供 app.py 呼叫的狀態檢查函式
# ==========================================
def is_ocr_available():
    return HAS_PDF2IMAGE and HAS_OCR

# ==========================================
# 常數定義
# ==========================================
PHYSICS_CHAPTERS_LIST = [
    "第一章.科學的態度與方法", "第二章.物體的運動", "第三章. 物質的組成與交互作用",
    "第四章.電與磁的統一", "第五章. 能　量", "第六章.量子現象"
]

EXCLUDE_KEYWORDS = ["化學", "生物", "地科", "反應式", "細胞", "遺傳", "地質", "氣候"]

CHAPTER_KEYWORDS = {
    "第一章.科學的態度與方法": ["單位", "因次", "SI制", "測量", "國際單位"],
    "第二章.物體的運動": ["速度", "加速度", "牛頓", "拋體", "摩擦力", "萬有引力", "自由落體"],
    "第三章. 物質的組成與交互作用": ["強力", "弱力", "重力", "電磁力", "夸克", "原子核"],
    "第四章.電與磁的統一": ["電流", "電壓", "磁場", "安培", "法拉第", "電磁感應", "折射", "干涉"],
    "第五章. 能　量": ["動能", "位能", "守恆", "作功", "功率", "力學能", "核能", "焦耳"],
    "第六章.量子現象": ["光電效應", "光子", "波粒二象性", "物質波", "能階", "光譜", "量子"]
}

# ==========================================
# 候選題目物件
# ==========================================
class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number, options=None, chapter="未分類", is_likely=True, status_reason=""):
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text 
        self.options = options if options else []
        self.predicted_chapter = chapter
        self.is_physics_likely = is_likely
        self.status_reason = status_reason

        if not options and status_reason != "Gemini AI 辨識":
            self._parse_structure()
            if chapter == "未分類":
                self._predict_classification()

    def _parse_structure(self):
        opt_pattern = re.compile(r'\s*[\(（]?[A-Ea-e][\)）][\.\、\．]?\s+')
        match = opt_pattern.search(self.raw_text)
        if match:
            self.content = self.raw_text[:match.start()].strip()
            opts_text = self.raw_text[match.start():]
            temp_text = opt_pattern.sub(lambda m: f"|||{m.group().strip()}|||", opts_text)
            parts = temp_text.split('|||')
            current_opt = ""
            for p in parts:
                if not p.strip(): continue
                if re.match(r'^[\(（]?[A-Ea-e][\)）][\.\、\．]?$', p.strip()):
                    if current_opt: self.options.append(current_opt)
                    current_opt = "" 
                else:
                    current_opt = p.strip()
            if current_opt: self.options.append(current_opt)
        else:
            self.content = self.raw_text.strip()

    def _predict_classification(self):
        text_for_search = self.content + " " + " ".join(self.options)
        for chap, kws in CHAPTER_KEYWORDS.items():
            if any(k in text_for_search for k in kws):
                self.predicted_chapter = chap
                self.status_reason = "關鍵字命中"
                break

# ==========================================
# Gemini AI 解析邏輯
# ==========================================
def clean_json_string(json_str):
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    return json_str.strip()

def parse_with_gemini(file_bytes, file_type, api_key):
    if not HAS_GENAI: return {"error": "缺少 google-generativeai 套件"}
    if not HAS_PDF2IMAGE: return {"error": "缺少 pdf2image 套件 (Poppler 未安裝)"}
    if not api_key: return {"error": "請輸入 API Key"}

    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        return {"error": f"API Key 設定失敗: {str(e)}"}

    images = []
    try:
        if file_type == 'pdf':
            images = convert_from_bytes(file_bytes, dpi=200, fmt='jpeg')[:10]
        else:
            return {"error": "目前僅支援 PDF"}
    except Exception as e:
        return {"error": f"PDF 轉圖片失敗 (Poppler問題?): {str(e)}"}

    if not images: return {"error": "PDF 頁面為空"}

    # Prompt
    chapters_str = "\n".join(PHYSICS_CHAPTERS_LIST)
    prompt = f"""
    你是一個高中物理老師助理。請分析試卷圖片。
    目標：
    1. 辨識所有「物理科」試題 (忽略化學/生物)。
    2. 回傳 JSON List。
    
    格式範例：
    [
        {{
            "number": 1,
            "content": "題目敘述...",
            "options": ["(A)...", "(B)..."],
            "answer": "A",
            "chapter": "從此清單選擇: {chapters_str}"
        }}
    ]
    """
    
    input_parts = [prompt]
    input_parts.extend(images)

    # === 真實世界可用的模型清單 ===
    # 注意：Gemini 3.0/2.0 在真實伺服器尚未發布，必須使用 1.5 系列
    candidate_models = [
        "gemini-1.5-flash",       # 目前最穩定且快速
        "gemini-1.5-pro",         # 效能較強
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro-latest"
    ]

    response = None
    last_error = None
    used_model = "Unknown"

    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(input_parts)
            used_model = model_name
            break 
        except Exception as e:
            last_error = e
            continue

    if not response:
        # 如果所有已知模型都失敗，嘗試列出該 Key 可用的模型名稱以供除錯
        available_models = []
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    available_models.append(m.name)
        except:
            available_models = ["無法取得列表"]

        return {"error": f"所有模型皆失敗。最後錯誤: {str(last_error)}。您的 Key 可用模型: {', '.join(available_models)}"}

    try:
        try:
            text = response.text
        except ValueError:
            return {"error": f"Gemini ({used_model}) 拒絕回應"}

        json_text = clean_json_string(text)
        data = json.loads(json_text)
        
        candidates = []
        for item in data:
            cand = SmartQuestionCandidate(
                raw_text=item.get('content', ''),
                question_number=item.get('number', 0),
                options=item.get('options', []),
                chapter=item.get('chapter', '未分類'),
                is_likely=True,
                status_reason=f"Gemini AI ({used_model})"
            )
            cand.content = item.get('content', '')
            candidates.append(cand)
        return candidates

    except Exception as e:
        return {"error": f"Gemini ({used_model}) 執行錯誤: {str(e)}"}

# ==========================================
# 傳統 OCR 邏輯 (備用)
# ==========================================
def parse_raw_file(file_obj, file_type, use_ocr=False):
    full_text = ""
    file_obj.seek(0)
    file_bytes = file_obj.read()
    
    if use_ocr and file_type == 'pdf' and HAS_PDF2IMAGE and HAS_OCR:
        try:
            images = convert_from_bytes(file_bytes, dpi=200)[:5]
            for img in images:
                full_text += pytesseract.image_to_string(img, lang='chi_tra+eng') + "\n"
        except: pass
    elif file_type == 'pdf':
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for p in pdf.pages:
                    full_text += (p.extract_text() or "") + "\n"
        except: pass
        
    candidates = []
    lines = full_text.split('\n')
    pattern = re.compile(r'^\s*[\(（]?(\d+)[\)）]?[\.\、]')
    
    curr_q = None
    for line in lines:
        match = pattern.match(line)
        if match:
            if curr_q: candidates.append(curr_q)
            curr_q = SmartQuestionCandidate(line, int(match.group(1)), status_reason="Regex")
        elif curr_q:
            curr_q.raw_text += "\n" + line
            curr_q.content += "\n" + line
            
    if curr_q: candidates.append(curr_q)
    return candidates
