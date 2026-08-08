import os
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any
from app.core.vetting import VettingEngine
from app.core.pricing import PricingEngine
from app.orchestrator.master import MasterOrchestrator
from app.api.v1_feedback import router as feedback_router
from app.sales.auth_gateway import create_auth_gateway_router

app = FastAPI(
    title="Gateway X-OS API Gateway",
    version="3.2.0",
    description="Physical Execution Gateway for Autonomous AI Agents"
)
vetting_engine = VettingEngine()
pricing_engine = PricingEngine()
orchestrator = MasterOrchestrator()
app.include_router(feedback_router, prefix="/mcp/v1", tags=["Feedback"])
# AuthGatewayの単体ルーター(/gateway/route)。orchestrator内部で使っているsales_repoと
# 同じインスタンスを渡すことで、accountsテーブルの状態を共有する。
app.include_router(create_auth_gateway_router(orchestrator.sales_repo))


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

    # AuthGatewayによるルーティング判定(routine/concierge)。
    # ConciergeService未実装のため、現時点ではフローをブロックせず、
    # レスポンスに含めて可視化するだけに留める(ConciergeService実装時に、
    # route == "concierge" の場合はここで処理を分岐させる)。
    route_info = await orchestrator.auth_gateway.decide_route(client_id, intent, tier)
    if route_info["route"] == "concierge":
        # 初回・非定型パターンの場合はConciergeServiceにリードとして記録させる。
        # 現時点ではフローはブロックせず、通常のVetting/Pricingに進む
        # (要件明確化ダイアログは未実装のため、ここでは記録と案内メッセージ生成のみ)
        await orchestrator.concierge_service.handle_first_contact(client_id, intent, tier)

    # 0. Tier availability check (tactical は未実装のため拒否)
    tier_check = VettingEngine.check_tier_availability(tier)
    if not tier_check["available"]:
        return JSONResponse(
            status_code=400,
            content={"status": "REJECTED", "reason": tier_check["reason"]}
        )

    # 1. Vetting
    vetting_result = await vetting_engine.evaluate(intent=intent, client_id=client_id)
    if not vetting_result["passed"]:
        background_tasks.add_task(
            orchestrator.log_security_alert, client_id, intent, vetting_result["reason"]
        )
        return JSONResponse(
            status_code=403,
            content={
                "status": "DECLINED",
                "reason": vetting_result["reason"],
                "vetting_assessment": vetting_result
            }
        )

    # 2. Pricing
    quote = pricing_engine.calculate_quote(
        estimated_cost_jpy=estimated_cost_jpy,
        tier=tier
    )

    # 3. Master Orchestrator（永続化。この中でAuthGatewayの承認カウントも加算される）
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
        "vetting_assessment": vetting_result,
        "orchestration_event_id": dispatch_event["event_id"],
        "routing": route_info["route"],  # 'routine' | 'concierge'(現時点では表示のみ)
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
