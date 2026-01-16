import re
import pdfplumber
import docx
import io
import json
import time
import sys

# ==========================================
# 依賴套件檢查 (拆分檢查以利除錯)
# ==========================================
HAS_GENAI = False
HAS_PDF2IMAGE = False
HAS_OCR = False

# 1. 檢查 Google AI SDK
try:
    import google.generativeai as genai
    from google.api_core import retry
    HAS_GENAI = True
except ImportError:
    print("Warning: google-generativeai 未安裝")

# 2. 檢查 PDF 轉圖片工具 (Gemini 視覺辨識需要)
try:
    from pdf2image import convert_from_bytes
    from PIL import Image
    HAS_PDF2IMAGE = True
except ImportError:
    print("Warning: pdf2image 或 pillow 未安裝")

# 3. 檢查 Tesseract OCR (本機備用模式需要)
try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    print("Warning: pytesseract 未安裝")


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
        exclude_hits = [k for k in EXCLUDE_KEYWORDS if k in text_for_search]
        if len(exclude_hits) >= 1:
            physics_rescue = sum(1 for k in ["牛頓", "電路", "透鏡", "拋體", "波長"] if k in text_for_search)
            if physics_rescue == 0:
                self.is_physics_likely = False
                self.status_reason = f"非物理關鍵字: {', '.join(exclude_hits[:2])}"
                return
        
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
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    return json_str.strip()

def parse_with_gemini(file_bytes, file_type, api_key):
    # 1. 檢查 Python 套件是否安裝
    if not HAS_GENAI:
        return {"error": "缺少 'google-generativeai' 套件。請檢查 requirements.txt。"}
    if not HAS_PDF2IMAGE:
        return {"error": "缺少 'pdf2image' 套件。請檢查 requirements.txt。"}
    
    if not api_key:
        return {"error": "請輸入 Google Gemini API Key"}
    
    # 2. 設定 API
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        return {"error": f"Gemini API 設定失敗，Key 可能無效: {str(e)}"}
    
    # 3. 嘗試轉換圖片 (這步最常失敗，因為需要 poppler)
    images = []
    try:
        if file_type == 'pdf':
            # 只取前 10 頁，減少傳輸量與錯誤機率
            images = convert_from_bytes(file_bytes, dpi=200, fmt='jpeg')[:10]
        else:
            return {"error": "目前 AI 辨識僅支援 PDF 檔案"}
    except Exception as e:
        return {"error": f"PDF 轉圖片失敗。請確認 packages.txt 包含 'poppler-utils' 且已重啟 App。詳細錯誤: {str(e)}"}

    if not images:
        return {"error": "PDF 轉換後沒有圖片，請確認檔案是否正常。"}

    # Prompt
    chapters_str = "\n".join(PHYSICS_CHAPTERS_LIST)
    prompt = f"""
    你是一個高中物理老師助理。請分析試卷圖片。
    目標：
    1. 辨識所有「物理科」試題。
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
    for img in images: input_parts.append(img)
    
    # 4. 呼叫 API (簡化安全設定，避免舊版相容性問題)
    try:
        # 嘗試不傳送 safety_settings，使用預設值，避免參數格式錯誤
        # 若需要調整，請確保 google-generativeai 版本是最新的
        response = model.generate_content(input_parts)
        
        try:
            text_response = response.text
        except ValueError:
            # 可能是被安全阻擋
            return {"error": f"Gemini 拒絕回應。原因: {response.candidates[0].finish_reason if response.candidates else 'Unknown'}"}

        json_text = clean_json_string(text_response)
        data = json.loads(json_text)
        
        candidates = []
        for item in data:
            cand = SmartQuestionCandidate(
                raw_text=item.get('content', ''),
                question_number=item.get('number', 0),
                options=item.get('options', []),
                chapter=item.get('chapter', '未分類'),
                is_likely=True,
                status_reason="Gemini AI 辨識"
            )
            cand.content = item.get('content', '')
            candidates.append(cand)
        return candidates

    except json.JSONDecodeError:
        return {"error": "AI 回傳了非 JSON 格式的資料，請重試一次。"}
    except Exception as e:
        return {"error": f"Gemini API 連線或回應錯誤: {str(e)}"}

# ==========================================
# 傳統 OCR 邏輯 (備用)
# ==========================================
# 為了避免找不到 OCR_AVAILABLE 變數，這裡定義一個屬性供外部讀取
def is_ocr_available():
    return HAS_OCR and HAS_PDF2IMAGE

def parse_raw_file(file_obj, file_type, use_ocr=False):
    full_text = ""
    file_obj.seek(0)
    file_bytes = file_obj.read()
    
    # 使用我們拆分後的檢查變數
    if use_ocr and file_type == 'pdf':
        if not HAS_PDF2IMAGE:
             print("Error: 缺少 pdf2image，無法執行 OCR")
             return []
        
        try:
            images = convert_from_bytes(file_bytes, dpi=300)
            for img in images:
                if HAS_OCR:
                    full_text += pytesseract.image_to_string(img, lang='chi_tra+eng') + "\n"
        except Exception as e:
            print(f"OCR Error: {e}")
            pass

    elif file_type == 'pdf':
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t: full_text += t + "\n"
        except: pass
    elif file_type == 'docx':
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            full_text = "\n".join([p.text for p in doc.paragraphs])
        except: pass

    if not full_text.strip(): return []
    
    lines = full_text.split('\n')
    possible_anchors = []
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
        end_line_idx = valid_anchors[i+1]['idx'] if i < len(valid_anchors)-1 else len(lines)
        
        first_line = lines[start_line_idx]
        match = anchor_pattern.match(first_line)
        if match: lines[start_line_idx] = first_line[match.end():].strip()
            
        raw_text_chunk = "\n".join(lines[start_line_idx:end_line_idx])
        if len(raw_text_chunk) > 2:
            candidates.append(SmartQuestionCandidate(raw_text_chunk, q_num))
            
    return candidates
