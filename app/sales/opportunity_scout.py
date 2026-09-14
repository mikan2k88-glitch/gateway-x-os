import json
import os
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from .gemini_retry import generate_content_with_retry


class OpportunityScout:
    """
    営業エンジン(StrategyPlanner)が討論する「議題」を、Gateway X自身が発掘する。

    背景: これまでは外部クライアント(Company X等)からの発注や、人間が手動で指定した
    topic/contextに依存していた。しかしCompany Xは頻繁に改修される不安定な存在であり、
    Gateway Xが自律的に案件を発掘できないと、営業エンジンが「何もすることがない」
    状態に陥ってしまう(2026-09-11の議論より)。

    ConciergeService.get_capability_briefing()由来のcapability_context(Gateway Xが
    実際に対応可能な業務範囲)を必ず踏まえて発想させることで、Company Xの案件発掘で
    起きたような「実行不可能な案件を思いついてしまう」問題を最初から回避する。

    現時点ではGemini自身の知識による発想(ブレインストーミング)であり、実際のWeb検索や
    実在企業のリサーチは行わない(そこまで求めるなら別途Web検索ツールの統合が必要)。
    あくまで「営業エンジンが自走できる」ことを優先した軽量版。
    """

    _SYSTEM_INSTRUCTION = (
        "あなたはGateway X-OSの新規事業開発担当です。Gateway Xが実際に対応できる"
        "業務範囲(渡される情報を厳守すること)の中で、今すぐ営業提案できそうな、"
        "具体的で現実的なB2B案件のアイデアを1つ考えてください。\n\n"
        "Gateway Xには2つの業務チャネルがあります:\n"
        "- 物理チャネル: 東京23区内で、スマートフォン1台のワーカーが即日対応できる現地作業\n"
        "- デジタルチャネル: Gateway X自身(Gemini)がその場で完結できる、翻訳・要約・"
        "リサーチ・文章作成等のデジタル業務(ソフトウェア開発等、実行環境を要するものは含めない)\n\n"
        "重要な制約:\n"
        "- 必ずどちらかの「Gateway Xが実際に対応可能な業務範囲」の中に収まる内容にすること\n"
        "- 実在しそうな業種・業態を想定すること(架空の技術トレンドに寄せない)\n\n"
        "必ず以下のJSON形式のみで出力してください(説明文やコードブロック記号は付けない):\n"
        '{"topic": "議題を1文で(例: 飲食店向け行列状況の定期モニタリングサービスの提案)", '
        '"context": "背景・想定ターゲット業種・想定頻度などを2〜3文で", '
        '"channel": "physical または digital"}'
    )

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-3.8-flash"):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model

    async def scout(self, capability_context: str, digital_capability_context: str = "") -> Dict[str, str]:
        prompt = (
            f"【物理チャネルで対応可能な業務範囲】\n{capability_context}\n\n"
            f"【デジタルチャネルで対応可能な業務範囲】\n{digital_capability_context or '(情報なし)'}"
        )

        response = await generate_content_with_retry(
            self.client,
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=self._SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
            ),
        )
        raw_text = (response.text or "").strip()

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = raw_text.strip("`\n ")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                parsed = {
                    "topic": "都内飲食店向け行列状況モニタリングサービスの提案",
                    "context": "案件発掘の応答解析に失敗したため、既定の安全な議題にフォールバック。",
                    "channel": "physical",
                }

        return {
            "topic": parsed.get("topic") or "都内飲食店向け行列状況モニタリングサービスの提案",
            "context": parsed.get("context", ""),
            "channel": parsed.get("channel") if parsed.get("channel") in ("physical", "digital") else "physical",
        }
