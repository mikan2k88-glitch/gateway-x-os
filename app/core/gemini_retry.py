import asyncio
import logging
from typing import Any

from google.genai import errors as genai_errors

logger = logging.getLogger("gateway_x")


async def generate_content_with_retry(
    client: Any,
    max_attempts: int = 3,
    initial_delay: float = 2.0,
    fallback_model: str = "gemini-3.6-flash",
    **kwargs,
):
    """
    google-genaiのgenerate_content呼び出しを、一時的なサーバーエラー(503 UNAVAILABLE等)
    に対して指数バックオフでリトライするラッパー。

    google-genai自体も内部でtenacityを使ってリトライしているが、それでも失敗して
    ServerErrorが上がってくることがある(新モデルのリリース直後は特に高負荷になりやすい)。
    そのため、こちらのリトライを使い切っても失敗した場合は、最後の手段として
    fallback_model(デフォルトは実績のある gemini-3.6-flash)で1回だけ試す。
    フォールバックも失敗したら、その例外を送出する。

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
                logger.warning(
                    f"[Gemini retry] All {max_attempts} attempts on "
                    f"{kwargs.get('model')} failed: {e}"
                )
                break
            logger.info(
                f"[Gemini retry] Attempt {attempt}/{max_attempts} failed with ServerError "
                f"({e}), retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            delay *= 2

    # プライマリモデルのリトライを使い切った場合、フォールバックモデルで最後に1回だけ試す
    if fallback_model and fallback_model != kwargs.get("model"):
        logger.warning(f"[Gemini retry] Falling back to {fallback_model} after primary model failure")
        fallback_kwargs = {**kwargs, "model": fallback_model}
        try:
            return await client.aio.models.generate_content(**fallback_kwargs)
        except genai_errors.ServerError as e:
            logger.warning(f"[Gemini retry] Fallback model {fallback_model} also failed: {e}")
            last_exc = e

    raise last_exc
