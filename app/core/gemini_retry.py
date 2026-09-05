import asyncio
import logging
from typing import Any

from google.genai import errors as genai_errors

logger = logging.getLogger("gateway_x")


async def generate_content_with_retry(
    client: Any,
    max_attempts: int = 3,
    initial_delay: float = 2.0,
    fallback_models: tuple = ("gemini-3.7-flash", "gemini-3.6-flash"),
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

    注意: gemini-2.5-flashは公式の廃止予定日(2026-10-16)より前倒しで既に
    利用不可(404/500相当)を返すケースが実際に確認されたため、フォールバック候補
    から除外している(2026-08-18)。廃止予定が近いモデルはフォールバック先として
    選ばない方が安全。

    2026-08-20時点でgemini-3.7-flashが持続的に混雑しており、毎回3回リトライ後に
    フォールバックする無駄な待ち時間(1呼び出しあたり約90秒)が発生していたため、
    プライマリを実績のあるgemini-3.6-flashに変更し、3.7-flashはフォールバック側に
    回した(3.7の混雑が落ち着いたら再度プライマリに戻すことを検討)。

    2026-09-05: gemini-3.8-flashのリリースに伴い、プライマリをgemini-3.8-flashに、
    フォールバックをgemini-3.7-flash → gemini-3.6-flashの順に更新。3週間おきの
    新Flashリリースのたびに相対的に古いモデルから先に混雑・縮退する傾向が観測されて
    いるため、常に最新Flashをプライマリに、その前の2世代をフォールバックに置く方針とする。
    gemini-3.5-flash以前はフォールバック候補から外した(世代が離れすぎているため)。

    全モデルが失敗した場合は最後の例外を送出する。呼び出し元(main.py)が
    genai_errors.ServerError/ClientErrorをキャッチしてクライアントには503/429を返す設計。

    ClientError(429 RESOURCE_EXHAUSTED)はクォータ超過を意味し、同一モデルへの
    即時リトライでは解決しないため、リトライループには入らず即座にフォールバックへ進む。
    クォータはモデルごとに別枠で管理されているため、フォールバック先は別モデルとして
    独立したクォータを持つ(2026-08-18に無料枠のgemini-3.7-flashが1日20リクエストの
    上限に達したことで発覚。根本解決には有料プランへの切り替えが必要)。

    ServerError以外の例外(認証エラー等、リトライしても解決しないもの)はそのまま送出する。

    strategy_planner.py / concierge_service.py / semantic_safety.py の3箇所から
    共通で使う(重複実装を避けるため、このモジュールに切り出した)。
    """
    delay = initial_delay
    last_exc: Exception = None
    primary_model = kwargs.get("model")

    for attempt in range(1, max_attempts + 1):
        try:
            return await client.aio.models.generate_content(**kwargs)
        except genai_errors.ServerError as e:
            last_exc = e
            if attempt == max_attempts:
                logger.warning(
                    f"[Gemini retry] All {max_attempts} attempts on {primary_model} failed: {e}"
                )
                break
            logger.info(
                f"[Gemini retry] Attempt {attempt}/{max_attempts} failed with ServerError "
                f"({e}), retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            delay *= 2
        except genai_errors.ClientError as e:
            # 429 RESOURCE_EXHAUSTED(クォータ超過)は、同じモデルへの即時リトライでは
            # 解決しない(日次クォータのリセット待ちになるため)。ここで粘らず、
            # 即座にフォールバック候補(別モデル=別クォータ)へ切り替える。
            last_exc = e
            logger.warning(
                f"[Gemini retry] {primary_model} returned ClientError (likely quota exceeded): "
                f"{e}. Skipping same-model retries, moving to fallback."
            )
            break

    # プライマリモデルのリトライを使い切った場合、フォールバック候補を上から順に1回ずつ試す
    for fallback_model in fallback_models:
        if fallback_model == primary_model:
            continue
        logger.warning(f"[Gemini retry] Falling back to {fallback_model} after previous failure")
        fallback_kwargs = {**kwargs, "model": fallback_model}
        try:
            return await client.aio.models.generate_content(**fallback_kwargs)
        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            logger.warning(f"[Gemini retry] Fallback model {fallback_model} also failed: {e}")
            last_exc = e

    raise last_exc
