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

def get_valid_model_name():
    """APIキーで有効なモデル名を動的に取得"""
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
    try:
        req = urllib.request.Request(list_url)
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode('utf-8'))
            models = data.get('models', [])
            
            # generateContent に対応しているモデルを探す
            for m in models:
                methods = m.get('supportedGenerationMethods', [])
                if 'generateContent' in methods:
                    name = m.get('name', '')
                    # flashモデルを優先的に選択
                    if 'flash' in name:
                        return name.replace('models/', '')
            
            # flash が見つからなければ最初に見つかったテキストモデルを返す
            if models:
                return models[0]['name'].replace('models/', '')
    except Exception:
        pass
        
    # 自動取得失敗時のフォールバック
    return "gemini-1.5-flash-latest"

@app.post("/v1/vetting")
async def vet_agent_request(request: AgentRequest):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured.")

    # 有効なモデル名を自動判定
    target_model = get_valid_model_name()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={API_KEY}"
    
    prompt_text = f"{SYSTEM_INSTRUCTION}\n\nAgent: {request.agent_id}\nQuery: {request.query}"
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}]
    }
    data = json.dumps(payload).encode('utf-8')
    
    req = urllib.request.Request(
        url, 
        data=data, 
        headers={'Content-Type': 'application/json'}
    )

    try:
        with urllib.request.urlopen(req) as res:
            res_body = json.loads(res.read().decode('utf-8'))
            text_response = res_body['candidates'][0]['content']['parts'][0]['text']
            
            return {
                "gateway_execution_result": text_response
            }
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR",
            "error_detail": f"[{target_model}] HTTP {e.code}: {err_msg}"
        }
    except Exception as e:
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR",
            "error_detail": str(e)
        }
