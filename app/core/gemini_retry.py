import asyncio
import logging
from typing import Any

from google.genai import errors as genai_errors

logger = logging.getLogger("gateway_x")


async def generate_content_with_retry(
    client: Any, max_attempts: int = 3, initial_delay: float = 2.0, **kwargs
):
    """
    google-genaiのgenerate_content呼び出しを、一時的なサーバーエラー(503 UNAVAILABLE等)
    に対して指数バックオフでリトライするラッパー。

    503は「モデルが一時的に混雑している」という意味で、数秒〜数十秒待てば解消することが
    多いため、その場でユーザーにエラーを返す前に自動リトライする価値がある。
    ServerError以外の例外(認証エラー等、リトライしても解決しないもの)はそのまま送出する。

    strategy_planner.py / concierge_service.py / semantic_safety.py の3箇所から
    共通で使う(重複実装を避けるため、このモジュールに切り出した)。
    """
    delay = initial_delay
    last_exc: Exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await client.aio.models.generate_content(**kwargs)
        except genai_errors.ServerError as e:
            last_exc = e
            if attempt == max_attempts:
                logger.warning(f"[Gemini retry] All {max_attempts} attempts failed: {e}")
                raise
            logger.info(
                f"[Gemini retry] Attempt {attempt}/{max_attempts} failed with ServerError "
                f"({e}), retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            delay *= 2

    raise last_exc
