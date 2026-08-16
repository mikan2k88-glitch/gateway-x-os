from typing import Any, Dict, Optional

from .sales import SalesRepository
from .strategy_planner import StrategyPlanner
from .strategy_executor import StrategyExecutor
from .constraint_registry import ConstraintContext


class SalesEngine:
    """
    StrategyPlanner(討論) と StrategyExecutor(制約評価) を繋ぐオーケストレーション層。

    AuthGateway / ConciergeService / OutreachService はまだ実装していないため、
    「戦略サイクルを1つ回す」というコア動作だけを単独で使える形にしている。
    これにより、他コンポーネントの実装を待たずに戦略サイクル自体は一通り検証できる。
    """

    def __init__(self, planner: StrategyPlanner, executor: StrategyExecutor, sales_repo: SalesRepository):
        self.planner = planner
        self.executor = executor
        self.sales_repo = sales_repo

    async def run_strategy_cycle(
        self, topic: str, context: str, constraint_ctx: ConstraintContext
    ) -> Dict[str, Any]:
        """
        討論 -> (収束したら)制約評価 まで一気通貫で実行する。

        - 討論が3ラウンドで未収束の場合: StrategyPlanner側で既に 'pending' 判定済みのため、
          ここでは制約評価をスキップしてそのまま返す(まだ承認/却下を判断できる段階ではない)
        - 討論が収束した場合: StrategyExecutor.evaluate_cycle() を呼び、承認/却下を確定する
        """
        debate_result = await self.planner.run_debate_cycle(topic, context)

        if not debate_result["converged"]:
            return {
                "cycle_id": debate_result["cycle_id"],
                "stage": "pending",
                "debate": debate_result,
                "evaluation": None,
            }

        evaluation = await self.executor.evaluate_cycle(debate_result["cycle_id"], constraint_ctx)

        feature_request = None
        if evaluation["status"] == "approved":
            # 承認された(=実際に実行される)戦略案についてのみ機能要望を検出する。
            # 却下/保留案について機能要望を検討するのは無駄なコストなので行わない。
            detection = await self.planner.detect_feature_request(debate_result["final_proposal"])
            if detection["feature_needed"]:
                await self.sales_repo.create_feature_request(
                    debate_result["cycle_id"], detection["title"], detection["description"]
                )
                feature_request = detection

        return {
            "cycle_id": debate_result["cycle_id"],
            "stage": evaluation["status"],  # 'approved' | 'rejected'
            "debate": debate_result,
            "evaluation": evaluation,
            "feature_request": feature_request,
        }

    async def report_capacity(
        self, supply_channel: str, available_capacity: float, demand: float
    ) -> Dict[str, Any]:
        """
        供給キャパシティ監視からの通知を受け取り、Executorの判定を返す。
        pause_outreach / seek_alternative_supply が立った場合の実際の配線
        (OutreachServiceへの一時停止指示、StrategyPlannerへの代替供給源探索指示)は、
        それぞれのコンポーネント実装時にこの戻り値を見て呼び出す想定。
        """
        return await self.executor.handle_capacity_alert(supply_channel, available_capacity, demand)
