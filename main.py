import os
import logging
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
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

logger = logging.getLogger("gateway_x")
logging.basicConfig(level=logging.INFO)

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
    worker_line_user_id: str = None  # 指定があればLINE経由の非同期フローに切り替わる


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
    /mcp/v1/tools/call が返した見積(quote)を受け取り、決済(Auth)を行う。

    worker_line_user_id が指定されている場合: LINEでワーカーへタスク通知し、
    status: "DISPATCHED" を返す(Captureは後でLINE Webhook経由で非同期に行われる)。
    クライアントAIは返ってきた execution_id で GET /mcp/v1/tools/execute/{execution_id}
    をポーリングして進捗を確認できる。

    worker_line_user_id が無い場合: レガシーの同期実行パス(execute_physical_task)を使う。
    """
    if request.worker_line_user_id:
        result = await orchestrator.dispatch_to_worker(
            client_id=request.client_id,
            quote=request.quote,
            worker_line_user_id=request.worker_line_user_id,
            payment_method_id=request.payment_method_id,
        )
    else:
        result = await orchestrator.execute_physical_task(
            client_id=request.client_id,
            quote=request.quote,
            payment_method_id=request.payment_method_id,
        )
    return result


@app.get("/mcp/v1/tools/execute/{execution_id}")
async def get_execution_status(execution_id: str):
    """クライアントAIがDISPATCHED後の進捗をポーリングするためのエンドポイント"""
    dispatch = await orchestrator.execution_repo.get_dispatch(execution_id)
    if dispatch is None:
        raise HTTPException(status_code=404, detail="execution_id not found")
    return dispatch


@app.post("/line/webhook")
async def line_webhook(request: Request):
    """
    LINE Messaging APIからのWebhook。ワーカーが「完了 exec_xxx」/「失敗 exec_xxx」
    と返信した際にここへ届く。署名を検証したうえで、対応する派遣(dispatch)を
    完了/失敗として処理する。
    """
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    if not orchestrator.line_service.verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    results = []
    for event in payload.get("events", []):
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            continue

        text = event["message"]["text"].strip()
        line_user_id = event.get("source", {}).get("userId", "")
        reply_token = event.get("replyToken", "")

        # ワーカー登録の仕組みがまだ無いため、動作確認用に受信したuserIdをログへ出力する。
        # Renderのログでこの行を探せば、worker_line_user_idに設定すべき値が分かる。
        logger.info(f"[LINE webhook] Received message from userId={line_user_id}: {text!r}")

        # メッセージ本文にexecution_idが含まれていればそれを優先し、
        # 無ければそのワーカーの直近のDISPATCHED案件を対象にする(簡易フォールバック)
        execution_id = None
        for token in text.split():
            if token.startswith("exec_"):
                execution_id = token
                break
        if execution_id is None:
            latest = await orchestrator.execution_repo.find_latest_dispatched_by_worker(line_user_id)
            execution_id = latest["execution_id"] if latest else None

        if execution_id is None:
            continue

        if "完了" in text:
            result = await orchestrator.complete_dispatch(execution_id, "completed")
            reply_text = "報告ありがとうございます。完了処理をしました。" if result["status"] == "COMPLETED" \
                else f"処理でエラーが発生しました({result['status']})。運営にご連絡ください。"
        elif "失敗" in text or "できません" in text:
            result = await orchestrator.complete_dispatch(execution_id, "failed")
            reply_text = "承知しました。与信を解放しました。ご対応ありがとうございました。"
        else:
            result = None
            reply_text = "「完了」または「失敗」に管理番号を添えて返信してください。"

        if reply_token:
            await orchestrator.line_service.reply_message(reply_token, reply_text)
        results.append(result)

    return {"status": "ok", "processed": len(results)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
