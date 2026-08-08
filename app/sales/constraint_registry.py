from dataclasses import dataclass
from enum import Enum
from typing import List


class ImplementationPhase(str, Enum):
    B2B_PROCUREMENT = "b2b_procurement"   # 現行の最優先フェーズ(需要が明確なため)
    TACTICAL_TIER = "tactical_tier"       # NDA締結の精鋭チーム。将来目標、現時点では未実装
    NOT_STARTED = "not_started"


class LegalRiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    HIGH = "high"  # 例: Gateway X社自身が発注者になる構造(職業安定法上のリスク)


@dataclass
class ConstraintCheckResult:
    passed: bool
    reason: str


@dataclass
class ConstraintContext:
    """StrategyExecutorが1つの戦略案を評価する際に渡す入力情報"""
    implementation_phase: ImplementationPhase
    legal_risk: LegalRiskLevel
    requires_external_api: bool
    external_api_available: bool
    requires_payment_support: bool = False
    payment_supported: bool = True  # Stripe対応済みのためデフォルトTrue
    segment: str = "b2b_procurement"


class ConstraintRegistry:
    """
    これまで会話の中にしかなかった制約情報を一元管理するコード上の台帳。
    StrategyExecutorはこれを参照して承認/却下を判断する。
    """

    # 現時点で実行してよいフェーズ(tacticalティア等はまだここに含めない)
    ACTIVE_PHASES = {ImplementationPhase.B2B_PROCUREMENT}

    # 優先セグメント(需要が明確なB2B調達を最優先とする方針)
    PRIORITY_SEGMENTS = {"b2b_procurement"}

    def check(self, ctx: ConstraintContext) -> List[ConstraintCheckResult]:
        results: List[ConstraintCheckResult] = []

        if ctx.implementation_phase not in self.ACTIVE_PHASES:
            results.append(ConstraintCheckResult(
                False, f"実装フェーズ '{ctx.implementation_phase.value}' は現時点で未実装のためスコープ外"
            ))
        else:
            results.append(ConstraintCheckResult(True, "実装フェーズは現行スコープ内"))

        if ctx.legal_risk == LegalRiskLevel.HIGH:
            results.append(ConstraintCheckResult(
                False, "職業安定法等に抵触しうる高リスクな法的構造(Gateway X社自身が発注者になる等)"
            ))
        else:
            results.append(ConstraintCheckResult(True, "法的リスクは許容範囲"))

        if ctx.requires_external_api and not ctx.external_api_available:
            results.append(ConstraintCheckResult(
                False, "必要な公式外部APIが利用不可。タイミーワーカー等の人力代行フローの検討が必要"
            ))
        else:
            results.append(ConstraintCheckResult(True, "外部API要件は満たされている、または不要"))

        if ctx.requires_payment_support and not ctx.payment_supported:
            results.append(ConstraintCheckResult(False, "決済手段(Stripe)が未対応"))
        else:
            results.append(ConstraintCheckResult(True, "決済要件は満たされている、または不要"))

        return results

    def is_priority_segment(self, segment: str) -> bool:
        return segment in self.PRIORITY_SEGMENTS
