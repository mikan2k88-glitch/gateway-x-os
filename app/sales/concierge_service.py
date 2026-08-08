from typing import Any, Dict, Optional

from .sales import SalesRepository


class ConciergeService:
    """
    初回・曖昧な依頼を持つAIクライアントとの対話窓口。

    責務:
    1. AuthGatewayが'concierge'と判定したリクエストのリード登録(初回接触の記録)
    2. VettingEngine却下時の通知受け皿(却下理由をクライアントへ返す文面を組み立てる)
    3. Stripe決済失敗時の通知受け皿(同上)

    実際の要件明確化ダイアログ(質問を重ねて意図を明確にする部分)はまだ実装していない。
    現時点では「初回接触をリードとして記録し、状況に応じた案内文を返す」という
    最小限の受け皿に留めている。
    """

    def __init__(self, sales_repo: SalesRepository):
        self.sales_repo = sales_repo

    async def handle_first_contact(
        self, client_id: str, intent: str, tier: str, source: str = "auto_routed"
    ) -> Dict[str, Any]:
        """
        AuthGatewayでroute == 'concierge'と判定された際に呼ぶ。
        既存リードが無ければ新規作成し、案内メッセージを返す。
        現時点ではMasterOrchestratorへのフローはブロックしない
        (ConciergeServiceは並行して初回接触を記録するだけ)。
        """
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

    async def notify_vetting_rejection(self, client_id: str, intent: str, reason: str) -> Dict[str, Any]:
        """VettingEngine却下時の通知受け皿。却下理由をクライアントへ返す文面を組み立てる"""
        message = (
            f"申し訳ございませんが、今回のご依頼(intent: {intent})はお受けできませんでした。"
            f"理由: {reason}"
        )
        return {"client_id": client_id, "handled": True, "message": message}

    async def notify_payment_failure(self, client_id: str, quote_id: str, reason: str) -> Dict[str, Any]:
        """Stripe決済失敗(リトライ後)の通知受け皿"""
        message = (
            f"お見積り(quote_id: {quote_id})について決済処理に失敗しました。理由: {reason}。"
            "お手数ですが決済方法をご確認のうえ再度お試しください。"
        )
        return {"client_id": client_id, "handled": True, "message": message}
