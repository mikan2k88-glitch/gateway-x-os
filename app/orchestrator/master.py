from typing import Dict, Any, Optional

from app.db.repository import DatabaseRepository
from app.core.pricing import PricingEngine
from app.core.vetting import VettingEngine


class MasterOrchestrator:
    """
    Gateway X-OS Master Orchestrator
    Vetting → Pricing → 永続化 を統括する
    """

    def __init__(self, db_repository: Optional[DatabaseRepository] = None):
        self.db = db_repository or DatabaseRepository()
        self.pricing_engine = PricingEngine()
        self.vetting_engine = VettingEngine()

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
