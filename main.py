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
from app.sales.concierge_service import create_concierge_router
from app.core.rate_limiter import RateLimiter

# レートリミッター設定: 1クライアントあたり60秒間に20リクエストまで(DoS対策)
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60.0

# カーディング攻撃検知設定: 5分以内に$5未満の見積が5件以上ある場合は疑わしいパターンとみなす
CARDING_WINDOW_SECONDS = 300.0
CARDING_PRICE_THRESHOLD_USD = 5.0
CARDING_MAX_SMALL_QUOTES = 5

app = FastAPI(
    title="Gateway X-OS API Gateway",
    version="3.2.0",
    description="Physical Execution Gateway for Autonomous AI Agents"
)
vetting_engine = VettingEngine()
pricing_engine = PricingEngine()
orchestrator = MasterOrchestrator()
rate_limiter = RateLimiter()
app.include_router(feedback_router, prefix="/mcp/v1", tags=["Feedback"])
# AuthGatewayの単体ルーター(/gateway/route)。orchestrator内部で使っているsales_repoと
# 同じインスタンスを渡すことで、accountsテーブルの状態を共有する。
app.include_router(create_auth_gateway_router(orchestrator.sales_repo))
# ConciergeServiceの対話エンドポイント(/concierge/message)。自由記述の依頼から
# intent/tier/estimated_cost_jpyを抽出し、不足があれば聞き返す質問を返す。
# クライアントAIはここで情報が揃うまでやり取りし、揃った結果を
# /mcp/v1/tools/call の arguments としてそのまま渡す想定。
app.include_router(create_concierge_router(orchestrator.concierge_service))


class MCPToolCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]


class ExecuteRequest(BaseModel):
    client_id: str
    quote: Dict[str, Any]
    payment_method_id: str = None


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

    # レートリミッター: 海外AIクライアントの無限連打(DoS)を防止する一次防御
    if not rate_limiter.check(client_id, RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS):
        return JSONResponse(
            status_code=429,
            content={
                "status": "RATE_LIMITED",
                "reason": f"Too many requests. Limit: {RATE_LIMIT_MAX_REQUESTS} per {int(RATE_LIMIT_WINDOW_SECONDS)}s.",
            }
        )

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

    # 2.5 カーディング攻撃検知: 少額見積の大量発行パターンをチェック
    # (盗難カードの有効性を「与信が通るか」だけで検証する手口への対策)
    await orchestrator.sales_repo.log_quote_attempt(client_id, quote["price_usd"])
    small_quote_count = await orchestrator.sales_repo.count_recent_small_quotes(
        client_id, CARDING_WINDOW_SECONDS, CARDING_PRICE_THRESHOLD_USD
    )
    if small_quote_count >= CARDING_MAX_SMALL_QUOTES:
        reason = (
            f"Suspicious pattern: {small_quote_count} quotes under "
            f"${CARDING_PRICE_THRESHOLD_USD} within {int(CARDING_WINDOW_SECONDS)}s "
            f"(possible carding attempt)."
        )
        background_tasks.add_task(orchestrator.log_security_alert, client_id, intent, reason)
        return JSONResponse(
            status_code=403,
            content={"status": "FLAGGED_FOR_REVIEW", "reason": reason}
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


@app.post("/mcp/v1/tools/execute")
async def handle_execute(request: ExecuteRequest):
    """
    /mcp/v1/tools/call が返した見積(quote)を受け取り、
    Phase A(与信仮押さえ) → 現場ルーティング/プレ検収 → Phase B(売上確定) を実行する。
    決済(Stripe)が絡むため、見積発行(QUOTED)とは別エンドポイントに分離している。
    """
    result = await orchestrator.execute_physical_task(
        client_id=request.client_id,
        quote=request.quote,
        payment_method_id=request.payment_method_id,
    )
    return result


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
