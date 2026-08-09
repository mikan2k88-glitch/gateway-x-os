from typing import Any, Dict


class PhysicalExecutionRouter:
    """
    層⑤ 現場物理実行層への動的ルーティング。

    Gemini側で検証済みだったtierごとの割り当てロジックをそのまま踏襲している。
    実際のディスパッチ先(タイミー連携API等)との接続はまだ実装しておらず、
    現時点ではアサイン先の決定とプレ検収ステータスの返却のみを行う
    (実ディスパッチ実装時にここへ実際のAPI呼び出しを追加する)。
    """

    def route(self, tier: str, intent: str) -> Dict[str, Any]:
        if tier == "express":
            target_org = "既存インフラ連携(タイミー等 SLA制御便)"
            agent_role = "自社AI「現場監督」"
        elif tier == "tactical":
            # tacticalティアは将来目標であり現時点では未実装。Vetting層のcheck_tier_availability
            # で既に弾かれている想定だが、念のためここでも安全側に倒す。
            target_org = "Gateway X Tactical Force(未実装)"
            agent_role = "未実装"
        else:
            target_org = "Economy Service(24時間猶予便)"
            agent_role = "標準ガイドラインナビ"

        return {
            "assigned_to": target_org,
            "agent_role": agent_role,
            "field_status": "PRE_INSPECTED_PASSED" if tier != "tactical" else "UNAVAILABLE",
            "evidence_photos": [],
        }
