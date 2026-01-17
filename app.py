import docx
import io
import re
from docx.shared import Pt
from google.cloud import firestore

# ==========================================
# 資料模型定義 (共用)
# ==========================================

class Question:
    def __init__(self, q_type, content, options=None, answer=None, original_id=None, image_data=None, 
                 source="一般試題", chapter="", unit=""):
        self.id = original_id # Firestore Document ID
        self.type = q_type    # 'Single', 'Multi', 'Fill'
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.image_data = image_data # 二進位圖片資料

    def to_firestore_dict(self):
        """轉換為 Firestore 儲存格式"""
        # 注意：Firestore 單一文件限制 1MB。
        # 若圖片過大，建議改為上傳 Cloud Storage 並在此儲存 URL。
        # 這裡為了簡單示範，我們暫時不將二進位圖片寫入資料庫，以免爆量。
        return {
            "type": self.type,
            "source": self.source,
            "chapter": self.chapter,
            "unit": self.unit,
            "content": self.content,
            "options": self.options,
            "answer": self.answer,
            "has_image": True if self.image_data else False,
            "created_at": firestore.SERVER_TIMESTAMP
        }

    @staticmethod
    def from_dict(doc_id, data):
        """從 Firestore 資料還原為物件"""
        return Question(
            q_type=data.get('type'),
            content=data.get('content'),
            options=data.get('options'),
            answer=data.get('answer'),
            original_id=doc_id,
            source=data.get('source'),
            chapter=data.get('chapter'),
            unit=data.get('unit')
        )

# ==========================================
# Word 解析邏輯
# ==========================================

def extract_images_from_paragraph(paragraph, doc_part):
    """從 Word 段落中擷取圖片"""
    images = []
    # 定義 Word XML 的命名空間
    nsmap = {
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    }
    # 尋找所有圖片參照
    blips = paragraph._element.findall('.//a:blip', namespaces=nsmap)
    for blip in blips:
        embed_attr = blip.get(f"{{{nsmap['r']}}}embed")
        if embed_attr and embed_attr in doc_part.rels:
            part = doc_part.rels[embed_attr].target_part
            # 確認是圖片類型
            if "image" in part.content_type:
                images.append(part.blob)
    return images

def parse_docx(file_bytes):
    """
    解析 Word 檔案
    支援標籤：[Src:], [Chap:], [Unit:], [Type:], [Q], [Opt], [Ans]
    """
    doc = docx.Document(io.BytesIO(file_bytes))
    doc_part = doc.part
    
    questions = []
    current_q = None
    state = None # Q, Opt, Ans
    
    # 用於移除選項開頭的 (A) 或 A.
    opt_pattern = re.compile(r'^\s*\(?[A-Ea-e]\)?\s*[.、]?\s*')
    
    # 預設分類狀態 (會延續到下一題)
    curr_src = "一般試題"
    curr_chap = ""
    curr_unit = ""

    for para in doc.paragraphs:
        text = para.text.strip()
        found_images = extract_images_from_paragraph(para, doc_part)
        
        # ---------------------------
        # 0. 偵測全域分類標籤
        # ---------------------------
        if text.startswith('[Src:'):
            curr_src = text.split(':', 1)[1].replace(']', '').strip()
            continue
        if text.startswith('[Chap:'):
            curr_chap = text.split(':', 1)[1].replace(']', '').strip()
            continue
        if text.startswith('[Unit:'):
            curr_unit = text.split(':', 1)[1].replace(']', '').strip()
            continue
        # 相容舊版標籤
        if text.startswith('[Cat:'):
            curr_unit = text.split(':', 1)[1].replace(']', '').strip()
            continue

        # ---------------------------
        # 1. 偵測新題目開始 [Type:...]
        # ---------------------------
        if text.startswith('[Type:'):
            # 先儲存上一題
            if current_q: questions.append(current_q)
            
            q_type_str = text.split(':', 1)[1].replace(']', '').strip()
            # 建立新題目物件
            current_q = Question(
                q_type=q_type_str, 
                content="", 
                options=[], 
                answer="", 
                source=curr_src,
                chapter=curr_chap,
                unit=curr_unit
            )
            state = None
            continue

        # ---------------------------
        # 2. 狀態切換標籤
        # ---------------------------
        if text.startswith('[Q]'):
            state = 'Q'
            continue
        elif text.startswith('[Opt]'):
            state = 'Opt'
            continue
        elif text.startswith('[Ans]'):
            # 有些人會把答案寫在 [Ans] 同一行
            remain_text = text.replace('[Ans]', '').strip()
            if remain_text and current_q: 
                current_q.answer = remain_text
            state = 'Ans'
            continue

        # ---------------------------
        # 3. 填入內容
        # ---------------------------
        if current_q:
            # 如果在 [Q] 區塊發現圖片，存入題目
            if found_images and state == 'Q':
                current_q.image_data = found_images[0]

            if not text: continue

            if state == 'Q':
                current_q.content += text + "\n"
            elif state == 'Opt':
                clean_opt = opt_pattern.sub('', text)
                current_q.options.append(clean_opt)
            elif state == 'Ans':
                current_q.answer += text

    # 迴圈結束後，加入最後一題
    if current_q: 
        questions.append(current_q)
        
    return questions
