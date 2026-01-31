import docx
from docx.shared import Pt, Inches
from docx.oxml.ns import qn
import io
import requests

# 題型對照表 (Export 需要知道中文顯示)
TYPE_MAP_ZH_TO_EN = {"單選": "Single", "多選": "Multi", "填充": "Fill", "題組": "Group"}
TYPE_MAP_EN_TO_ZH = {v: k for k, v in TYPE_MAP_ZH_TO_EN.items()}

def get_image_bytes(q):
    """嘗試取得題目的圖片 (Base64 或 URL)"""
    if q.image_data: return q.image_data
    if q.image_url:
        try:
            response = requests.get(q.image_url, timeout=3)
            if response.status_code == 200: return response.content
        except: return None
    return None

def generate_word_files(selected_questions):
    """
    生成試題卷與答案卷 Word 檔
    """
    exam_doc = docx.Document()
    ans_doc = docx.Document()
    
    # 設定基本字型
    style = exam_doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '標楷體')
    
    exam_doc.add_heading('物理科 試題卷', 0)
    ans_doc.add_heading('物理科 答案卷', 0)
    
    q_counter = 1
    
    def write_single_question(doc, q, idx_str):
        p = doc.add_paragraph()
        
        # 顯示中文題型標籤
        type_badge_zh = TYPE_MAP_EN_TO_ZH.get(q.type, q.type)
        type_label = f"【{type_badge_zh}】"
        src_label = f"[{q.source}] " if q.source and not q.parent_id else "" 
        
        runner = p.add_run(f"{idx_str}. {src_label}{type_label} {q.content.strip()}")
        runner.bold = True
        
        img_bytes = get_image_bytes(q)
        if img_bytes:
            try:
                img_p = doc.add_paragraph()
                run = img_p.add_run()
                run.add_picture(io.BytesIO(img_bytes), width=Inches(2.5))
            except: pass

        if q.type in ['Single', 'Multi'] and q.options:
            opts = q.options
            max_len = max([len(str(o)) for o in opts]) if opts else 0
            if max_len < 10 and len(opts) > 0:
                doc.add_paragraph("　　".join(opts))
            elif max_len < 25 and len(opts) > 0 and len(opts) % 2 == 0:
                table = doc.add_table(rows=(len(opts) // 2), cols=2)
                table.autofit = True
                for i, opt in enumerate(opts):
                    table.cell(i // 2, i % 2).text = opt
                doc.add_paragraph("")
            else:
                for opt in opts:
                    doc.add_paragraph(f"{opt}")
        elif q.type == 'Fill':
            doc.add_paragraph("答：______________________")
        doc.add_paragraph("") 

    for q in selected_questions:
        if q.is_group_parent:
            write_single_question(exam_doc, q, f"{q_counter}-{q_counter + len(q.sub_questions) - 1} 為題組")
            for sub_q in q.sub_questions:
                write_single_question(exam_doc, sub_q, str(q_counter))
                ans_p = ans_doc.add_paragraph()
                ans_p.add_run(f"{q_counter}. {sub_q.answer}")
                q_counter += 1
        else:
            write_single_question(exam_doc, q, str(q_counter))
            ans_p = ans_doc.add_paragraph()
            ans_p.add_run(f"{q_counter}. {q.answer}")
            q_counter += 1
        
    exam_io = io.BytesIO()
    ans_io = io.BytesIO()
    exam_doc.save(exam_io)
    ans_doc.save(ans_io)
    exam_io.seek(0)
    ans_io.seek(0)
    return exam_io, ans_io