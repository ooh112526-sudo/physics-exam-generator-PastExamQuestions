import re
import pdfplumber
import docx
import io
import json
import time
import sys

# 嘗試匯入 OCR 與 Google AI 套件
# 若環境未安裝，OCR_AVAILABLE 會變成 False，部分功能將停用
try:
    import pytesseract
    from pdf2image import convert_from_bytes
    from PIL import Image
    import google.generativeai as genai
    from google.api_core import retry
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ==========================================
# 常數定義
# ==========================================

PHYSICS_CHAPTERS_LIST = [
    "第一章.科學的態度與方法",
    "第二章.物體的運動",
    "第三章. 物質的組成與交互作用",
    "第四章.電與磁的統一",
    "第五章. 能　量",
    "第六章.量子現象"
]

# (以下為舊版 Regex 邏輯需要的關鍵字，保留以供備用模式使用)
EXCLUDE_KEYWORDS = [
    "化學", "反應式", "莫耳", "有機", "細胞", "遺傳", "DNA", "生態", "地質", "氣候", 
    "酸鹼", "沉澱", "氧化還原", "生物", "染色體", "演化", "板塊", "洋流", "試管",
    "葉綠素", "酵素", "粒線體", "北極冰蓋", "地層", "岩石", "地心引力", "月球"
]

CHAPTER_KEYWORDS = {
    "第一章.科學的態度與方法": ["單位", "因次", "SI制", "有效數字", "誤差", "測量", "國際單位"],
    "第二章.物體的運動": ["速度", "加速度", "位移", "牛頓", "運動定律", "拋體", "斜面", "摩擦力", "萬有引力", "克卜勒", "自由落體", "衝量", "動量"],
    "第三章. 物質的組成與交互作用": ["強力", "弱力", "重力", "電磁力", "夸克", "原子核", "基本粒子", "交互作用"],
    "第四章.電與磁的統一": ["電流", "電壓", "電阻", "磁場", "安培", "法拉第", "電磁感應", "透鏡", "折射", "反射", "干涉", "繞射", "都卜勒", "電磁波", "光電效應"],
    "第五章. 能　量": ["動能", "位能", "守恆", "作功", "功率", "力學能", "核能", "質能", "熱功當量", "焦耳"],
    "第六章.量子現象": ["光電效應", "光子", "波粒二象性", "物質波", "德布羅意", "能階", "光譜", "黑體輻射", "量子"]
}

PHYSICS_GENERAL_KEYWORDS = [
    "物體", "粒子", "系統", "軌跡", "圖形", "數據", "實驗", "裝置", "觀察", "現象", "波長", "頻率"
]

# ==========================================
# 候選題目物件
# ==========================================

class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number, options=None, chapter="未分類", is_likely=True, status_reason=""):
        self.raw_text = raw_text
        self.number = question_number
        self.content = raw_text # 預設內容
        self.options = options if options else []
        self.predicted_chapter = chapter
        self.is_physics_likely = is_likely
        self.status_reason = status_reason

        # 如果是傳統 Regex 模式建立的，可能需要解析結構
        if not options and status_reason != "Gemini AI 辨識":
            self._parse_structure()
            if chapter == "未分類":
                self._predict_classification()

    def _parse_structure(self):
        """(Regex模式用) 解析題目結構：分離選項與題幹"""
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
        """(Regex模式用) 關鍵字預測分類"""
        text_for_search = self.content + " " + " ".join(self.options)
        
        # 1. 排除
        exclude_hits = [k for k in EXCLUDE_KEYWORDS if k in text_for_search]
        if len(exclude_hits) >= 1:
            physics_rescue = sum(1 for k in ["牛頓", "電路", "透鏡", "拋體", "波長"] if k in text_for_search)
            if physics_rescue == 0:
                self.is_physics_likely = False
                self.status_reason = f"非物理關鍵字: {', '.join(exclude_hits[:2])}"
                return

        # 2. 章節
        max_score = 0
        best_chap = "未分類"
        for chap, keywords in CHAPTER_KEYWORDS.items():
            score = sum(1 for k in keywords if k in text_for_search)
            if score > max_score:
                max_score = score
                best_chap = chap
        
        if max_score > 0:
            self.predicted_chapter = best_chap
            self.status_reason = f"命中關鍵字 ({max_score})"
        else:
            general_score = sum(1 for k in PHYSICS_GENERAL_KEYWORDS if k in text_for_search)
            if general_score > 0:
                self.status_reason = "通用科學詞"
            else:
                self.is_physics_likely = False
                self.status_reason = "無明顯特徵"

# ==========================================
# Gemini AI 解析邏輯
# ==========================================

def clean_json_string(json_str):
    """清理 Gemini 回傳的 Markdown 格式，提取純 JSON"""
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    return json_str.strip()

