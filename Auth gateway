import hashlib
import json
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .sales import SalesRepository

RouteDecision = Literal["routine", "concierge"]


def build_pattern_signature(intent: str, tier: str) -> str:
    """
    「同一パターン」を表す文字列を組み立てるユーティリティ。
    intent(発注内容の種別)とtier(economy/tactical等)から決定論的なハッシュを作る。
    QuoteBuilder/MasterOrchestrator側が見積作成時に同じ入力から同じ値を再現できるよう、
    正規化(小文字化・空白除去)してからハッシュ化する。
    """
    normalized = json.dumps(
        {"intent": intent.strip().lower(), "tier": tier.strip().lower()},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class AuthGateway:
    """
    MasterOrchestrator前段のルーティング層。

    - 同一クライアントが同一パターン(intent+tier)で threshold 回以上承認済みなら
      'routine'(MasterOrchestratorへ直行)と判定
    - それ以外(初回・パターン変化・未承認)は 'concierge'(ConciergeServiceへ)と判定

    AuthGateway自身はVetting/Pricingの合否を判断しない。承認された発注が完了した後、
    呼び出し側(MasterOrchestrator)が record_order_completion() を呼ぶことで、
    accountsテーブルの承認カウントが積み上がっていく。
    """

    def __init__(self, sales_repo: SalesRepository, routine_threshold: int = 3):
        self.sales_repo = sales_repo
        self.routine_threshold = routine_threshold

    async def decide_route(self, client_id: str, intent: str, tier: str) -> Dict[str, Any]:
        pattern_signature = build_pattern_signature(intent, tier)
        is_routine = await self.sales_repo.is_routine_order(
            client_id, pattern_signature, threshold=self.routine_threshold
        )
        route: RouteDecision = "routine" if is_routine else "concierge"
        return {
            "client_id": client_id,
            "route": route,
            "pattern_signature": pattern_signature,
        }

    async def record_order_completion(self, client_id: str, intent: str, tier: str) -> None:
        """MasterOrchestrator側でVetting/Pricingが承認・完了した発注について呼ぶ"""
        pattern_signature = build_pattern_signature(intent, tier)
        await self.sales_repo.record_approved_order(client_id, pattern_signature)


# ---------- FastAPI ルーター ----------
# main.py 側で `app.include_router(auth_gateway_router)` する想定。
# SalesRepository のインスタンスは main.py 側で1つ作り、
# create_auth_gateway_router(sales_repo) に渡して使う。

class RouteRequest(BaseModel):
    client_id: str
    intent: str
    tier: str = "economy"


class RouteResponse(BaseModel):
    client_id: str
    route: RouteDecision
    pattern_signature: str


def create_auth_gateway_router(sales_repo: SalesRepository) -> APIRouter:
    router = APIRouter(prefix="/gateway", tags=["auth-gateway"])
    gateway = AuthGateway(sales_repo)

    @router.post("/route", response_model=RouteResponse)
    async def route_order(payload: RouteRequest) -> RouteResponse:
        if not payload.client_id:
            raise HTTPException(status_code=400, detail="client_id is required")
        result = await gateway.decide_route(payload.client_id, payload.intent, payload.tier)
        return RouteResponse(**result)

    return router
