import time
import random
import base64
class Question:
    def __init__(self, q_type, content, options=None, answer=None, 
                 solution=None, # [New] 解析欄位
                 image_blob_name=None, # [New] 圖片儲存路徑 (用於刪除)
                 original_id=0, image_data=None, 
                 source="一般試題", chapter="未分類", unit="", db_id=None, 
                 parent_id=None, is_group_parent=False, sub_questions=None, image_url=None,
                 source_file_id=None):
        self.id = db_id if db_id else str(int(time.time()*1000)) + str(random.randint(0, 999))
        self.type = q_type 
        self.source = source
        self.chapter = chapter
        self.unit = unit
        self.content = content
        self.options = options if options else []
        self.answer = answer
        self.solution = solution if solution else "" # [New] 初始化解析
        self.image_blob_name = image_blob_name # [New]
        self.image_data = image_data 
        self.image_url = image_url   
        self.parent_id = parent_id 
        self.is_group_parent = is_group_parent 
        self.sub_questions = sub_questions if sub_questions else [] 
        self.source_file_id = source_file_id
    def to_dict(self):
        img_str = None
        if self.image_data: img_str = base64.b64encode(self.image_data).decode('utf-8')
        subs = [q.to_dict() for q in self.sub_questions] if self.sub_questions else []
        return {
            "id": self.id, "type": self.type, "source": self.source, "chapter": self.chapter,
            "content": self.content, "options": self.options, "answer": self.answer,
            "solution": self.solution, # [New] 儲存解析
            "image_blob_name": self.image_blob_name, # [New]
            "image_data_b64": img_str, "image_url": self.image_url,
            "parent_id": self.parent_id, "is_group_parent": self.is_group_parent,
            "sub_questions": subs, "source_file_id": self.source_file_id
        }
    @staticmethod
    def from_dict(data):
        img_bytes = None
        if data.get("image_data_b64"):
            try: img_bytes = base64.b64decode(data["image_data_b64"])
            except: pass
        q = Question(
            q_type=data.get("type", "Single"), content=data.get("content", ""),
            options=data.get("options", []), answer=data.get("answer", ""),
            solution=data.get("solution", ""), # [New] 讀取解析
            image_blob_name=data.get("image_blob_name"), # [New]
            original_id=0, image_data=img_bytes, image_url=data.get("image_url"),
            source=data.get("source", ""), chapter=data.get("chapter", "未分類"),
            db_id=data.get("id"), parent_id=data.get("parent_id"),
            is_group_parent=data.get("is_group_parent", False),
            source_file_id=data.get("source_file_id")
        )
        if data.get("sub_questions"):
            q.sub_questions = [Question.from_dict(sub) for sub in data["sub_questions"]]
        return q
