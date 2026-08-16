from typing import Any, Dict, Optional

from .sales import SalesRepository


class QuoteBuilder:
    """
    PricingEngineの薄いラッパー。PricingEngine自体の所有権(価格計算ロジック)は
    コア側(app/core/pricing.py)に残したまま、トライアル割引のような「営業判断」だけを
    ここで上乗せする。

    現在のトライアル割引ルール:
    - AuthGatewayの accounts テーブルに記録が無い(＝初回発注)クライアントには
      15%割引を適用する。leadsテーブルの状態(stage)は問わない
      (leadsは初回接触の記録用で、実際の発注実績はaccountsテーブル側にあるため)
    - 2回目以降(accountsに記録がある)は通常価格

    割引率が固定値なのは、現時点でA/Bテストや割引効果測定の仕組みが無いため。
    将来的に「トライアル割引が実際にconversion(trial->active)を上げているか」を
    strategy_cyclesの実行結果と突き合わせて検証できるようになったら、
    割引率自体をStrategyExecutorの判断対象にする拡張が考えられる。
    """

    TRIAL_DISCOUNT_RATE = 0.15

    def __init__(self, pricing_engine, sales_repo: SalesRepository):
        self.pricing_engine = pricing_engine
        self.sales_repo = sales_repo

    async def build_quote(self, client_id: str, estimated_cost_jpy: float, tier: str) -> Dict[str, Any]:
        base_quote = self.pricing_engine.calculate_quote(
            estimated_cost_jpy=estimated_cost_jpy, tier=tier
        )

        existing_account = await self.sales_repo.get_account(client_id)
        is_first_time = existing_account is None

        if is_first_time:
            discounted_price = round(base_quote["price_usd"] * (1 - self.TRIAL_DISCOUNT_RATE), 2)
            base_quote = {
                **base_quote,
                "price_usd": discounted_price,
                "original_price_usd": base_quote["price_usd"],
                "trial_discount_applied": True,
                "trial_discount_rate": self.TRIAL_DISCOUNT_RATE,
            }
        else:
            base_quote = {
                **base_quote,
                "trial_discount_applied": False,
                "trial_discount_rate": 0.0,
            }

        return base_quote
