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

def fetch_available_models():
    """APIキーで利用可能なモデル一覧を取得"""
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
    try:
        req = urllib.request.Request(list_url)
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode('utf-8'))
            models = []
            for m in data.get('models', []):
                if 'generateContent' in m.get('supportedGenerationMethods', []):
                    models.append(m['name'].replace('models/', ''))
            return models
    except Exception as e:
        print(f"⚠️ [Auto-Debug] モデル一覧取得エラー: {e}")
        return []

def call_gemini_auto(prompt_text: str):
    """有効なモデルを順次自動試行して結果を返す"""
    # 探索順序: 自動検出されたモデル -> 予備候補
    candidates = fetch_available_models() + [
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro-latest",
        "gemini-1.5-flash",
        "gemini-1.0-pro"
    ]
    
    # 重複を除去
    seen = set()
    unique_candidates = [x for x in candidates if not (x in seen or seen.add(x))]

    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
    data = json.dumps(payload).encode('utf-8')

    last_error = ""
    for model_name in unique_candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={API_KEY}"
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as res:
                res_body = json.loads(res.read().decode('utf-8'))
                text = res_body['candidates'][0]['content']['parts'][0]['text']
                print(f"✅ [Auto-Debug] 成功モデル: {model_name}")
                return text, None
        except urllib.error.HTTPError as e:
            last_error = f"[{model_name}] HTTP {e.code}"
            continue
        except Exception as e:
            last_error = f"[{model_name}] {str(e)}"
            continue

    return None, last_error

@app.on_event("startup")
async def startup_event():
    """起動時に自動デバッグを実行"""
    if not API_KEY:
        print("❌ [Auto-Debug] APIキーが未設定です")
        return
    print("🚀 [Auto-Debug] アプリ起動: 内部自動接続テストを開始...")
    res, err = call_gemini_auto("Ping test")
    if res:
        print("🎉 [Auto-Debug] 接続テスト完全成功！即座に利用可能です。")
    else:
        print(f"❌ [Auto-Debug] 接続テスト失敗: {err}")

@app.post("/v1/vetting")
async def vet_agent_request(request: AgentRequest):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured.")

    prompt_text = f"{SYSTEM_INSTRUCTION}\n\nAgent: {request.agent_id}\nQuery: {request.query}"
    text_response, err = call_gemini_auto(prompt_text)

    if text_response:
        return {"gateway_execution_result": text_response}
    else:
        return {
            "system_version": "4.3",
            "request_id": f"XOS-{request.agent_id}-ERROR",
            "status": "DECLINED",
            "reason_code": "SYSTEM_ERROR",
            "error_detail": err
        }
