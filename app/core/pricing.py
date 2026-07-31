# app/core/pricing.py
from typing import Dict, Any

class DynamicPricingEngine:
    @staticmethod
    def calculate_quote(estimated_cost_jpy: float, tier: str) -> Dict[str, Any]:
        """ Naval-Collison 3-Tier Model による83%純利益マージン設計 """
        tier_multipliers = {
            "economy": 1.5,
            "express": 2.5,
            "tactical_force": 5.0
        }
        multiplier = tier_multipliers.get(tier, 2.0)
        usd_jpy_rate = 155.0  # 為替レート（自動同期可能）
        
        # マージン乗算後の請求単価（USD）
        total_usd = round((estimated_cost_jpy * multiplier) / usd_jpy_rate, 2)
        margin_percent = round((1 - (1 / multiplier)) * 100, 1)

        return {
            "price_usd": total_usd,
            "estimated_cost_jpy": estimated_cost_jpy,
            "margin_percent": margin_percent,
            "currency": "USD"
        }
