import asyncio
import os
from typing import Any, Dict, Optional

import stripe


class StripeService:
    """
    Stripe 2-Phase Settlement(Auth → Capture)のラッパー。

    Phase A (authorize_payment): Vetting通過直後に与信枠を確保する(PaymentIntent作成
    + capture_method='manual'で即キャプチャしない)。
    Phase B (capture_payment): 現場でのプレ検収通過後に売上を確定する。
    cancel_payment: プレ検収不合格やタイムアウト時に与信を解放する(例外系リカバリー)。

    stripe SDKは同期的なので、既存のsales.py/repository.pyと同じくasyncio.to_thread
    でラップして非同期化している。

    注意: 実際の決済にはクライアント側の支払い方法(payment_method)が必要。
    B2Bクライアント企業が事前にカード情報を登録している前提とし、
    payment_method_id が渡されない場合はStripeのテスト用トークン(pm_card_visa)を
    フォールバックとして使う(サンドボックス/テスト運用向けの暫定措置。本番では
    各クライアントの実際の支払い方法IDを渡すこと)。
    """

    def __init__(self, api_key: Optional[str] = None, webhook_secret: Optional[str] = None):
        stripe.api_key = api_key or os.environ.get("STRIPE_SECRET_KEY")
        self.webhook_secret = webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET")

    async def authorize_payment(
        self, quote: Dict[str, Any], payment_method_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Phase A: 与信仮押さえ。amountはUSDのドル単位からセント単位に変換する"""
        def _execute():
            amount_cents = int(round(quote["price_usd"] * 100))
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=quote.get("currency", "usd").lower(),
                payment_method=payment_method_id or "pm_card_visa",
                capture_method="manual",
                confirm=True,
                metadata={"quote_id": quote["quote_id"], "intent": quote.get("intent", "")},
                automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            )
            return intent

        try:
            intent = await asyncio.to_thread(_execute)
            return {
                "success": intent.status in ("requires_capture", "succeeded"),
                "payment_intent_id": intent.id,
                "status": intent.status,
                "reason": "",
            }
        except stripe.error.StripeError as e:
            return {
                "success": False,
                "payment_intent_id": None,
                "status": "failed",
                "reason": str(e.user_message or e),
            }

    async def capture_payment(
        self, payment_intent_id: str, amount_to_capture_cents: Optional[int] = None
    ) -> Dict[str, Any]:
        """Phase B: 売上確定。プレ検収通過後に呼ぶ"""
        def _execute():
            kwargs = {}
            if amount_to_capture_cents is not None:
                kwargs["amount_to_capture"] = amount_to_capture_cents
            return stripe.PaymentIntent.capture(payment_intent_id, **kwargs)

        try:
            intent = await asyncio.to_thread(_execute)
            return {
                "success": intent.status == "succeeded",
                "payment_intent_id": intent.id,
                "status": intent.status,
                "amount_captured_cents": intent.amount_received,
                "reason": "",
            }
        except stripe.error.StripeError as e:
            return {
                "success": False,
                "payment_intent_id": payment_intent_id,
                "status": "failed",
                "amount_captured_cents": 0,
                "reason": str(e.user_message or e),
            }

    async def cancel_payment(self, payment_intent_id: str, reason: str = "") -> Dict[str, Any]:
        """例外系リカバリー: プレ検収不合格・タイムアウト等で与信を解放する"""
        def _execute():
            return stripe.PaymentIntent.cancel(
                payment_intent_id, cancellation_reason="abandoned"
            )

        try:
            intent = await asyncio.to_thread(_execute)
            return {
                "success": intent.status == "canceled",
                "payment_intent_id": intent.id,
                "status": intent.status,
                "reason": reason,
            }
        except stripe.error.StripeError as e:
            return {
                "success": False,
                "payment_intent_id": payment_intent_id,
                "status": "failed",
                "reason": str(e.user_message or e),
            }

    def verify_webhook(self, payload: bytes, sig_header: str):
        """
        Stripe Webhookの署名検証(なりすまし防止)。成功時はイベントオブジェクトを返す。
        失敗時は stripe.error.SignatureVerificationError を送出する(呼び出し側でcatchする)。
        """
        return stripe.Webhook.construct_event(payload, sig_header, self.webhook_secret)

    async def submit_dispute_evidence(self, dispute_id: str, evidence_text: str) -> Dict[str, Any]:
        """
        チャージバック(dispute)発生時に、蓄積済みの監査ログ(Auth ID/Capture ID/
        LINE完了報告のタイムスタンプ等)を根拠として自動的に証拠を提出する。

        「サービス提供後に未提供だと異議申立てされる」濫用への対策
        (悪意あるクライアント対策5系統の5番)。evidence_textは呼び出し側
        (master.py)が監査ログから組み立てて渡す。
        """
        def _execute():
            return stripe.Dispute.modify(
                dispute_id,
                evidence={"uncategorized_text": evidence_text},
                submit=True,
            )

        try:
            dispute = await asyncio.to_thread(_execute)
            return {"success": True, "dispute_id": dispute.id, "status": dispute.status}
        except stripe.error.StripeError as e:
            return {"success": False, "dispute_id": dispute_id, "status": "failed", "reason": str(e.user_message or e)}
