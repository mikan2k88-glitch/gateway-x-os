import hashlib
import hmac
import base64
import os
from typing import Any, Dict, Optional

import httpx


class LineService:
    """
    LINE Messaging APIのラッパー(httpx直叩き、line-bot-sdkは使わない)。

    - push_message: システム側からワーカーへタスク内容を通知する(能動的送信)
    - reply_message: Webhookで受け取ったイベントへの応答(replyTokenは1回限り有効)
    - verify_signature: WebhookリクエストがLINEから来た本物か検証する
      (X-Line-SignatureヘッダーをチャネルシークレットでHMAC-SHA256検証)

    line-bot-sdkを追加せずhttpxで直叩きしているのは、StrategyPlannerでOpenAI/Anthropic
    SDKを避けてhttpx直叩きにしたのと同じ方針(依存を増やさない)。
    """

    PUSH_URL = "https://api.line.me/v2/bot/message/push"
    REPLY_URL = "https://api.line.me/v2/bot/message/reply"

    def __init__(
        self,
        channel_access_token: Optional[str] = None,
        channel_secret: Optional[str] = None,
    ):
        self.channel_access_token = channel_access_token or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
        self.channel_secret = channel_secret or os.environ.get("LINE_CHANNEL_SECRET")

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """WebhookのX-Line-Signatureヘッダーを検証する"""
        if not self.channel_secret:
            return False
        hash_digest = hmac.new(
            self.channel_secret.encode("utf-8"), body, hashlib.sha256
        ).digest()
        expected_signature = base64.b64encode(hash_digest).decode("utf-8")
        return hmac.compare_digest(expected_signature, signature)

    async def push_message(self, line_user_id: str, text: str) -> Dict[str, Any]:
        """ワーカーへタスク通知等を能動的に送信する"""
        headers = {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "to": line_user_id,
            "messages": [{"type": "text", "text": text}],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.PUSH_URL, headers=headers, json=payload)
            return {"success": resp.status_code == 200, "status_code": resp.status_code}

    async def reply_message(self, reply_token: str, text: str) -> Dict[str, Any]:
        """Webhookイベントへの返信(replyTokenは発行から短時間・1回のみ有効)"""
        headers = {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.REPLY_URL, headers=headers, json=payload)
            return {"success": resp.status_code == 200, "status_code": resp.status_code}

    def build_task_notification(self, execution_id: str, intent: str, tier: str) -> str:
        """ワーカーに送るタスク通知文面を組み立てる"""
        return (
            f"【Gateway X タスク通知】\n"
            f"依頼内容: {intent}\n"
            f"tier: {tier}\n"
            f"管理番号: {execution_id}\n\n"
            f"作業が完了したら「完了 {execution_id}」と返信してください。\n"
            f"作業できない場合は「失敗 {execution_id}」と返信してください。"
        )