def parse_with_gemini(file_bytes, file_type, api_key):
    """
    將檔案轉換為圖片，並呼叫 Gemini 1.5 Flash 進行多模態辨識
    """
    if not OCR_AVAILABLE:
         return {"error": "伺服器未安裝必要套件 (google-generativeai 或 pdf2image)。請檢查 requirements.txt 與 packages.txt。"}
    
    if not api_key:
        return {"error": "請輸入 Google Gemini API Key"}
    
    # 設定 API
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        return {"error": f"Gemini API 設定失敗: {str(e)}"}
    
    images = []
    
    try:
        if file_type == 'pdf':
            # 將 PDF 轉為圖片列表 (dpi=200 兼顧清晰度與傳輸速度)
            # 限制處理前 15 頁以避免過大
            images = convert_from_bytes(file_bytes, dpi=200, fmt='jpeg')[:15]
        else:
            return {"error": "目前 AI 辨識僅支援 PDF 檔案 (需轉為圖片處理)"}
    except Exception as e:
        return {"error": f"PDF 轉圖片失敗: {str(e)}。請確認系統已安裝 poppler-utils。"}

    # 準備 Prompt
    chapters_str = "\n".join(PHYSICS_CHAPTERS_LIST)
    
    prompt = f"""
    你是一個專業的高中物理老師助理。請幫我分析這份自然科試卷的圖片。
    
    任務目標：
    1. 辨識出所有的「物理科」試題。請忽略單純的化學、生物、地科題目。
    2. 如果是跨科題目且包含物理概念，請保留。
    3. 將辨識出的題目整理成 JSON 格式。
    
    請依照以下 JSON 結構回傳一個 List，不要包含任何其他解釋文字：
    [
        {{
            "number": 題號 (整數),
            "content": "題目敘述 (不含選項，去除題號)",
            "options": ["(A) 選項內容", "(B) 選項內容", ...],
            "answer": "答案 (若試卷上有標示，否則留空)",
            "chapter": "從下列清單中選擇最合適的章節: {chapters_str}"
        }}
    ]

    注意：
    - 題號請依序排列。
    - 數學公式請使用 LaTeX 格式 (例如 $E=mc^2$)。
    - 若無法判斷章節，請填「未分類」。
    - 確保 JSON 格式合法。
    """
    
    input_parts = [prompt]
    # 加入圖片物件
    for img in images: 
        input_parts.append(img)
        
    try:
        # 發送請求
        response = model.generate_content(input_parts)
        
        # 解析回傳的文字
        json_text = clean_json_string(response.text)
        data = json.loads(json_text)
        
        candidates = []
        for item in data:
            # 建立候選題物件
            cand = SmartQuestionCandidate(
                raw_text=item.get('content', ''),
                question_number=item.get('number', 0),
                options=item.get('options', []),
                chapter=item.get('chapter', '未分類'),
                is_likely=True,
                status_reason="Gemini AI 辨識"
            )
            # 確保內容正確賦值
            cand.content = item.get('content', '')
            candidates.append(cand)
            
        return candidates

    except json.JSONDecodeError:
        return {"error": "Gemini 回傳的資料無法解析為 JSON，請重試。"}
    except Exception as e:
        return {"error": f"Gemini API 呼叫失敗: {str(e)}"}

# ==========================================
# 傳統 OCR 與 Regex 邏輯 (備用模式)
# ==========================================

def perform_ocr(file_bytes):
    """將 PDF 轉圖片並執行 OCR"""
    if not OCR_AVAILABLE:
        return "Error: 伺服器未安裝 OCR 模組 (tesseract/poppler)。"
    
    try:
        images = convert_from_bytes(file_bytes, dpi=300)
        full_text = ""
        for img in images:
            # 使用 Tesseract 辨識
            text = pytesseract.image_to_string(img, lang='chi_tra+eng')
            full_text += text + "\n"
        return full_text
    except Exception as e:
        return f"OCR Error: {str(e)}"

def parse_raw_file(file_obj, file_type, use_ocr=False):
    """
    傳統 Regex/OCR 解析入口
    """
    full_text = ""
    file_obj.seek(0)
    file_bytes = file_obj.read()
    
    # 1. 取得文字
    if use_ocr and file_type == 'pdf':
        full_text = perform_ocr(file_bytes)
        if full_text.startswith("Error"): return []
    elif file_type == 'pdf':
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text: full_text += text + "\n"
        except: return []
    elif file_type == 'docx':
        doc = docx.Document(io.BytesIO(file_bytes))
        full_text = "\n".join([p.text for p in doc.paragraphs])

    if not full_text.strip(): return []

    lines = full_text.split('\n')
    possible_anchors = []
    
    # 允許數字間有空格 (針對 OCR): 1 0 . -> 10.
    anchor_pattern = re.compile(r'^\s*[\(（]?(\d+)\s*[\)）]?\s*[\.\、\．]')
    
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        match = anchor_pattern.match(line)
        if match:
            try:
                num = int(match.group(1))
                if 0 < num < 200: 
                    possible_anchors.append({'idx': idx, 'num': num, 'line': line})
            except: pass

    if not possible_anchors: return []

    # LIS 演算法 (最長遞增子序列)
    n = len(possible_anchors)
    dp = [1] * n
    prev = [-1] * n
    
    for i in range(n):
        for j in range(i):
            diff = possible_anchors[i]['num'] - possible_anchors[j]['num']
            if 1 <= diff <= 5: 
                if dp[j] + 1 > dp[i]:
                    dp[i] = dp[j] + 1
                    prev[i] = j
    
    max_len = 0
    end_idx = -1
    for i in range(n):
        if dp[i] > max_len:
            max_len = dp[i]
            end_idx = i
            
    valid_anchors = []
    curr = end_idx
    while curr != -1:
        valid_anchors.append(possible_anchors[curr])
        curr = prev[curr]
    valid_anchors.reverse()
    
    candidates = []
    for i in range(len(valid_anchors)):
        current_anchor = valid_anchors[i]
        start_line_idx = current_anchor['idx']
        q_num = current_anchor['num']
        
        if i < len(valid_anchors) - 1:
            end_line_idx = valid_anchors[i+1]['idx']
        else:
            end_line_idx = len(lines)
            
        first_line = lines[start_line_idx]
        match = anchor_pattern.match(first_line)
        if match:
            lines[start_line_idx] = first_line[match.end():].strip()
            
        raw_text_chunk = "\n".join(lines[start_line_idx:end_line_idx])
        
        if len(raw_text_chunk) > 2:
            candidates.append(SmartQuestionCandidate(raw_text_chunk, q_num))
            
    return candidates
