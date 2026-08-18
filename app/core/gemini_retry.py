import asyncio
import logging
from typing import Any

from google.genai import errors as genai_errors

logger = logging.getLogger("gateway_x")


async def generate_content_with_retry(
    client: Any,
    max_attempts: int = 3,
    initial_delay: float = 2.0,
    fallback_models: tuple = ("gemini-3.6-flash", "gemini-2.5-flash"),
    **kwargs,
):
    """
    google-genaiのgenerate_content呼び出しを、一時的なサーバーエラー(503 UNAVAILABLE等)
    に対して指数バックオフでリトライするラッパー。

    google-genai自体も内部でtenacityを使ってリトライしているが、それでも失敗して
    ServerErrorが上がってくることがある。さらに、プライマリモデルだけでなく
    フォールバック先のモデルまで同時に混雑することもある(2026-08-18に実際に観測: 
    gemini-3.7-flashだけでなくgemini-3.6-flashも503になるケースがあった。これは
    単一モデルの問題ではなくGemini API全体が広く不安定になっている時間帯だった)。
    そのため、fallback_modelsは単一モデルではなくタプル(複数候補)を受け取り、
    プライマリのリトライを使い切った後、上から順に1回ずつ試す。

    全モデルが失敗した場合は最後の例外を送出する。呼び出し元(main.py)が
    genai_errors.ServerErrorをキャッチしてクライアントには503を返す設計になっている。

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

    # プライマリモデルのリトライを使い切った場合、フォールバック候補を上から順に1回ずつ試す
    for fallback_model in fallback_models:
        if fallback_model == kwargs.get("model"):
            continue
        logger.warning(f"[Gemini retry] Falling back to {fallback_model} after previous failure")
        fallback_kwargs = {**kwargs, "model": fallback_model}
        try:
            return await client.aio.models.generate_content(**fallback_kwargs)
        except genai_errors.ServerError as e:
            logger.warning(f"[Gemini retry] Fallback model {fallback_model} also failed: {e}")
            last_exc = e

    raise last_exc
