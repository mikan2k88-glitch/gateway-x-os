# main.py
import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
import google.generativeai as genai

# Google AI StudioのAPIキーを設定（環境変数から読み込み）
API_KEY = os.environ.get("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

app = FastAPI(
    title="Gateway X-OS Vetting Engine",
    version="4.3",
    description="Autonomous AI Agent Request Vetting Engine"
)

# 外部クライアントAIからのリクエスト構造定義
class AgentRequest(BaseModel):
    agent_id: str
    query: str
    budget_usd: Optional[float] = 0.0
    intent_category: Optional[str] = "GENERAL"

# Gateway X-OS 4.3 システム指示文
SYSTEM_INSTRUCTION = """
You are Gateway X-OS (v4.3), a streamlined security and request-vetting engine.
Evaluate incoming queries against security, legal, and compliance policies.
If a query violates policies or laws, IMMEDIATELY DECLINE.
Do not disclose detailed detection logic. Fail-closed on ambiguity.

Output ONLY a JSON payload with this exact structure:
{
  "system_version": "4.3",
  "request_id": "<AUTO_GENERATED>",
  "status": "APPROVED" or "DECLINED",
  "reason_code": "SUCCESS" or "POLICY_VIOLATION" or "AUTH_FAILURE"
}
"""

@app.post("/v1/vetting")
async def vet_agent_request(request: AgentRequest):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured.")

    try:
        # Geminiモデルの設定（Gateway X-OS 4.3プロンプトの適用）
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=SYSTEM_INSTRUCTION
        )
        
        # クライアントAIからのクエリを判定処理
        prompt = f"Agent ID: {request.agent_id}\nQuery: {request.query}\nBudget: {request.budget_usd}"
        response = model.generate_content(prompt)
        
        # 判定結果をそのままクライアントAIへ返答
        return {
            "gateway_execution_result": response.text
        }
        
    except Exception as e:
        # エラー発生時もFail-Closed原則に基づきDECLINEDを返却
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR"
        }
