import re
import pdfplumber
import docx
import io

# ==========================================
# 關鍵字字典
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
                # 檢查是否為空文件或純圖片
                first_page_text = pdf.pages[0].extract_text()
                if not first_page_text:
                    # 嘗試如果不加 layout 參數是否能讀到
                    pass 
                
                for page in pdf.pages:
                    # 關鍵修改：不使用 layout=True，避免雙欄排版文字碎裂
                    # 大多數雙欄 PDF 的文字流順序是：左欄全部 -> 右欄全部
                    text = page.extract_text() 
                    if text: full_text += text + "\n"
        except Exception as e:
            return []
    elif file_type == 'docx':
        doc = docx.Document(file_obj)
        full_text = "\n".join([p.text for p in doc.paragraphs])
    
    if not full_text.strip():
        return []

    lines = full_text.split('\n')
    
    # === 第一階段：收集所有疑似題號的候選者 ===
    # 格式：(行號 index, 題號數字 number, 原始行文字 line_content)
    possible_anchors = []
    
    # Regex: 寬鬆匹配行首數字
    # 支援: "1.", "1 ", "(1)", "1．", "1、"
    anchor_pattern = re.compile(r'^\s*[\(（]?(\d+)[\)）]?[\.\、\．\s]')
    
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        
        match = anchor_pattern.match(line)
        if match:
            try:
                num = int(match.group(1))
                # 排除顯然不合理的數字 (例如年份 107, 2023 或過大的數字)
                if 0 < num < 200: 
                    possible_anchors.append({'idx': idx, 'num': num, 'line': line})
            except: pass

    if not possible_anchors:
        return []

    # === 第二階段：使用 LIS (最長遞增子序列) 演算法找出真正的題號序列 ===
    # 目標：在雜亂的 possible_anchors 中，找出一條路徑，
    # 使得 num[i+1] == num[i] + 1 (完美連續) 或 num[i] < num[i+1] <= num[i] + 5 (允許少量跳題/漏抓)
    
    # dp[i] 儲存以第 i 個 anchor 結尾的最長鏈長度
    # prev[i] 儲存路徑的前一個節點 index
    n = len(possible_anchors)
    dp = [1] * n
    prev = [-1] * n
    
    for i in range(n):
        for j in range(i):
            diff = possible_anchors[i]['num'] - possible_anchors[j]['num']
            # 條件：
            # 1. 數字遞增
            # 2. 差值在 1~5 之間 (允許題目中間夾雜小標題導致漏抓，但不允許跳太大)
            # 3. 行號必須在後面 (這在 loop 順序已保證)
            if 1 <= diff <= 5:
                if dp[j] + 1 > dp[i]:
                    dp[i] = dp[j] + 1
                    prev[i] = j
    
    # 找出最長鏈的結尾
    max_len = 0
    end_idx = -1
    for i in range(n):
        if dp[i] > max_len:
            max_len = dp[i]
            end_idx = i
            
    # 如果找不到長度 > 2 的序列，可能是單題匯入或辨識失敗
    if max_len < 2 and n > 5:
        # 如果真的很少，放寬標準，只要是 1 開頭就試試看
        pass 
        
    # 重建路徑 (倒推回來)
    valid_anchors = []
    curr = end_idx
    while curr != -1:
        valid_anchors.append(possible_anchors[curr])
        curr = prev[curr]
    valid_anchors.reverse() # 轉回正序
    
    # === 第三階段：根據篩選出的 Anchor 切割文本 ===
    candidates = []
    
    for i in range(len(valid_anchors)):
        current_anchor = valid_anchors[i]
        start_line_idx = current_anchor['idx']
        q_num = current_anchor['num']
        
        # 決定結束行：下一個 anchor 的開始行，或是文件結尾
        if i < len(valid_anchors) - 1:
            end_line_idx = valid_anchors[i+1]['idx']
        else:
            end_line_idx = len(lines)
            
        # 組合題目內容
        # 第一行通常包含題號，我們把題號去除，只留內容
        first_line = lines[start_line_idx]
        # 去除開頭的 "1." 或 "1 "
        content_start_match = anchor_pattern.match(first_line)
        if content_start_match:
            lines[start_line_idx] = first_line[content_start_match.end():].strip()
            
        # 收集範圍內的所有行
        raw_text_chunk = "\n".join(lines[start_line_idx:end_line_idx])
        
        # 只有當內容長度足夠時才加入 (避免只抓到一個題號)
        if len(raw_text_chunk) > 2:
            candidates.append(SmartQuestionCandidate(raw_text_chunk, q_num))
            
    return candidates
