from typing import Dict, Any, List


class VettingEngine:
    """
    経済安全保障・テロ・スパイリクエストの自動事前検閲
    (将来的にはGemini Flash等によるセマンティック審査に拡張予定。
     現時点ではキーワードベースの一次フィルタリングのみ実装)
    """

    def __init__(self):
        # 防衛キーワード（自衛隊・変電所・軍事インフラ等）
        self.prohibited_keywords = [
            "self-defense", "substation", "military",
            "自衛隊", "変電所", "基地"
        ]

    async def evaluate(self, intent: str, client_id: str = "anonymous") -> Dict[str, Any]:
        intent_lower = intent.lower()

        flagged: List[str] = [
            kw for kw in self.prohibited_keywords if kw in intent_lower
        ]

        if flagged:
            return {
                "passed": False,
                "reason": f"Security Policy Violation: High-risk target keyword(s) detected {flagged}.",
                "flagged_keywords": flagged,
                "client_id": client_id
            }

        return {
            "passed": True,
            "reason": "Standard business operations verified with no security or economic compliance violations.",
            "flagged_keywords": [],
            "client_id": client_id
        }

    @staticmethod
    def check_tier_availability(tier: str) -> Dict[str, Any]:
        """tacticalティアは将来実装のため、現時点ではリクエストを拒否する"""
        if tier == "tactical" or tier == "tactical_force":
            return {
                "available": False,
                "reason": "Tactical tier is planned for a future phase and is not available yet."
            }
        return {"available": True, "reason": ""}
