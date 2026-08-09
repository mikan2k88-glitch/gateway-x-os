from typing import Dict, Any, List, Optional

from .semantic_safety import SemanticSafetyReviewer


class VettingEngine:
    """
    経済安全保障・テロ・スパイリクエストの自動事前検閲(キーワードベース)
    + 一般的な違法・危険タスクのセマンティック審査(SemanticSafetyReviewer)

    キーワードフィルタは軍事・経済安全保障関連の固定リストのみを見るため、
    嫌がらせ・ストーキング・危険物運搬等の一般的な違法/危険タスクは検知できない。
    この穴を埋めるため、docstringで元々予告されていた「Gemini Flash等による
    セマンティック審査」をSemanticSafetyReviewerとして実装し、両方のチェックを
    通過した場合のみ合格とする。
    """
    def __init__(self, semantic_reviewer: Optional[SemanticSafetyReviewer] = None):
        # 防衛キーワード（自衛隊・変電所・軍事インフラ等）
        self.prohibited_keywords = [
            "self-defense", "substation", "military",
            "自衛隊", "変電所", "基地"
        ]
        self.semantic_reviewer = semantic_reviewer or SemanticSafetyReviewer()

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

        # キーワードフィルタを通過した場合のみ、セマンティック審査に進む
        # (明らかな軍事キーワードでAPI呼び出しコストをかけないための順序)
        semantic_result = await self.semantic_reviewer.review(intent)
        if not semantic_result["is_safe"]:
            return {
                "passed": False,
                "reason": f"Semantic Safety Review Violation: {semantic_result['reasoning']} "
                          f"(categories: {semantic_result['risk_categories']})",
                "flagged_keywords": semantic_result["risk_categories"],
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
