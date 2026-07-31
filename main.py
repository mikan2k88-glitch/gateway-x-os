import os
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional

# モジュール群のインポート (提示いただいた構成に対応)
from app.core.vetting import VettingEngine
from app.core.pricing import DynamicPricingEngine
from app.orchestrator.master import MasterOrchestrator
from app.api.v1_feedback import router as feedback_router

app = FastAPI(
    title="Gateway X-OS API Gateway",
    version="3.2.0",
    description="Physical Execution Gateway for Autonomous AI Agents"
)

# 各エンジンの初期化
vetting_engine = VettingEngine()
pricing_engine = DynamicPricingEngine()
orchestrator = MasterOrchestrator()

# フィードバックAPIのルーター組み込み
app.include_router(feedback_router, prefix="/mcp/v1", tags=["Feedback"])

# --- データモデル定義 ---
class MCPToolCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]

@app.get("/")
async def root():
    return {
        "status": "online",
        "system": "Gateway X-OS",
        "version": "3.2.0",
        "architecture": "4-Tier Event-Driven Orchestration"
    }

@app.post("/mcp/v1/tools/call")
async def handle_mcp_tool_call(
    request: MCPToolCallRequest,
    background_tasks: BackgroundTasks
):
    """
    海外AIエージェントからのMCPリクエストを受け取り、
    Vetting(審査) -> Pricing(価格設定) -> Orchestration(統括) へルーティング
    """
    if request.name != "dispatch_physical_execution":
        raise HTTPException(status_code=400, detail=f"Unknown tool name: {request.name}")

    args = request.arguments
    intent = args.get("intent", "")
    tier = args.get("tier", "express")
    estimated_cost_jpy = args.get("estimated_cost_jpy", 5000)
    client_id = args.get("client_id", "anonymous_ai")

    # 1. 安全性・コンプライアンス審査 (Vetting)
    vetting_result = await vetting_engine.evaluate(intent=intent, client_id=client_id)
    if not vetting_result.passed:
        # 防衛フィルター検知（即時拒絶 & 監査ログ永続化）
        background_tasks.add_task(orchestrator.log_security_alert, client_id, intent, vetting_result.reason)
        return JSONResponse(
            status_code=403,
            content={
                "status": "DECLINED",
                "reason": vetting_result.reason,
                "vetting_assessment": vetting_result.dict()
            }
        )

    # 2. 動的価格計算 (Dynamic Pricing: 83% High-Margin Lock)
    quote = pricing_engine.calculate_quote(
        estimated_cost_jpy=estimated_cost_jpy,
        tier=tier
    )

    # 3. Master Orchestrator によるイベント登録・タスク発行
    dispatch_event = await orchestrator.create_execution_event(
        client_id=client_id,
        intent=intent,
        quote=quote,
        vetting_result=vetting_result
    )

    return {
        "status": "QUOTED",
        "quote_id": quote["quote_id"],
        "tier": quote["tier"],
        "price_usd": quote["price_usd"],
        "estimated_cost_jpy": quote["estimated_cost_jpy"],
        "margin_percent": quote["margin_percent"],
        "currency": "USD",
        "vetting_assessment": vetting_result.dict(),
        "orchestration_event_id": dispatch_event["event_id"]
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
