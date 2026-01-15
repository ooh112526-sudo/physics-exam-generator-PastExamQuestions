import re
import pdfplumber
import docx
import io

# ==========================================
# 關鍵字字典：用於判斷科目與章節
# ==========================================

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
        # 尋找選項 (A)... (B)...
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
            self.options = []

    def _predict_classification(self):
        text_for_search = self.content + " " + " ".join(self.options)
        
        # 1. 關鍵字排除
        exclude_hits = [k for k in EXCLUDE_KEYWORDS if k in text_for_search]
        if len(exclude_hits) >= 1:
            physics_rescue = sum(1 for k in ["牛頓", "電路", "透鏡", "拋體", "波長"] if k in text_for_search)
            if physics_rescue == 0:
                self.is_physics_likely = False
                self.status_reason = f"非物理關鍵字: {', '.join(exclude_hits[:2])}"
                return

        # 2. 章節預測
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
            self.status_reason = f"命中關鍵字 ({max_score})"
        else:
            general_score = sum(1 for k in PHYSICS_GENERAL_KEYWORDS if k in text_for_search)
            if general_score > 0:
                self.is_physics_likely = True
                self.status_reason = "通用科學詞"
            else:
                self.is_physics_likely = False
                self.status_reason = "無明顯特徵"

def parse_raw_file(file_obj, file_type):
    full_text = ""
    if file_type == 'pdf':
        try:
            with pdfplumber.open(file_obj) as pdf:
                for page in pdf.pages:
                    # 使用 layout=True 保持位置，避免雙欄混亂
                    text = page.extract_text(x_tolerance=3, y_tolerance=3, layout=True)
                    if text: full_text += text + "\n"
        except: return []
    elif file_type == 'docx':
        doc = docx.Document(file_obj)
        full_text = "\n".join([p.text for p in doc.paragraphs])
    
    lines = full_text.split('\n')
    candidates = []
    
    # === 寬鬆的題號掃描 ===
    # 策略：先抓出所有「疑似題號」的行，再決定哪些是真的
    # 允許：1. (1) 1 (帶有空格或點)
    q_start_pattern = re.compile(r'(?:^|\s)[\(（]?(\d+)[\)）]?[\.\、\．\s]')
    
    current_q_num = 0
    current_text = []
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        matches = list(q_start_pattern.finditer(line))
        found_match = None
        
        # 尋找這行中最合理的題號
        for match in matches:
            try:
                num = int(match.group(1))
            except: continue
            
            # 判斷是否接受這個數字為新題號
            is_valid = False
            
            # 條件A: 重設 (遇到 1) - 永遠接受，即便目前已經到第 10 題 (可能是誤判或新部分)
            if num == 1:
                is_valid = True
            
            # 條件B: 循序漸進 (接續上一題，允許跳號 1-5 題)
            elif current_q_num > 0 and 0 < (num - current_q_num) <= 5:
                is_valid = True
                
            # 條件C: 任意起點 (如果目前是 0，且數字 < 100，假設是試卷中段開始)
            elif current_q_num == 0 and 1 < num < 100:
                is_valid = True
                
            # 條件D: 修正 (如果當前題號 == 1 但內容很少，可能是抓到頁碼，允許被新的 1 取代)
            elif current_q_num == 1 and num == 1:
                is_valid = True

            if is_valid:
                # 為了避免行內數字誤判 (如 "長度 20 公尺")
                # 簡單檢查：數字後面的文字長度
                # 如果是題號，通常後面會有文字，或者這行就是題號
                found_match = (num, match)
                break # 這一行找到一個合理的就先當作題目開始 (簡化雙欄處理)
        
        if found_match:
            num, match = found_match
            
            # 存檔上一題
            if current_text:
                # 過濾過短的誤判 (例如只有題號沒有內容)
                if len("".join(current_text)) > 2: 
                    candidates.append(SmartQuestionCandidate("\n".join(current_text), current_q_num))
            
            current_q_num = num
            current_text = []
            
            # 擷取題號後的內容
            content_start = match.end()
            remain_text = line[content_start:].strip()
            if remain_text:
                current_text.append(remain_text)
        else:
            # 不是新題目，歸入當前題目
            if current_q_num > 0:
                current_text.append(line)
    
    # 存最後一題
    if current_text and current_q_num > 0:
        candidates.append(SmartQuestionCandidate("\n".join(current_text), current_q_num))
    
    # 排序修正 (解決雙欄導致的跳號)
    candidates.sort(key=lambda x: x.number)
    
    # 移除重複題號 (保留內容較長的那個)
    unique_candidates = []
    seen_nums = {}
    for c in candidates:
        if c.number in seen_nums:
            existing_idx = seen_nums[c.number]
            # 如果現在這個內容比之前那個長，就替換
            if len(c.content) > len(unique_candidates[existing_idx].content):
                unique_candidates[existing_idx] = c
        else:
            seen_nums[c.number] = len(unique_candidates)
            unique_candidates.append(c)
            
    return unique_candidates
