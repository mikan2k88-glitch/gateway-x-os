import asyncio
from typing import Dict, Any, List, Optional

# --- 必要なモジュール・クラスのインポート ---
from app.db.repository import DatabaseRepository
from app.core.pricing import DynamicPricingEngine  # <-- このインポートが不足していました
from app.core.vetting import VettingEngine          # <-- 念のためVettingEngineも確認


class MasterOrchestrator:
    """
    Gateway X-OS Phase 2 Master Orchestrator (CEO/COO-OS)
    全社的なステータス管理、部門間連携、売上・利益マージンの集計を統括する。
    """

    def __init__(self, db_repository: Optional[DatabaseRepository] = None):
        self.db = db_repository or DatabaseRepository()
        self.pricing_engine = DynamicPricingEngine()
        self.vetting_engine = VettingEngine()

    async def process_task_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        タスクリクエストの統合処理フロー:
        1. Vetting (審査)
        2. Pricing (動的価格算出)
        3. ログ永続化
        """
        intent = payload.get("intent", "")
        tier = payload.get("tier", "economy")
        cost_jpy = payload.get("estimated_cost_jpy", 5000)
        client_id = payload.get("client_id", "anonymous")

        # 1. 審査 (Vetting)
        vetting_result = await self.vetting_engine.evaluate_intent(intent)

        # 監査ログの保存
        await self.db.save_vetting_log({
            "client_id": client_id,
            "intent": intent,
            "passed": vetting_result.get("passed", False),
            "reason": vetting_result.get("reason", ""),
            "flagged_keywords": vetting_result.get("flagged_keywords", [])
        })

        if not vetting_result.get("passed", False):
            return {
                "status": "DECLINED",
                "reason": vetting_result.get("reason", "Security vetting failed."),
                "vetting_assessment": vetting_result
            }

        # 2. 価格計算 (Pricing)
        quote = self.pricing_engine.calculate_quote(
            estimated_cost_jpy=cost_jpy,
            tier=tier
        )

        quote_data = {
            "quote_id": quote["quote_id"],
            "client_id": client_id,
            "intent": intent,
            "tier": tier,
            "price_usd": quote["price_usd"],
            "cost_jpy": cost_jpy,
            "margin_percent": quote["margin_percent"],
            "status": "QUOTED"
        }

        # 見積もりの保存
        await self.db.save_quote(quote_data)

        return {
            "status": "QUOTED",
            "quote": quote,
            "vetting_assessment": vetting_result
        }
