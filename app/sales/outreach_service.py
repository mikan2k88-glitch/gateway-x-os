from typing import Any, Dict, List, Optional

from .sales import SalesRepository


class OutreachService:
    """
    StrategyExecutorが承認した戦略案を受けて、実際のリード獲得アクションを行う。

    - 新規リードへのトライアル案内(lead -> trial)
    - 既存クライアントへのフォローアップ/アップセル(active維持)
    - report_capacity() が pause_outreach=True を返した場合の新規獲得の一時停止

    実際の送信手段(メール/API等)は未実装。ここでは「何を・誰に・どう実行したか」を
    leads / event的な記録として残す責務に留め、送信部分は後で差し込めるようにしている。
    """

    def __init__(self, sales_repo: SalesRepository):
        self.sales_repo = sales_repo
        self._outreach_paused = False

    def pause(self) -> None:
        """StrategyExecutorのcapacity判定でpause_outreach=Trueが返された場合に呼ぶ"""
        self._outreach_paused = True

    def resume(self) -> None:
        self._outreach_paused = False

    @property
    def is_paused(self) -> bool:
        return self._outreach_paused

    async def invite_trial(self, client_id: str, source: str, notes: str = "") -> Dict[str, Any]:
        """
        承認された戦略案に基づき、新規リードにトライアルを案内する。
        新規獲得が一時停止中の場合は何もせずスキップする。
        """
        if self._outreach_paused:
            return {"client_id": client_id, "action": "skipped", "reason": "outreach paused"}

        existing = await self.sales_repo.get_lead_by_client(client_id)
        if existing is None:
            await self.sales_repo.create_lead(client_id, source=source, notes=notes)
        await self.sales_repo.update_lead_stage(client_id, "trial")

        return {"client_id": client_id, "action": "trial_invited", "reason": ""}

    async def follow_up(self, client_id: str, notes: str = "") -> Dict[str, Any]:
        """既存クライアント(active)へのフォローアップ/アップセル。一時停止の影響は受けない"""
        await self.sales_repo.create_lead(client_id, source="follow_up", notes=notes)
        return {"client_id": client_id, "action": "follow_up_sent", "reason": ""}

    async def promote_to_active(self, client_id: str) -> Dict[str, Any]:
        """トライアル後、正式契約に至った場合に呼ぶ"""
        await self.sales_repo.update_lead_stage(client_id, "active")
        return {"client_id": client_id, "action": "promoted_to_active", "reason": ""}

    async def run_from_strategy_result(
        self, strategy_result: Dict[str, Any], target_client_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        SalesEngine.run_strategy_cycle() の戻り値を受けて、承認された場合のみ
        対象クライアント群にトライアル案内を実行する。却下/保留の場合は何もしない。
        """
        if strategy_result.get("stage") != "approved":
            return [{
                "action": "no_op",
                "reason": f"strategy stage was '{strategy_result.get('stage')}', not 'approved'",
            }]

        results = []
        for client_id in target_client_ids:
            result = await self.invite_trial(
                client_id,
                source=f"strategy_cycle:{strategy_result['cycle_id']}",
                notes=strategy_result["evaluation"]["reason"],
            )
            results.append(result)
        return results
