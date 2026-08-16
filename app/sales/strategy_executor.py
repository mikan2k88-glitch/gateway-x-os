from typing import Any, Dict, Optional

from .sales import SalesRepository
from .constraint_registry import ConstraintRegistry, ConstraintContext


class StrategyExecutor:
    """
    StrategyPlannerが討論した戦略案(strategy_cycles)を、ConstraintRegistryの制約に
    照らして承認/却下する。

    スコアリング方式は「単純合否」を採用した(重み付け総合スコアではない)。理由:
    - 制約(法的リスク/実装フェーズ/外部API可用性/決済対応)はどれも「1つでも違反すれば
      実行してはいけない」性質のハードな制約であり、重み付けで相殺してよいものではない
    - 現時点でスコアの重み付けを決める判断材料(過去の実行結果の蓄積)がまだ無い
    - 判断理由が明確になるため、KB(strategy_cycles)に却下理由をそのまま保存でき、
      次回のStrategyPlannerの討論時に「なぜ却下されたか」を参照しやすい
    将来的に実行結果データが十分蓄積された段階で、重み付けスコア方式への切り替えを検討する。
    """

    def __init__(
        self,
        sales_repo: SalesRepository,
        constraint_registry: Optional[ConstraintRegistry] = None,
    ):
        self.sales_repo = sales_repo
        self.constraints = constraint_registry or ConstraintRegistry()

    async def evaluate_cycle(self, cycle_id: int, ctx: ConstraintContext) -> Dict[str, Any]:
        results = self.constraints.check(ctx)
        failed = [r for r in results if not r.passed]

        if failed:
            status = "rejected"
            decision = "却下"
            reason = " / ".join(r.reason for r in failed)
        else:
            status = "approved"
            decision = "承認"
            reason = " / ".join(r.reason for r in results)

        await self.sales_repo.resolve_cycle(cycle_id, status=status, decision=decision, reason=reason)

        return {
            "cycle_id": cycle_id,
            "status": status,
            "decision": decision,
            "reason": reason,
            "failed_checks": [r.reason for r in failed],
        }

    async def handle_capacity_alert(
        self, supply_channel: str, available_capacity: float, demand: float
    ) -> Dict[str, Any]:
        """
        供給キャパシティ監視からのアラートを受けて対応レベルを判定する。
        - normal: 供給が需要を満たしている
        - medium: 供給が需要の50%以上だが不足している → 新規獲得ペースを落とす
        - high: 供給が需要の50%未満 → 新規獲得を一時停止 + 代替供給源の探索が必要
        (フロー図の「逼迫時: 新規獲得を一時停止をOutreachへ指示」「代替供給源の探索をPlannerに指示」に対応。
        実際にOutreachService/StrategyPlannerを呼び出す配線は、それぞれの実装時に
        戻り値の pause_outreach / seek_alternative_supply を見て呼び出し元が行う)
        """
        if available_capacity >= demand:
            level = "normal"
            action = ""
        elif available_capacity >= demand * 0.5:
            level = "medium"
            action = "新規獲得ペースを落とす"
        else:
            level = "high"
            action = "新規獲得を一時停止・代替供給源の探索が必要"

        await self.sales_repo.log_capacity_alert(
            supply_channel=supply_channel,
            available_capacity=available_capacity,
            demand=demand,
            alert_level=level,
            action_taken=action,
        )

        return {
            "supply_channel": supply_channel,
            "alert_level": level,
            "action_taken": action,
            "pause_outreach": level == "high",
            "seek_alternative_supply": level == "high",
        }
