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
    "酸鹼", "沉澱", "氧化還原", "生物", "染色體", "演化", "板塊", "洋流", "試管",
    "葉綠素", "酵素", "粒線體", "北極冰蓋", "地層", "岩石", "地心引力" # 擴充排除詞
]

# 物理章節關鍵字映射 (用於預測章節)
CHAPTER_KEYWORDS = {
    "第一章.科學的態度與方法": ["單位", "因次", "SI制", "有效數字", "誤差", "測量", "國際單位"],
    "第二章.物體的運動": ["速度", "加速度", "位移", "牛頓", "運動定律", "拋體", "斜面", "摩擦力", "萬有引力", "克卜勒", "自由落體", "衝量", "動量"],
    "第三章. 物質的組成與交互作用": ["強力", "弱力", "重力", "電磁力", "夸克", "原子核", "基本粒子", "交互作用"],
    "第四章.電與磁的統一": ["電流", "電壓", "電阻", "磁場", "安培", "法拉第", "電磁感應", "透鏡", "折射", "反射", "干涉", "繞射", "都卜勒", "電磁波", "光電效應"],
    "第五章. 能　量": ["動能", "位能", "守恆", "作功", "功率", "力學能", "核能", "質能", "熱功當量", "焦耳"],
    "第六章.量子現象": ["光電效應", "光子", "波粒二象性", "物質波", "德布羅意", "能階", "光譜", "黑體輻射", "量子"]
}

# 簡單的物理通用詞
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
        """解析題目結構：分離選項與題幹"""
        # 嘗試尋找選項 (A) ... (B) ... 或 A. B.
        # 支援全形括號與點
        opt_pattern = re.compile(r'\s*[\(（]?[A-Ea-e][\)）][\.\、\．]?\s+')
        
        match = opt_pattern.search(self.raw_text)
        if match:
            self.content = self.raw_text[:match.start()].strip()
            opts_text = self.raw_text[match.start():]
            
            # 使用特殊標記分割選項
            temp_text = opt_pattern.sub(lambda m: f"|||{m.group().strip()}|||", opts_text)
            parts = temp_text.split('|||')
            
            current_opt = ""
            for p in parts:
                if not p.strip(): continue
                # 檢查是否為選項標籤
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
        """預測是否為物理題以及章節"""
        text_for_search = self.content + " " + " ".join(self.options)
        
        # 1. 判斷是否排除 (化學/生物/地科)
        exclude_hits = [k for k in EXCLUDE_KEYWORDS if k in text_for_search]
        if len(exclude_hits) >= 1: # 嚴格一點，出現一個明顯非物理關鍵字就排除
            # 但如果有明顯物理關鍵字，可能是跨科題，這裡先簡單判定
            # 檢查是否有強烈的物理關鍵字來救回
            physics_rescue = sum(1 for k in ["牛頓", "電路", "透鏡", "拋體"] if k in text_for_search)
            if physics_rescue == 0:
                self.is_physics_likely = False
                self.status_reason = f"偵測到非物理關鍵字: {', '.join(exclude_hits[:3])}"
                return

        # 2. 判斷章節
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
            general_score = sum(1 for k in PHYSICS_GENERAL_KEYWORDS if k in text_for_search)
            if general_score > 0:
                self.is_physics_likely = True
                self.status_reason = "通用科學詞彙，需人工確認"
            else:
                self.is_physics_likely = False # 若連通用詞都沒有，預設不選
                self.status_reason = "無明顯特徵"

