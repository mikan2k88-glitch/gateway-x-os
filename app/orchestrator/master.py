from typing import Dict, Any, Optional
from app.db.repository import DatabaseRepository
from app.core.pricing import PricingEngine
from app.core.vetting import VettingEngine
from app.core.stripe_service import StripeService
from app.core.physical_execution import PhysicalExecutionRouter
from app.sales.sales import SalesRepository
from app.sales.auth_gateway import AuthGateway
from app.sales.concierge_service import ConciergeService


class MasterOrchestrator:
    """
    Gateway X-OS Master Orchestrator
    Vetting → Pricing → 永続化 → 決済(Auth/Capture) → 現場実行 を統括する
    """
    def __init__(
        self,
        db_repository: Optional[DatabaseRepository] = None,
        sales_repo: Optional[SalesRepository] = None,
        stripe_service: Optional[StripeService] = None,
    ):
        self.db = db_repository or DatabaseRepository()
        self.pricing_engine = PricingEngine()
        self.vetting_engine = VettingEngine()
        # AuthGatewayが参照するaccountsテーブルはsales_repo側にある。
        # db_repository(operations用)とは別のSQLiteファイル接続だが、
        # 同じgateway_x.dbを指す想定なのでdb_pathを揃えて渡すこと。
        self.sales_repo = sales_repo or SalesRepository()
        self.auth_gateway = AuthGateway(self.sales_repo)
        self.concierge_service = ConciergeService(self.sales_repo)
        self.stripe_service = stripe_service or StripeService()
        self.physical_router = PhysicalExecutionRouter()

    async def create_execution_event(
        self,
        client_id: str,
        intent: str,
        quote: Dict[str, Any],
        vetting_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Vetting通過後、見積を永続化しイベントIDを発行する(まだ決済は行わない)"""
        quote_data = {**quote, "client_id": client_id, "intent": intent, "status": "QUOTED"}
        await self.db.save_quote(quote_data)
        await self.db.save_vetting_log({**vetting_result, "intent": intent, "client_id": client_id})
        await self.db.log_event("QUOTED", intent, f"Quote generated: {quote['quote_id']}", client_id)

        # AuthGatewayの承認カウントはここでは加算しない。
        # 「見積が出ただけ」は本当の意味での承認済み発注ではないため、
        # Capture確定(execute_physical_task内)まで持ち越す。

        return {"event_id": quote["quote_id"]}

    async def log_security_alert(self, client_id: str, intent: str, reason: str) -> None:
        """Vetting不合格時の拒絶ログ永続化"""
        await self.db.save_vetting_log({
            "passed": False,
            "reason": reason,
            "flagged_keywords": [],
            "intent": intent,
            "client_id": client_id
        })
        await self.db.log_event("DECLINED", intent, reason, client_id)
        # 却下された発注はAuthGatewayの承認カウントに影響しない(record_order_completionを呼ばない)
        # ConciergeServiceに却下理由を渡し、クライアントへ返す案内文を組み立ててもらう
        await self.concierge_service.notify_vetting_rejection(client_id, intent, reason)

    async def execute_physical_task(
        self, client_id: str, quote: Dict[str, Any], payment_method_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Phase A(与信仮押さえ) → 現場ルーティング → Phase B(売上確定) の一気通貫パイプライン。
        quote は /mcp/v1/tools/call が返した見積オブジェクトをそのまま渡す想定
        (quote_id/tier/intent/price_usd/estimated_cost_jpy/margin_percent/currencyを含む)。

        承認済み発注のカウント(AuthGateway.record_order_completion)は、
        Capture成功時にのみ加算する。見積もりだけで決済に至らなかった注文は
        定型ルート判定の実績に含めない。
        """
        intent = quote.get("intent", "")
        tier = quote.get("tier", "economy")

        # Phase A: 与信仮押さえ
        auth_result = await self.stripe_service.authorize_payment(quote, payment_method_id)
        if not auth_result["success"]:
            await self.db.log_event(
                "PAYMENT_AUTH_FAILED", intent, auth_result["reason"], client_id
            )
            await self.concierge_service.notify_payment_failure(
                client_id, quote["quote_id"], auth_result["reason"]
            )
            return {
                "status": "PAYMENT_FAILED",
                "quote_id": quote["quote_id"],
                "reason": auth_result["reason"],
            }

        await self.db.log_event(
            "PAYMENT_AUTHORIZED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id
        )

        # 現場ルーティング + プレ検収
        field_result = self.physical_router.route(tier, intent)

        if field_result["field_status"] != "PRE_INSPECTED_PASSED":
            # プレ検収不合格 or tactical等の未実装ティア -> 与信解放(例外系リカバリー)
            cancel_result = await self.stripe_service.cancel_payment(
                auth_result["payment_intent_id"], reason=field_result["field_status"]
            )
            await self.db.log_event(
                "PAYMENT_CANCELED", intent, f"field_status={field_result['field_status']}", client_id
            )
            return {
                "status": "EXECUTION_FAILED",
                "quote_id": quote["quote_id"],
                "reason": field_result["field_status"],
                "payment_canceled": cancel_result["success"],
            }

        # Phase B: 売上確定
        capture_result = await self.stripe_service.capture_payment(auth_result["payment_intent_id"])
        if not capture_result["success"]:
            await self.db.log_event(
                "PAYMENT_CAPTURE_FAILED", intent, capture_result["reason"], client_id
            )
            await self.concierge_service.notify_payment_failure(
                client_id, quote["quote_id"], capture_result["reason"]
            )
            return {
                "status": "CAPTURE_FAILED",
                "quote_id": quote["quote_id"],
                "reason": capture_result["reason"],
            }

        await self.db.log_event(
            "CAPTURED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id
        )
        # Capture成功 = 本当の意味での承認済み発注。ここでAuthGatewayのカウントを加算する
        await self.auth_gateway.record_order_completion(client_id, intent, tier)

        return {
            "status": "COMPLETED",
            "quote_id": quote["quote_id"],
            "execution_id": f"exec_{quote['quote_id']}",
            "assigned_to": field_result["assigned_to"],
            "revenue_captured_usd": quote["price_usd"],
            "net_profit_usd": round(quote["price_usd"] * quote.get("margin_percent", 0) / 100, 2),
        }
