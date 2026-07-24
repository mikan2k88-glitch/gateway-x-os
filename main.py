import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import google.generativeai as genai

# アプリ起動時にAPIキーを設定
API_KEY = os.environ.get("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

app = FastAPI(
    title="Gateway X-OS Vetting Engine",
    version="4.3",
    description="Autonomous AI Agent Request Vetting Engine"
)

class AgentRequest(BaseModel):
    agent_id: str
    query: str
    budget_usd: Optional[float] = 0.0
    intent_category: Optional[str] = "GENERAL"

SYSTEM_INSTRUCTION = """
You are Gateway X-OS (v4.3), a security and request-vetting engine.
Evaluate queries against security and compliance policies.
If safe, status is APPROVED. If unsafe, status is DECLINED.

Output ONLY a JSON string:
{
  "system_version": "4.3",
  "request_id": "XOS-APPROVED",
  "status": "APPROVED",
  "reason_code": "SUCCESS"
}
"""

@app.post("/v1/vetting")
async def vet_agent_request(request: AgentRequest):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured.")

    try:
        # 無料枠で確実に利用可能な gemini-1.5-flash を指定
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash"
        )
        
        prompt = f"{SYSTEM_INSTRUCTION}\n\nAgent: {request.agent_id}\nQuery: {request.query}"
        response = model.generate_content(prompt)
        
        return {
            "gateway_execution_result": response.text
        }
        
    except Exception as e:
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR",
            "error_detail": str(e)
        }
