# app/api/v1_feedback.py
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from app.db.repository import DatabaseRepository

router = APIRouter()
db = DatabaseRepository()

class FeedbackRequest(BaseModel):
    task_id: str
    client_id: str
    rating: int  # 1 to 5
    feedback_text: str

@router.post("/mcp/v1/feedback")
async def receive_feedback(req: FeedbackRequest, background_tasks: BackgroundTasks):
    """顧客AIからのレスポンス評価・エラーフィードバックを受信してバックグラウンド学習へ送出"""
    
    # 1. DBにフィードバックを永続化
    await db.save_feedback(req.task_id, req.client_id, req.rating, req.feedback_text)
    
    # 2. 非同期でシステムプロンプト/ナレッジへ還元処理を実行
    background_tasks.add_task(db.optimize_instructions_from_feedback, req.feedback_text)
    
    return {
        "status": "ACCEPTED",
        "message": "Feedback received. System instruction auto-tuning triggered."
    }
