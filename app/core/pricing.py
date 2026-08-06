import uuid
from typing import Dict, Any


class PricingEngine:
    """Naval-Collison 3-Tier Model による動的見積エンジン"""

    TIER_MULTIPLIERS = {
        "economy": 1.5,
        "express": 2.5,
        "tactical": 5.0,
        "tactical_force": 5.0,  # 互換用エイリアス
    }

    DEFAULT_USD_JPY_RATE = 155.0  # 為替レート（将来的に外部API同期可能）

    @classmethod
    def calculate_quote(
        cls,
        estimated_cost_jpy: float,
        tier: str,
        usd_jpy_rate: float = DEFAULT_USD_JPY_RATE
    ) -> Dict[str, Any]:
        multiplier = cls.TIER_MULTIPLIERS.get(tier, 2.0)

        total_usd = round((estimated_cost_jpy * multiplier) / usd_jpy_rate, 2)
        margin_percent = round((1 - (1 / multiplier)) * 100, 1)
        quote_id = f"q_{uuid.uuid4().hex[:8]}"

        return {
            "quote_id": quote_id,
            "price_usd": total_usd,
            "estimated_cost_jpy": estimated_cost_jpy,
            "cost_jpy": estimated_cost_jpy,  # repository.save_quote互換のキー名
            "margin_percent": margin_percent,
            "tier": tier,
            "currency": "USD"
        }
