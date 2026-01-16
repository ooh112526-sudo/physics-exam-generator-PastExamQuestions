import streamlit as st
import json
from google.cloud import firestore
from google.oauth2 import service_account

# 檢查是否有設定 Firebase Secrets
def get_db():
    # 嘗試從 Streamlit Secrets 讀取 Firebase 設定
    if "firebase" in st.secrets:
        try:
            # 將 secrets 轉換為 dict
            key_dict = dict(st.secrets["firebase"])
            creds = service_account.Credentials.from_service_account_info(key_dict)
            db = firestore.Client(credentials=creds, project=key_dict["project_id"])
            return db
        except Exception as e:
            print(f"Firebase 連線失敗: {e}")
            return None
    return None

def save_question_to_cloud(question_data):
    """儲存單一題目到 Firestore"""
    db = get_db()
    if not db: return False
    
    try:
        # 使用題目的 ID 或自動產生 ID
        doc_ref = db.collection("questions").document(str(question_data["id"]))
        doc_ref.set(question_data)
        return True
    except Exception as e:
        st.error(f"儲存失敗: {e}")
        return False

def load_questions_from_cloud():
    """從 Firestore 載入所有題目"""
    db = get_db()
    if not db: return []
    
    questions = []
    try:
        docs = db.collection("questions").stream()
        for doc in docs:
            questions.append(doc.to_dict())
    except Exception as e:
        st.error(f"讀取失敗: {e}")
    
    return questions

def delete_question_from_cloud(q_id):
    """刪除題目"""
    db = get_db()
    if not db: return False
    try:
        db.collection("questions").document(str(q_id)).delete()
        return True
    except: return False
