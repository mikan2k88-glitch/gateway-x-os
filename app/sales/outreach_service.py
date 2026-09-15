import logging
from typing import Any, Dict, List, Optional

import httpx
from google import genai
from google.genai import types

from .sales import SalesRepository
from ..core.gemini_retry import generate_content_with_retry

logger = logging.getLogger("gateway_x.outreach_service")


class OutreachService:
    """
    StrategyExecutorが承認した戦略案を受けて、実際のリード獲得アクションを行う。

    - 新規リードへのトライアル案内(lead -> trial)
    - 既存クライアントへのフォローアップ/アップセル(active維持)
    - report_capacity() が pause_outreach=True を返した場合の新規獲得の一時停止

    2026-09-14以前は、実際の送信手段(メール/API等)が未実装で、leads/event的な
    記録として残すだけだった。「APIで受けてAPIで返す」という対称性の方針を受け、
    leadsにcallback_urlが登録されている場合のみ、実際にそのURLへPOSTするようにした。
    callback_url未登録のリード(架空の議題で発掘した想定顧客等、実在しない相手)は
    従来通り記録のみで、無理に送信しようとはしない。
    """

    _OUTREACH_SYSTEM_INSTRUCTION = (
        "あなたはGateway X-OSの営業担当です。渡された「Gateway Xの対応可能範囲」を踏まえ、"
        "トライアル案内の短い挨拶文(3〜4文程度)を書いてください。"
        "誇張や架空の実績には触れず、事実(渡された対応可能範囲)のみを根拠にしてください。\n\n"
        "出力は案内文の本文のみとし、前置きや後書きは含めないでください。"
    )

    def __init__(self, sales_repo: SalesRepository, api_key: Optional[str] = None, model: str = "gemini-3.8-flash"):
        self.sales_repo = sales_repo
        self._outreach_paused = False
        import os
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model

    def pause(self) -> None:
        """StrategyExecutorのcapacity判定でpause_outreach=Trueが返された場合に呼ぶ"""
        self._outreach_paused = True

    def resume(self) -> None:
        self._outreach_paused = False

    @property
    def is_paused(self) -> bool:
        return self._outreach_paused

    async def _send_outreach_message(self, callback_url: str, capability_context: str, notes: str) -> Dict[str, Any]:
        """
        callback_urlが登録されている場合に、実際にトライアル案内をPOSTする。
        送信失敗はここで吸収し(呼び出し元の戦略サイクル自体を失敗させない)、
        結果をログに残すのみとする。
        """
        prompt = f"【Gateway Xの対応可能範囲】\n{capability_context}\n\n【背景】\n{notes}"
        try:
            response = await generate_content_with_retry(
                self.client, model=self.model, contents=prompt,
                config=types.GenerateContentConfig(system_instruction=self._OUTREACH_SYSTEM_INSTRUCTION),
            )
            message_text = (response.text or "").strip() or "Gateway Xのトライアルをご案内します。"
        except Exception as e:
            logger.warning(f"[Outreach] 案内文生成に失敗、定型文で送信します: {e}")
            message_text = "Gateway Xのトライアルをご案内します。"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(callback_url, json={"type": "trial_invitation", "message": message_text})
                logger.info(f"[Outreach] {callback_url} へ送信完了 (HTTP {res.status_code})")
                return {"delivered": True, "http_status": res.status_code}
        except Exception as e:
            logger.warning(f"[Outreach] {callback_url} への送信に失敗: {e}")
            return {"delivered": False, "error": str(e)}

    async def invite_trial(
        self, client_id: str, source: str, notes: str = "", capability_context: str = "",
    ) -> Dict[str, Any]:
        """
        承認された戦略案に基づき、新規リードにトライアルを案内する。
        新規獲得が一時停止中の場合は何もせずスキップする。
        """
        if self._outreach_paused:
            return {"client_id": client_id, "action": "skipped", "reason": "outreach paused"}

        existing = await self.sales_repo.get_lead_by_client(client_id)
        if existing is None:
            await self.sales_repo.create_lead(client_id, source=source, notes=notes)
            existing = await self.sales_repo.get_lead_by_client(client_id)
        await self.sales_repo.update_lead_stage(client_id, "trial")

        callback_url = (existing or {}).get("callback_url")
        if not callback_url:
            return {"client_id": client_id, "action": "trial_invited", "reason": "", "delivery": "recorded_only"}

        delivery = await self._send_outreach_message(callback_url, capability_context, notes)
        return {"client_id": client_id, "action": "trial_invited", "reason": "", "delivery": delivery}

    async def follow_up(self, client_id: str, notes: str = "") -> Dict[str, Any]:
        """既存クライアント(active)へのフォローアップ/アップセル。一時停止の影響は受けない"""
        await self.sales_repo.create_lead(client_id, source="follow_up", notes=notes)
        return {"client_id": client_id, "action": "follow_up_sent", "reason": ""}

    async def promote_to_active(self, client_id: str) -> Dict[str, Any]:
        """トライアル後、正式契約に至った場合に呼ぶ"""
        await self.sales_repo.update_lead_stage(client_id, "active")
        return {"client_id": client_id, "action": "promoted_to_active", "reason": ""}

    async def run_from_strategy_result(
        self, strategy_result: Dict[str, Any], target_client_ids: List[str], capability_context: str = "",
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
                capability_context=capability_context,
            )
            results.append(result)
        return results
