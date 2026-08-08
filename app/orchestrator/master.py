from typing import Dict, Any, Optional
from app.db.repository import DatabaseRepository
from app.core.pricing import PricingEngine
from app.core.vetting import VettingEngine
from app.sales.sales import SalesRepository
from app.sales.auth_gateway import AuthGateway
from app.sales.concierge_service import ConciergeService


class MasterOrchestrator:
    """
    Gateway X-OS Master Orchestrator
    Vetting → Pricing → 永続化 を統括する
    """
    def __init__(
        self,
        db_repository: Optional[DatabaseRepository] = None,
        sales_repo: Optional[SalesRepository] = None,
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

    async def create_execution_event(
        self,
        client_id: str,
        intent: str,
        quote: Dict[str, Any],
        vetting_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Vetting通過後、見積を永続化しイベントIDを発行する"""
        quote_data = {**quote, "client_id": client_id, "intent": intent, "status": "QUOTED"}
        await self.db.save_quote(quote_data)
        await self.db.save_vetting_log({**vetting_result, "intent": intent, "client_id": client_id})
        await self.db.log_event("QUOTED", intent, f"Quote generated: {quote['quote_id']}", client_id)

        # AuthGateway: 承認済み発注として記録し、次回以降の定型ルート判定に使う
        tier = quote.get("tier", "economy")
        await self.auth_gateway.record_order_completion(client_id, intent, tier)

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
