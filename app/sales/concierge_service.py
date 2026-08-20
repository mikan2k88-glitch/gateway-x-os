import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from google import genai
from google.genai import types

from .sales import SalesRepository
from app.core.gemini_retry import generate_content_with_retry


class ConciergeService:
    """
    初回・曖昧な依頼を持つAIクライアントとの対話窓口。

    責務:
    1. AuthGatewayが'concierge'と判定したリクエストのリード登録(初回接触の記録)
    2. 要件明確化ダイアログ(自由記述の依頼から intent/tier/estimated_cost_jpy を抽出し、
       情報が足りない場合は聞き返す質問を生成する)
    3. VettingEngine却下時の通知受け皿(却下理由をクライアントへ返す文面を組み立てる)
    4. Stripe決済失敗時の通知受け皿(同上)

    要件明確化はGemini(google-genai)に、JSON形式での構造化出力を指示して行う。
    Gemini APIが有料プランで安価に使えることが判明したため、Groq/OpenRouterへの
    移行は行わずGeminiを継続利用する(2026-08-18)。
    会話履歴はsales_repoのconcierge_messagesテーブルに永続化する。呼び出し側が
    historyを明示的に渡した場合はそちらを優先する(後方互換のステートレス利用も可能)が、
    渡さない場合はDBに保存された過去のやり取りを自動的に読み込んで文脈を引き継ぐ。
    """

    def __init__(
        self,
        sales_repo: SalesRepository,
        api_key: Optional[str] = None,
        model: str = "gemini-3.7-flash",
    ):
        self.sales_repo = sales_repo
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model

    async def handle_first_contact(
        self, client_id: str, intent: str, tier: str, source: str = "auto_routed"
    ) -> Dict[str, Any]:
        existing = await self.sales_repo.get_account(client_id)
        if existing is None:
            await self.sales_repo.create_lead(
                client_id, source=source, notes=f"初回問い合わせ: intent={intent}, tier={tier}"
            )
        message = (
            "初めてのご依頼、ありがとうございます。今回はコンシェルジュ経由でご案内しています。"
            "内容を確認のうえ通常フローで見積を発行します。"
        )
        return {"client_id": client_id, "handled": True, "message": message}

    _CLARIFY_SYSTEM_INSTRUCTION = (
        "あなたはB2B調達エージェント向けサービス「Gateway X」のコンシェルジュです。"
        "AIクライアントからの自由記述の依頼を読み、発注に必要な情報"
        "(intent: 何を依頼したいか, tier: economy/express/tacticalのいずれか, "
        "estimated_cost_jpy: 想定コスト(円、数値)) を抽出してください。"
        "情報が不足していて確定できない場合は、聞き返す質問を1つ作ってください。"
        "必ず以下のJSON形式のみで、説明文やコードブロック記号を付けずに出力してください:\n"
        '{"needs_clarification": true/false, "question": "聞き返す質問(不要ならnull)", '
        '"intent": "抽出できたintent(不明ならnull)", '
        '"tier": "economy/express/tactical のいずれか(不明ならnull)", '
        '"estimated_cost_jpy": 数値(不明ならnull)}'
    )

    async def clarify_intent(
        self, client_id: str, message: str, history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        history_explicit = history is not None
        if not history_explicit:
            past = await self.sales_repo.get_concierge_history(client_id)
            history = [{"role": h["role"], "text": h["message"]} for h in past]

        conversation = "\n".join(f"{h['role']}: {h['text']}" for h in history)
        prompt = (
            (f"これまでのやり取り:\n{conversation}\n\n" if conversation else "")
            + f"クライアントからの最新メッセージ:\n{message}"
        )

        response = await generate_content_with_retry(
            self.client,
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=self._CLARIFY_SYSTEM_INSTRUCTION,
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
                    "needs_clarification": True,
                    "question": "恐れ入りますが、ご依頼内容をもう少し具体的に教えていただけますか？",
                    "intent": None,
                    "tier": None,
                    "estimated_cost_jpy": None,
                }

        result = {
            "client_id": client_id,
            "needs_clarification": bool(parsed.get("needs_clarification", True)),
            "question": parsed.get("question"),
            "intent": parsed.get("intent"),
            "tier": parsed.get("tier"),
            "estimated_cost_jpy": parsed.get("estimated_cost_jpy"),
        }

        if not history_explicit:
            await self.sales_repo.append_concierge_message(client_id, "user", message)
            concierge_reply = result["question"] or "情報を確認しました。ありがとうございます。"
            await self.sales_repo.append_concierge_message(client_id, "concierge", concierge_reply)

        return result

    async def notify_vetting_rejection(self, client_id: str, intent: str, reason: str) -> Dict[str, Any]:
        message = (
            f"申し訳ございませんが、今回のご依頼(intent: {intent})はお受けできませんでした。"
            f"理由: {reason}"
        )
        return {"client_id": client_id, "handled": True, "message": message}

    async def notify_payment_failure(self, client_id: str, quote_id: str, reason: str) -> Dict[str, Any]:
        message = (
            f"お見積り(quote_id: {quote_id})について決済処理に失敗しました。理由: {reason}。"
            "お手数ですが決済方法をご確認のうえ再度お試しください。"
        )
        return {"client_id": client_id, "handled": True, "message": message}


class ConciergeMessageRequest(BaseModel):
    client_id: str
    message: str
    history: Optional[List[Dict[str, str]]] = None


class ConciergeMessageResponse(BaseModel):
    client_id: str
    needs_clarification: bool
    question: Optional[str] = None
    intent: Optional[str] = None
    tier: Optional[str] = None
    estimated_cost_jpy: Optional[float] = None


def create_concierge_router(concierge_service: ConciergeService) -> APIRouter:
    router = APIRouter(prefix="/concierge", tags=["concierge"])

    @router.post("/message", response_model=ConciergeMessageResponse)
    async def send_message(payload: ConciergeMessageRequest) -> ConciergeMessageResponse:
        result = await concierge_service.clarify_intent(
            payload.client_id, payload.message, payload.history
        )
        return ConciergeMessageResponse(**result)

    return router
