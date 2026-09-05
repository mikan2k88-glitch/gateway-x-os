import json
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from .sales import SalesRepository
from app.core.gemini_retry import generate_content_with_retry


class StrategyPlanner:
    """
    StrategyPlanner: google-genai(Gemini API)を使い、2つの人格(提案担当/批判担当)に
    自己討論させる。

    提案(Proposer) -> 批判(Critic) -> 修正(Proposer) を1ラウンドとし、最大 max_rounds 回
    繰り返す。Critic側が「これ以上の問題なし」と判断した場合はCONVERGEDを返し、そこで
    討論を打ち切る。max_rounds に達しても収束しない場合は、本来StrategyExecutorが行う
    保留判定を暫定的にここで代行する(Executor実装後はそちらに移す)。

    Gemini APIが有料プランで安価に使えることが判明したため、Groq/OpenRouterへの
    移行は行わず、Geminiを継続利用する。無料枠のクォータ制約に対しては、
    gemini_retry.pyの多段フォールバックで耐性を持たせている。
    """

    def __init__(
        self,
        sales_repo: SalesRepository,
        api_key: Optional[str] = None,
        model: str = "gemini-3.8-flash",
        max_rounds: int = 3,
    ):
        self.sales_repo = sales_repo
        import os
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model
        self.max_rounds = max_rounds

    # ---------- Gemini呼び出し(人格ごとにsystem_instructionを変える) ----------

    async def _call_gemini(self, prompt: str, system_instruction: str) -> str:
        response = await generate_content_with_retry(
            self.client,
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
                "厳しく問題点を指摘してください。提案担当に同調しないでください。\n\n"
                "【重要】ただし、この営業エンジンの目的は能動的に戦略を立て、実行することです。"
                "完璧な計画ができるまで承認しないと、いつまでも何も実行されなくなってしまいます。"
                "以下の場合は、多少の見積もりの粗さや未検証の前提が残っていても承認(CONVERGED)して"
                "ください:\n"
                "- 提案が少数（5〜10社程度）の限定的なパイロット/PoCとして設計されている\n"
                "- 明確な撤退条件(キルスイッチ)が示されている\n"
                "- 法的リスク・重大な安全性の問題・回復不能な財務的損失のリスクが無い\n"
                "この場合、財務モデルの精緻さや全社展開時の完成度を求めるのは過剰な要求です。"
                "小さく試して学ぶことがパイロットの目的なので、「試してみないとわからない」"
                "程度の不確実性は許容してください。\n\n"
                "逆に、全社一斉展開や不可逆な意思決定(大規模な契約変更、後戻りできない"
                "システム変更等)を伴う提案については、これまで通り厳しく審査してください。\n\n"
                "【出力形式に関する厳密なルール】\n"
                "承認する場合は、回答の1行目に、他の文字を一切付け加えず「CONVERGED」という"
                "単語だけを書いてください(例:「CONVERGEDとは言えません」のように、他の語を"
                "続けて書くことは絶対に禁止です。1行目は「CONVERGED」の7文字のみにしてください)。"
                "2行目以降に理由を書くのは構いません。"
                "承認しない場合は、1行目に「CONVERGED」という単語を絶対に使わず、"
                "問題点の指摘から書き始めてください。"
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
                "回答の1行目に他の文字を一切付け加えず「CONVERGED」という単語だけを"
                "書いてください。\n\n"
                f"戦略案:\n{revision}"
            )
            critique = await self._call_critic(critique_prompt)

            # 収束判定: 1行目が厳密に「CONVERGED」と完全一致する場合のみ承認とみなす。
            # 「CONVERGEDとは言えません」のように、批判担当が否定文の中で単語として言及した
            # だけのケースを誤って承認扱いしてしまう不具合が実際に発生したため、
            # startswith()による部分一致ではなく、1行目全体の完全一致に変更した(重要な修正)。
            first_line = critique.strip().split("\n", 1)[0].strip()
            if first_line == "CONVERGED":
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

    # ---------- 機能要望検出 ----------

    _FEATURE_DETECTION_INSTRUCTION = (
        "あなたはGateway X-OSの開発ロードマップ担当です。以下の営業戦略案を読み、"
        "これを実行するために、今のGateway Xにまだ無い新しいシステム機能が必要かどうかを"
        "判定してください。既存の一般的な営業活動(トライアル案内、フォローアップ等)だけで"
        "実行できる場合は不要と判定してください。"
        "必ず以下のJSON形式のみで出力してください(説明文やコードブロック記号は付けない):\n"
        '{"feature_needed": true/false, "title": "機能名を一言で(不要ならnull)", '
        '"description": "その機能が何をすべきかの説明(不要ならnull)"}'
    )

    async def detect_feature_request(self, proposal: str) -> Dict[str, Any]:
        """
        討論で承認された戦略案が、既存のGateway Xの機能だけで実行可能かを判定し、
        新機能が必要な場合はタイトルと説明を返す。営業活動の副産物として
        開発ロードマップのヒントを蓄積するために使う(SalesEngine側から呼ばれる)。
        """
        response = await generate_content_with_retry(
            self.client,
            model=self.model,
            contents=f"戦略案:\n{proposal}",
            config=types.GenerateContentConfig(
                system_instruction=self._FEATURE_DETECTION_INSTRUCTION,
                response_mime_type="application/json",
            ),
        )
        raw_text = (response.text or "").strip()
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            # 判定不能な場合は「不要」扱いにする(機能要望は誤検知より見逃しの方が実害が小さい)
            parsed = {"feature_needed": False, "title": None, "description": None}

        return {
            "feature_needed": bool(parsed.get("feature_needed", False)),
            "title": parsed.get("title"),
            "description": parsed.get("description"),
        }
