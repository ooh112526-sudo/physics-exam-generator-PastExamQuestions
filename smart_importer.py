import re
import pdfplumber
import docx
import io


# ==========================================
# 關鍵字字典：用於判斷科目與章節
# ==========================================


# 排除關鍵字 (若出現這些詞，很可能不是物理題)
EXCLUDE_KEYWORDS = [
    "化學", "反應式", "莫耳", "有機", "細胞", "遺傳", "DNA", "生態", "地質", "氣候", 
    "酸鹼", "沉澱", "氧化還原", "生物", "染色體", "演化", "板塊", "洋流"
]


# 物理章節關鍵字映射 (用於預測章節)
CHAPTER_KEYWORDS = {
    "第一章.科學的態度與方法": ["單位", "因次", "SI制", "有效數字", "誤差", "測量", "國際單位"],
    "第二章.物體的運動": ["速度", "加速度", "位移", "牛頓", "運動定律", "拋體", "斜面", "摩擦力", "萬有引力", "克卜勒", "自由落體"],
    "第三章. 物質的組成與交互作用": ["強力", "弱力", "重力", "電磁力", "夸克", "原子核", "基本粒子", "交互作用"],
    "第四章.電與磁的統一": ["電流", "電壓", "電阻", "磁場", "安培", "法拉第", "電磁感應", "透鏡", "折射", "反射", "干涉", "繞射", "都卜勒", "電磁波"],
    "第五章. 能 量": ["動能", "位能", "守恆", "作功", "功率", "力學能", "核能", "質能", "熱功當量", "焦耳"],
    "第六章.量子現象": ["光電效應", "光子", "波粒二象性", "物質波", "德布羅意", "能階", "光譜", "黑體輻射", "量子"]
}


# 簡單的物理通用詞 (用於判定是否為物理)
PHYSICS_GENERAL_KEYWORDS = [
    "物體", "粒子", "系統", "軌跡", "圖形", "數據", "實驗", "裝置", "觀察", "現象"
]


class SmartQuestionCandidate:
    def __init__(self, raw_text, question_number):
        self.raw_text = raw_text
        self.number = question_number
        self.content = ""
        self.options = []
        self.predicted_chapter = "未分類"
        self.is_physics_likely = True
        self.status_reason = ""
        
        self._parse_structure()
        self._predict_classification()


    def _parse_structure(self):
        """解析題目結構：分離選項與題幹"""
        # 1. 嘗試尋找選項 (A) ... (B) ...
        # Regex patterns for options like (A), (B) or A. B.
        opt_pattern = re.compile(r'\s*[\(（]?[A-Ea-e][\)）][\.\、]?\s*')
        
        # 簡單切分邏輯：找到第一個選項標記，其後視為選項區
        match = opt_pattern.search(self.raw_text)
        if match:
            self.content = self.raw_text[:match.start()].strip()
            opts_text = self.raw_text[match.start():]
            
            # 分割選項
            # 這裡使用一個技巧：在每個選項前加特殊標記，再 split
            temp_text = opt_pattern.sub(lambda m: f"|||{m.group().strip()}|||", opts_text)
            parts = temp_text.split('|||')
            
            current_opt = ""
            for p in parts:
                if not p.strip(): continue
                # 如果是選項代號 (e.g. "(A)")
                if re.match(r'^[\(（]?[A-Ea-e][\)）][\.\、]?$', p.strip()):
                    if current_opt: self.options.append(current_opt)
                    current_opt = "" # 準備接內容
                else:
                    current_opt = p.strip()
            if current_opt: self.options.append(current_opt)
        else:
            self.content = self.raw_text.strip()
            self.options = []


    def _predict_classification(self):
        """預測是否為物理題以及章節"""
        text_for_search = self.content + " " + " ".join(self.options)
        
        # 1. 判斷是否排除 (化學/生物)
        exclude_score = sum(1 for k in EXCLUDE_KEYWORDS if k in text_for_search)
        if exclude_score >= 2: # 出現兩個以上排除關鍵字
            self.is_physics_likely = False
            self.status_reason = "偵測到化學/生物關鍵字"
            return


        # 2. 判斷章節與物理相關性
        max_score = 0
        best_chap = "未分類"
        
        for chap, keywords in CHAPTER_KEYWORDS.items():
            score = sum(1 for k in keywords if k in text_for_search)
            if score > max_score:
                max_score = score
                best_chap = chap
        
        if max_score > 0:
            self.predicted_chapter = best_chap
            self.is_physics_likely = True
            self.status_reason = f"命中關鍵字 (分數: {max_score})"
        else:
            # 如果沒有命中特定章節，但有通用物理詞
            general_score = sum(1 for k in PHYSICS_GENERAL_KEYWORDS if k in text_for_search)
            if general_score > 0:
                self.is_physics_likely = True # 暫定保留
                self.status_reason = "通用科學詞彙，需人工確認"
            else:
                self.is_physics_likely = False
                self.status_reason = "無明顯物理特徵"


def parse_raw_file(file_obj, file_type):
    """主解析入口"""
    full_text = ""
    
    if file_type == 'pdf':
        with pdfplumber.open(file_obj) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text: full_text += text + "\n"
                
    elif file_type == 'docx':
        doc = docx.Document(file_obj)
        full_text = "\n".join([p.text for p in doc.paragraphs])
    
    # 切分題目：假設題號格式為 "1. ", "2.", "(1)", "1 " 等
    # 這是一個困難點，這裡使用一個較通用的 Regex：行首數字加標點
    # 或是行首直接是數字
    
    # 策略：先把文本正規化
    lines = full_text.split('\n')
    candidates = []
    current_text = []
    current_q_num = 0
    
    q_start_pattern = re.compile(r'^\s*[\(（]?(\d+)[\)）]?[\.\、\s]')
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        match = q_start_pattern.match(line)
        if match:
            # 發現新題號
            num = int(match.group(1))
            # 如果題號是連續的或是新的開始 (允許一些誤差，例如跳題)
            if num == 1 or (num > current_q_num and num < current_q_num + 5):
                # 儲存上一題
                if current_text:
                    candidates.append(SmartQuestionCandidate("\n".join(current_text), current_q_num))
                
                current_text = [line] # 開始新的一題
                current_q_num = num
                continue
        
        # 累積到當前題目
        if current_text:
            current_text.append(line)
            
    # 儲存最後一題
    if current_text:
        candidates.append(SmartQuestionCandidate("\n".join(current_text), current_q_num))
        
    return candidates