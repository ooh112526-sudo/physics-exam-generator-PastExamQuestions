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

# 1. 檢查 Google AI SDK
try:
    import google.generativeai as genai
    from google.ai.generativelanguage_v1beta.types import content
    HAS_GENAI = True
except ImportError:
    pass

# 2. 檢查 PDF 轉圖片工具 (Poppler)
try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError:
    pass

# 3. 檢查 Tesseract OCR
try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    pass

# ==========================================
# 供 app.py 呼叫的狀態檢查函式
# ==========================================
def is_ocr_available():
    """回傳是否具備本機 OCR 能力 (需同時有 PDF 工具與 Tesseract)"""
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

        # 若非 AI 辨識，嘗試手動解析
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
        # 簡易關鍵字分類
        text_for_search = self.content + " " + " ".join(self.options)
        for chap, kws in CHAPTER_KEYWORDS.items():
            if any(k in text_for_search for k in kws):
                self.
