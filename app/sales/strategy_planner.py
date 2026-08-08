import os
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from .sales import SalesRepository


class StrategyPlanner:
    """
    StrategyPlanner: google-genai(Gemini API)を使い、2つの人格(提案担当/批判担当)に
    自己討論させる。

    提案(Proposer) -> 批判(Critic) -> 修正(Proposer) を1ラウンドとし、最大 max_rounds 回
    繰り返す。Critic側が「これ以上の問題なし」と判断した場合はCONVERGEDを返し、そこで
    討論を打ち切る。max_rounds に達しても収束しない場合は、本来StrategyExecutorが行う
    保留判定を暫定的にここで代行する(Executor実装後はそちらに移す)。

    当初は「ChatGPTとClaude Code、異なるモデル同士の討論」という設計だったが、既にGemini
    APIキー(有料プラン)を保有しており、requirements.txtにも google-genai が既に含まれて
    いるため、新規にOpenAI/Anthropicのサインアップ・キー管理コストをかけずGemini単体での
    2人格自己討論に変更した。トレードオフとして、批判役が別モデルほど厳しくならない
    (同じモデルの癖を引きずる)リスクがあるため、プロンプトで人格をはっきり分けている。
    """

    def __init__(
        self,
        sales_repo: SalesRepository,
        api_key: Optional[str] = None,
        model: str = "gemini-3.6-flash",
        max_rounds: int = 3,
    ):
        self.sales_repo = sales_repo
        # google-genai は GEMINI_API_KEY / GOOGLE_API_KEY 環境変数を自動で拾うが、
        # 明示的に渡された場合はそちらを優先する
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model
        self.max_rounds = max_rounds

    # ---------- Gemini呼び出し(人格ごとにsystem_instructionを変える) ----------

    async def _call_gemini(self, prompt: str, system_instruction: str) -> str:
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system_instruction),
        )
        return response.text or ""

    async def _call_proposer(self, prompt: str) -> str:
        return await self._call_gemini(
            prompt,
            system_instruction=(
                "あなたはB2B調達エージェント向けサービス「Gateway X」の営業戦略立案者(推進派)です。"
                "実行可能で具体的な戦略案を前向きに提案してください。"
            ),
        )

    async def _call_critic(self, prompt: str) -> str:
        return await self._call_gemini(
            prompt,
            system_instruction=(
                "あなたはGateway Xの営業戦略をレビューする批判担当(懐疑派)です。"
                "提案担当とは独立した立場で、実行可能性・法的リスク・費用対効果の観点から"
                "厳しく問題点を指摘してください。提案担当に同調しないでください。"
            ),
        )

    # ---------- 討論ループ本体 ----------

    async def run_debate_cycle(self, topic: str, context: str = "") -> Dict[str, Any]:
        """
        topic: 議題(例: 「新規セグメントAへの営業を強化すべきか」)
        context: 制約情報(法的リスク・実装フェーズ・API可用性等、呼び出し側から渡す)
        """
        past_cycles = await self.sales_repo.get_recent_cycles(limit=5)
        past_summary = "\n".join(
            f"- {(c['proposal'] or '')[:100]}... => {c['executor_decision'] or '未判定'}"
            for c in past_cycles
        ) or "(過去事例なし)"

        proposal_prompt = (
            f"議題: {topic}\n"
            f"制約・背景情報: {context}\n"
            f"過去の戦略サイクルの結果:\n{past_summary}\n\n"
            "上記を踏まえ、具体的な営業戦略案を1つ提案してください。"
        )
        proposal = await self._call_proposer(proposal_prompt)
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
            critique = await self._call_critic(critique_prompt)

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
            revision = await self._call_proposer(revision_prompt)
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