def parse_raw_file(file_obj, file_type):
    """主解析入口"""
    full_text = ""
    
    if file_type == 'pdf':
        try:
            with pdfplumber.open(file_obj) as pdf:
                for page in pdf.pages:
                    # 嘗試使用 layout=True 以保留空間感，避免雙欄文字擠在一起
                    text = page.extract_text(x_tolerance=2, y_tolerance=2)
                    if text: full_text += text + "\n"
        except Exception as e:
            return [] # 發生錯誤回傳空清單
                
    elif file_type == 'docx':
        doc = docx.Document(file_obj)
        full_text = "\n".join([p.text for p in doc.paragraphs])
    
    # === 強化版題號偵測邏輯 ===
    # 支援：
    # 1. 行首題號 (1. , 1 , (1))
    # 2. 全形標點 (１．, 1．)
    # 3. 雙欄排版導致題號在行中間的情況
    
    lines = full_text.split('\n')
    candidates = []
    current_text = []
    current_q_num = 0
    
    # Regex 解釋：
    # (?:^|\s)  -> 行首或空格開頭 (處理題號在行中間的情況)
    # [\(（]?   -> 可選的前括號
    # (\d+)     -> 數字本體 (Capture Group 1)
    # [\)）]?   -> 可選的後括號
    # [\.\、\．\s] -> 分隔符號：點、頓號、全形點、或空格
    q_start_pattern = re.compile(r'(?:^|\s)[\(（]?(\d+)[\)）]?[\.\、\．\s]')
    
    # 為了避免抓到內文中的數字 (如 "...長度為 20 公尺...")
    # 我們加入一個邏輯：數字必須呈現遞增趨勢
    
    buffer_lines = [] # 用來暫存尚未歸類的文字
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        # 在這一行中搜尋所有可能的題號
        matches = list(q_start_pattern.finditer(line))
        
        # 如果這一行有多個題號 (雙欄排版常發生，例如 "1. 題目A...  21. 題目B...")
        # 或是只有一個
        
        found_new_q = False
        
        for match in matches:
            try:
                num = int(match.group(1))
            except:
                continue
                
            # 判斷這個數字是否合理的「下一題」
            # 規則：
            # 1. 如果是第1題 (允許重設)
            # 2. 如果是接續題號 (current < num < current + 10) -> 允許跳題但不允許太遠
            is_valid_next = False
            if num == 1:
                if current_q_num == 0 or current_q_num > 10: # 可能是新的一份試卷開始
                    is_valid_next = True
            elif current_q_num < num < current_q_num + 5: # 允許稍微跳號(題組)
                is_valid_next = True
            
            if is_valid_next:
                # 找到新題目了！
                
                # 1. 先把舊題目存起來
                if current_text:
                    candidates.append(SmartQuestionCandidate("\n".join(current_text), current_q_num))
                    current_text = []
                
                # 2. 更新當前題號
                current_q_num = num
                found_new_q = True
                
                # 3. 處理這一行文字
                # 如果題號在行中間，這行前面的文字屬於上一題 (或被捨棄)，後面的屬於這一題
                start_idx = match.start()
                
                # 如果這一行還有前面的文字，且不是空白，要判斷歸屬
                # 簡單作法：如果發現新題號，則將 match 之後的文字視為該題內容
                # match 之前的文字如果很多，可能是上一題的尾巴 (但在這裡簡化處理，直接切斷)
                
                # 擷取題號之後的文字作為題目開頭
                content_start = match.end()
                current_text.append(line[content_start:].strip())
                
                # 注意：如果這一行後面還有另一個題號 (雙欄)，會在下一次迴圈處理嗎？
                # 因為我們是依序處理 matches，所以如果 "1. ... 2. ..."
                # 處理 1 時，會把 "..." 加上 "2. ..." 都當作 1 的內容
                # 等到迴圈跑到 2 時，會把 1 存檔，然後開始 2
                # 這樣邏輯是通的！
        
        if not found_new_q:
            # 如果這行沒有發現新題號，就歸到當前題目
            if current_q_num > 0:
                current_text.append(line)
            else:
                # 還沒開始第一題之前的文字 (可能是試卷標頭)
                pass

    # 迴圈結束，存最後一題
    if current_text and current_q_num > 0:
        candidates.append(SmartQuestionCandidate("\n".join(current_text), current_q_num))
    
    # 最後依照題號排序 (修正雙欄解析導致的 1, 21, 2, 22... 穿插問題)
    candidates.sort(key=lambda x: x.number)
        
    return candidates
