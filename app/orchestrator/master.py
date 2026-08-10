from typing import Dict, Any, Optional
from app.db.repository import DatabaseRepository
from app.core.pricing import PricingEngine
from app.core.vetting import VettingEngine
from app.core.stripe_service import StripeService
from app.core.physical_execution import PhysicalExecutionRouter
from app.core.line_service import LineService
from app.core.execution_repository import ExecutionRepository
from app.sales.sales import SalesRepository
from app.sales.auth_gateway import AuthGateway
from app.sales.concierge_service import ConciergeService


class MasterOrchestrator:
    """
    Gateway X-OS Master Orchestrator
    Vetting → Pricing → 永続化 → 決済(Auth/Capture) → 現場実行(LINE連携) を統括する
    """
    def __init__(
        self,
        db_repository: Optional[DatabaseRepository] = None,
        sales_repo: Optional[SalesRepository] = None,
        stripe_service: Optional[StripeService] = None,
        line_service: Optional[LineService] = None,
        execution_repo: Optional[ExecutionRepository] = None,
    ):
        self.db = db_repository or DatabaseRepository()
        self.pricing_engine = PricingEngine()
        self.vetting_engine = VettingEngine()
        self.sales_repo = sales_repo or SalesRepository()
        self.auth_gateway = AuthGateway(self.sales_repo)
        self.concierge_service = ConciergeService(self.sales_repo)
        self.stripe_service = stripe_service or StripeService()
        self.physical_router = PhysicalExecutionRouter()
        self.line_service = line_service or LineService()
        self.execution_repo = execution_repo or ExecutionRepository()

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
        return {"event_id": quote["quote_id"]}

    async def log_security_alert(self, client_id: str, intent: str, reason: str) -> None:
        """Vetting不合格時の拒絶ログ永続化"""
        await self.db.save_vetting_log({
            "passed": False, "reason": reason, "flagged_keywords": [],
            "intent": intent, "client_id": client_id
        })
        await self.db.log_event("DECLINED", intent, reason, client_id)
        await self.concierge_service.notify_vetting_rejection(client_id, intent, reason)

    async def dispatch_to_worker(
        self, client_id: str, quote: Dict[str, Any], worker_line_user_id: str,
        payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Phase A(与信仮押さえ) → ワーカーへLINE通知 までを行い、DISPATCHED状態を返す。
        現場での実作業完了は非同期(LINE Webhook経由)で報告されるため、
        Capture(売上確定)はここでは行わない(complete_dispatch()で行う)。
        """
        intent = quote.get("intent", "")
        tier = quote.get("tier", "economy")
        execution_id = f"exec_{quote['quote_id']}"

        auth_result = await self.stripe_service.authorize_payment(quote, payment_method_id)
        if not auth_result["success"]:
            await self.db.log_event("PAYMENT_AUTH_FAILED", intent, auth_result["reason"], client_id)
            await self.concierge_service.notify_payment_failure(client_id, quote["quote_id"], auth_result["reason"])
            return {"status": "PAYMENT_FAILED", "quote_id": quote["quote_id"], "reason": auth_result["reason"]}

        await self.db.log_event(
            "PAYMENT_AUTHORIZED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id
        )

        await self.execution_repo.create_dispatch(execution_id, {
            "client_id": client_id, "quote_id": quote["quote_id"],
            "payment_intent_id": auth_result["payment_intent_id"], "tier": tier, "intent": intent,
            "price_usd": quote["price_usd"], "margin_percent": quote.get("margin_percent", 0),
            "worker_line_user_id": worker_line_user_id,
        })

        notification_text = self.line_service.build_task_notification(execution_id, intent, tier)
        line_result = await self.line_service.push_message(worker_line_user_id, notification_text)
        await self.db.log_event(
            "WORKER_NOTIFIED", intent,
            f"execution_id={execution_id}, line_push_success={line_result['success']}", client_id
        )

        return {"status": "DISPATCHED", "quote_id": quote["quote_id"], "execution_id": execution_id}

    async def complete_dispatch(self, execution_id: str, field_status: str) -> Dict[str, Any]:
        """
        LINE Webhook経由でワーカーから完了/失敗報告を受けた際に呼ぶ。
        field_status: 'completed' | 'failed'
        completedならCapture(売上確定)、failedなら与信解放(Cancel)を行う。
        """
        dispatch = await self.execution_repo.get_dispatch(execution_id)
        if dispatch is None:
            return {"status": "NOT_FOUND", "execution_id": execution_id}
        if dispatch["status"] != "DISPATCHED":
            return {"status": "ALREADY_PROCESSED", "execution_id": execution_id, "current_status": dispatch["status"]}

        client_id = dispatch["client_id"]
        intent = dispatch["intent"]

        if field_status == "completed":
            capture_result = await self.stripe_service.capture_payment(dispatch["payment_intent_id"])
            if not capture_result["success"]:
                await self.db.log_event("PAYMENT_CAPTURE_FAILED", intent, capture_result["reason"], client_id)
                await self.concierge_service.notify_payment_failure(client_id, dispatch["quote_id"], capture_result["reason"])
                await self.execution_repo.update_status(execution_id, "CAPTURE_FAILED")
                return {"status": "CAPTURE_FAILED", "execution_id": execution_id, "reason": capture_result["reason"]}

            await self.db.log_event(
                "CAPTURED", intent, f"payment_intent={dispatch['payment_intent_id']}", client_id
            )
            await self.auth_gateway.record_order_completion(client_id, intent, dispatch["tier"])
            await self.execution_repo.update_status(execution_id, "COMPLETED")
            return {
                "status": "COMPLETED", "execution_id": execution_id,
                "revenue_captured_usd": dispatch["price_usd"],
                "net_profit_usd": round(dispatch["price_usd"] * dispatch["margin_percent"] / 100, 2),
            }
        else:
            cancel_result = await self.stripe_service.cancel_payment(
                dispatch["payment_intent_id"], reason="worker_reported_failure"
            )
            await self.db.log_event("PAYMENT_CANCELED", intent, "worker reported failure", client_id)
            await self.execution_repo.update_status(execution_id, "FAILED")
            return {"status": "FAILED", "execution_id": execution_id, "payment_canceled": cancel_result["success"]}

    async def execute_physical_task(
        self, client_id: str, quote: Dict[str, Any], payment_method_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        [レガシー/テスト用フォールバック] worker_line_user_idが無い場合の同期実行パス。
        実ワーカーとのLINE連携が無い環境での動作確認用に残している。
        本番でワーカーが登録されている場合は dispatch_to_worker() を使うこと。
        """
        intent = quote.get("intent", "")
        tier = quote.get("tier", "economy")

        auth_result = await self.stripe_service.authorize_payment(quote, payment_method_id)
        if not auth_result["success"]:
            await self.db.log_event("PAYMENT_AUTH_FAILED", intent, auth_result["reason"], client_id)
            await self.concierge_service.notify_payment_failure(client_id, quote["quote_id"], auth_result["reason"])
            return {"status": "PAYMENT_FAILED", "quote_id": quote["quote_id"], "reason": auth_result["reason"]}

        await self.db.log_event(
            "PAYMENT_AUTHORIZED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id
        )
        field_result = self.physical_router.route(tier, intent)

        if field_result["field_status"] != "PRE_INSPECTED_PASSED":
            cancel_result = await self.stripe_service.cancel_payment(
                auth_result["payment_intent_id"], reason=field_result["field_status"]
            )
            await self.db.log_event("PAYMENT_CANCELED", intent, f"field_status={field_result['field_status']}", client_id)
            return {
                "status": "EXECUTION_FAILED", "quote_id": quote["quote_id"],
                "reason": field_result["field_status"], "payment_canceled": cancel_result["success"],
            }

        capture_result = await self.stripe_service.capture_payment(auth_result["payment_intent_id"])
        if not capture_result["success"]:
            await self.db.log_event("PAYMENT_CAPTURE_FAILED", intent, capture_result["reason"], client_id)
            await self.concierge_service.notify_payment_failure(client_id, quote["quote_id"], capture_result["reason"])
            return {"status": "CAPTURE_FAILED", "quote_id": quote["quote_id"], "reason": capture_result["reason"]}

        await self.db.log_event("CAPTURED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id)
        await self.auth_gateway.record_order_completion(client_id, intent, tier)

        return {
            "status": "COMPLETED", "quote_id": quote["quote_id"],
            "execution_id": f"exec_{quote['quote_id']}",
            "assigned_to": field_result["assigned_to"],
            "revenue_captured_usd": quote["price_usd"],
            "net_profit_usd": round(quote["price_usd"] * quote.get("margin_percent", 0) / 100, 2),
        }
