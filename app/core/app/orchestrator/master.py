# app/orchestrator/master.py
import uuid
from app.core.vetting import VettingEngine
from app.core.pricing import PricingEngine
from app.db.repository import DatabaseRepository

class MasterOrchestrator:
    def __init__(self):
        self.vetting_engine = VettingEngine()
        self.pricing_engine = DynamicPricingEngine()
        self.db = DatabaseRepository()

    async def handle_execution_request(self, payload: dict) -> dict:
        intent = payload.get("intent", "")
        tier = payload.get("tier", "express")
        cost_jpy = payload.get("estimated_cost_jpy", 5000)

        # 1. Vetting (審査)
        vetting_res = await self.vetting_engine.evaluate(intent)
        
        if not vetting_res["passed"]:
            # 拒絶ログの永続化
            await self.db.log_event("DECLINED", intent, vetting_res["reason"])
            return {
                "status": "DECLINED",
                "vetting_assessment": vetting_res
            }

        # 2. Dynamic Pricing (見積発行)
        quote = self.pricing_engine.calculate_quote(cost_jpy, tier)
        quote_id = f"q_{uuid.uuid4().hex[:8]}"

        # 3. 状態管理 & ログ永続化
        await self.db.log_event("QUOTED", intent, f"Quote generated: {quote_id}")

        return {
            "status": "QUOTED",
            "quote_id": quote_id,
            "tier": tier,
            "quote": quote,
            "vetting_assessment": vetting_res
        }
