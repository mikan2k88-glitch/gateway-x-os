import os
from typing import Dict, Any, Optional

import httpx

from sales import SalesRepository


class StrategyPlanner:
    """
    StrategyPlanner: httpxでChatGPT(批判役)とClaude(提案役)を直接叩いて討論させる。

    提案(Claude) -> 批判(ChatGPT) -> 修正(Claude) を1ラウンドとし、最大 max_rounds 回繰り返す。
    ChatGPT側が「これ以上の問題なし」と判断した場合はCONVERGEDを返し、そこで討論を打ち切る。
    max_rounds に達しても収束しない場合は、本来StrategyExecutorが行う保留判定を暫定的にここで
    代行する(Executor実装後はそちらに移す)。

    SDK(openai/anthropic)を使わずhttpxで直叩きしているのは、requirements.txtに両SDKが
    含まれておらず、依存を増やさない方針にしたため。
    """

    OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
    ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        sales_repo: SalesRepository,
        openai_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        max_rounds: int = 3,
        request_timeout: float = 60.0,
    ):
        self.sales_repo = sales_repo
        self.openai_api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        self.anthropic_api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_rounds = max_rounds
        self.request_timeout = request_timeout

    # ---------- 各APIの直叩き ----------

    async def _call_claude(self, prompt: str) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
        headers = {
            "x-api-key": self.anthropic_api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            resp = await client.post(self.ANTHROPIC_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )

    async def _call_chatgpt(self, prompt: str) -> str:
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY が設定されていません")
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            resp = await client.post(self.OPENAI_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    # ---------- 討論ループ本体 ----------

    async def run_debate_cycle(self, topic: str, context: str = "") -> Dict[str, Any]:
        """
        topic: 議題(例: 「新規セグロメントAへの営業を強化すべきか」)
        context: 制約情報(法的リスク・実装フェーズ・API可用性等、呼び出し側から渡す)
        """
        past_cycles = await self.sales_repo.get_recent_cycles(limit=5)
        past_summary = "\n".join(
            f"- {(c['proposal'] or '')[:100]}... => {c['executor_decision'] or '未判定'}"
            for c in past_cycles
        ) or "(過去事例なし)"

        proposal_prompt = (
            "あなたはB2B調達エージェント向けサービス「Gateway X」の営業戦略立案者です。\n"
            f"議題: {topic}\n"
            f"制約・背景情報: {context}\n"
            f"過去の戦略サイクルの結果:\n{past_summary}\n\n"
            "上記を踏まえ、具体的な営業戦略案を1つ提案してください。"
        )
        proposal = await self._call_claude(proposal_prompt)
        cycle_id = await self.sales_repo.start_strategy_cycle(proposal)

        converged = False
        critique = ""
        revision = proposal
        round_count = 0

        for round_count in range(1, self.max_rounds + 1):
            critique_prompt = (
                "以下はB2B調達エージェント向けサービスの営業戦略案です。批判的にレビューし、"
                "問題点を指摘してください。大きな問題がなく実行可能と判断できる場合のみ、"
                "回答の最初の行に厳密に「CONVERGED」とだけ書いてください。\n\n"
                f"戦略案:\n{revision}"
            )
            critique = await self._call_chatgpt(critique_prompt)

            if critique.strip().startswith("CONVERGED"):
                converged = True
                await self.sales_repo.update_debate_round(cycle_id, round_count, critique, revision)
                break

            revision_prompt = (
                "あなたが提案した営業戦略案に対して、以下の批判が来ました。\n\n"
                f"批判:\n{critique}\n\n"
                f"元の案:\n{revision}\n\n"
                "批判を踏まえて修正した戦略案を提示してください。"
            )
            revision = await self._call_claude(revision_prompt)
            await self.sales_repo.update_debate_round(cycle_id, round_count, critique, revision)

        if not converged:
            # 本来はStrategyExecutorが行う保留判定。Executor未実装のため暫定的にここで代行
            await self.sales_repo.resolve_cycle(
                cycle_id, status="pending", decision="", reason="3ラウンドで未収束"
            )

        return {
            "cycle_id": cycle_id,
            "converged": converged,
            "round_count": round_count,
            "final_proposal": revision,
            "last_critique": critique,
        }
