import os
import json
import urllib.request
import urllib.error
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

API_KEY = os.environ.get("GEMINI_API_KEY")

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
        # API エンドポイントへ直接 REST リクエストを送信
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={API_KEY}"
        
        prompt_text = f"{SYSTEM_INSTRUCTION}\n\nAgent: {request.agent_id}\nQuery: {request.query}"
        
        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt_text}]
                }
            ]
        }
        
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, 
            data=data, 
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req) as res:
            res_body = json.loads(res.read().decode('utf-8'))
            text_response = res_body['candidates'][0]['content']['parts'][0]['text']
            
            return {
                "gateway_execution_result": text_response
            }
            
    except urllib.error.HTTPError as e:
        error_response = e.read().decode('utf-8')
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR",
            "error_detail": f"HTTPError {e.code}: {error_response}"
        }
    except Exception as e:
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR",
            "error_detail": str(e)
        }
