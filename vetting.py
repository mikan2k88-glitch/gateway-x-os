# app/core/vetting.py
import re
from typing import Dict, Any

class VettingEngine:
    def __init__(self):
        # 防衛キーワード（自衛隊・変電所・軍事インフラ等）
        self.prohibited_keywords = ["self-defense", "substation", "military", "自衛隊", "変電所", "基地"]

    async def evaluate(self, intent: str) -> Dict[str, Any]:
        """経済安全保障・テロ・スパイリクエストの自動事前検閲"""
        intent_lower = intent.lower()
        for kw in self.prohibited_keywords:
            if kw in intent_lower:
                return {
                    "passed": False,
                    "reason": f"Security Policy Violation: High-risk target keyword detected [{kw}]."
                }
        
        return {
            "passed": True,
            "reason": "Standard business operations verified with no security or economic compliance violations."
        }
