import os
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any

# --- クラス名の不一致を吸収するためのインポート設定 ---
from app.core.vetting import VettingEngine

# DynamicPricingEngine を PricingEngine としてインポート（名前の互換性を確保）
try:
    from app.core.pricing import DynamicPricingEngine as PricingEngine
except ImportError:
    from app.core.pricing import PricingEngine

from app.orchestrator.master import MasterOrchestrator
from app.api.v1_feedback import router as feedback_router

app = FastAPI(
    title="Gateway X-OS API Gateway",
    version="3.2.0",
    description="Physical Execution Gateway for Autonomous AI Agents"
)

# エンジン初期化
vetting_engine = VettingEngine()
pricing_engine = PricingEngine()
orchestrator = MasterOrchestrator()

# ルーター追加
app.include_router(feedback_router, prefix="/mcp/v1", tags=["Feedback"])

class MCPToolCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]

@app.get("/")
async def root():
    return {
        "status": "online",
        "system": "Gateway X-OS",
        "version": "3.2.0"
    }

@app.post("/mcp/v1/tools/call")
async def handle_mcp_tool_call(
    request: MCPToolCallRequest,
    background_tasks: BackgroundTasks
):
    if request.name != "dispatch_physical_execution":
        raise HTTPException(status_code=400, detail=f"Unknown tool name: {request.name}")

    args = request.arguments
    intent = args.get("intent", "")
    tier = args.get("tier", "express")
    estimated_cost_jpy = args.get("estimated_cost_jpy", 5000)
    client_id = args.get("client_id", "anonymous_ai")

    # 1. Vetting
    vetting_result = await vetting_engine.evaluate(intent=intent, client_id=client_id)
    if not vetting_result.passed:
        background_tasks.add_task(orchestrator.log_security_alert, client_id, intent, vetting_result.reason)
        return JSONResponse(
            status_code=403,
            content={
                "status": "DECLINED",
                "reason": vetting_result.reason,
                "vetting_assessment": vetting_result.dict()
            }
        )

    # 2. Pricing
    quote = pricing_engine.calculate_quote(
        estimated_cost_jpy=estimated_cost_jpy,
        tier=tier
    )

    # 3. Master Orchestrator
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
