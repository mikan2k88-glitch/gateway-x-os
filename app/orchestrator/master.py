from typing import Dict, Any, Optional, List
from app.db.repository import DatabaseRepository
from app.core.pricing import PricingEngine
from app.core.vetting import VettingEngine
from app.core.stripe_service import StripeService
from app.core.physical_execution import PhysicalExecutionRouter
from app.core.line_service import LineService
from app.core.execution_repository import ExecutionRepository
from app.sales.sales import SalesRepository
from app.sales.auth_gateway import AuthGateway
from app.sales.concierge_service import ConciergeService
from app.sales.strategy_planner import StrategyPlanner
from app.sales.strategy_executor import StrategyExecutor
from app.sales.sales_engine import SalesEngine
from app.sales.outreach_service import OutreachService
from app.sales.constraint_registry import ConstraintContext


class MasterOrchestrator:
    """
    Gateway X-OS Master Orchestrator
    Vetting → Pricing → 永続化 → 決済(Auth/Capture) → 現場実行(LINE連携) → 営業エンジン を統括する
    """
    def __init__(
        self,
        db_repository: Optional[DatabaseRepository] = None,
        sales_repo: Optional[SalesRepository] = None,
        stripe_service: Optional[StripeService] = None,
        line_service: Optional[LineService] = None,
        execution_repo: Optional[ExecutionRepository] = None,
    ):
        self.db = db_repository or DatabaseRepository()
        self.pricing_engine = PricingEngine()
        self.vetting_engine = VettingEngine()
        # AuthGatewayが参照するaccountsテーブルはsales_repo側にある。
        # db_repository(operations用)とは別のSQLiteファイル接続だが、
        # 同じgateway_x.dbを指す想定なのでdb_pathを揃えて渡すこと。
        self.sales_repo = sales_repo or SalesRepository()
        self.auth_gateway = AuthGateway(self.sales_repo)
        self.concierge_service = ConciergeService(self.sales_repo)
        self.stripe_service = stripe_service or StripeService()
        self.physical_router = PhysicalExecutionRouter()
        self.line_service = line_service or LineService()
        self.execution_repo = execution_repo or ExecutionRepository()

        # SalesEngine(討論→評価)とOutreachService(承認された戦略の実行)
        self.strategy_planner = StrategyPlanner(self.sales_repo)
        self.strategy_executor = StrategyExecutor(self.sales_repo)
        self.sales_engine = SalesEngine(self.strategy_planner, self.strategy_executor, self.sales_repo)
        self.outreach_service = OutreachService(self.sales_repo)

    async def create_execution_event(
        self,
        client_id: str,
        intent: str,
        quote: Dict[str, Any],
        vetting_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Vetting通過後、見積を永続化しイベントIDを発行する(まだ決済は行わない)"""
        quote_data = {**quote, "client_id": client_id, "intent": intent, "status": "QUOTED"}
        await self.db.save_quote(quote_data)
        await self.db.save_vetting_log({**vetting_result, "intent": intent, "client_id": client_id})
        await self.db.log_event("QUOTED", intent, f"Quote generated: {quote['quote_id']}", client_id)

        # AuthGatewayの承認カウントはここでは加算しない。
        # 「見積が出ただけ」は本当の意味での承認済み発注ではないため、
        # Capture確定(complete_dispatch/execute_physical_task内)まで持ち越す。

        return {"event_id": quote["quote_id"]}

    async def log_security_alert(self, client_id: str, intent: str, reason: str) -> None:
        """Vetting不合格時の拒絶ログ永続化"""
        await self.db.save_vetting_log({
            "passed": False, "reason": reason, "flagged_keywords": [],
            "intent": intent, "client_id": client_id
        })
        await self.db.log_event("DECLINED", intent, reason, client_id)
        # 却下された発注はAuthGatewayの承認カウントに影響しない(record_order_completionを呼ばない)
        # ConciergeServiceに却下理由を渡し、クライアントへ返す案内文を組み立ててもらう
        await self.concierge_service.notify_vetting_rejection(client_id, intent, reason)

    # ---------- LINE連携: 現場実行(非同期フロー) ----------

    async def dispatch_to_worker(
        self, client_id: str, quote: Dict[str, Any], worker_line_user_id: Optional[str] = None,
        payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Phase A(与信仮押さえ) → ワーカーへLINE通知 までを行い、DISPATCHED状態を返す。
        現場での実作業完了は非同期(LINE Webhook経由)で報告されるため、
        Capture(売上確定)はここでは行わない(complete_dispatch()で行う)。

        worker_line_user_id を省略した場合、sales_repo に登録済みの全アクティブワーカーへ
        一斉通知する(早い者勝ち方式)。complete_dispatch() はどのワーカーから「完了」報告が
        来ても execution_id が一致すれば処理するため、複数人に送っても問題なく機能する。
        """
        intent = quote.get("intent", "")
        tier = quote.get("tier", "economy")
        execution_id = f"exec_{quote['quote_id']}"

        # 宛先未指定なら登録済みワーカー全員をターゲットにする
        target_ids = [worker_line_user_id] if worker_line_user_id else [
            w["line_user_id"] for w in await self.sales_repo.get_active_workers()
        ]
        if not target_ids:
            return {
                "status": "NO_WORKER_AVAILABLE",
                "quote_id": quote["quote_id"],
                "reason": "登録済みのアクティブなワーカーがいません。worker_line_user_idを指定するか、"
                          "ワーカーにLINEで「登録」と送ってもらってください。",
            }

        auth_result = await self.stripe_service.authorize_payment(quote, payment_method_id)
        if not auth_result["success"]:
            await self.db.log_event("PAYMENT_AUTH_FAILED", intent, auth_result["reason"], client_id)
            await self.concierge_service.notify_payment_failure(client_id, quote["quote_id"], auth_result["reason"])
            return {"status": "PAYMENT_FAILED", "quote_id": quote["quote_id"], "reason": auth_result["reason"]}

        await self.db.log_event(
            "PAYMENT_AUTHORIZED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id
        )

        await self.execution_repo.create_dispatch(execution_id, {
            "client_id": client_id, "quote_id": quote["quote_id"],
            "payment_intent_id": auth_result["payment_intent_id"], "tier": tier, "intent": intent,
            "price_usd": quote["price_usd"], "margin_percent": quote.get("margin_percent", 0),
            "worker_line_user_id": worker_line_user_id,  # 一斉通知の場合はNoneのまま保存
        })

        notification_text = self.line_service.build_task_notification(execution_id, intent, tier)
        push_results = []
        for target_id in target_ids:
            result = await self.line_service.push_message(target_id, notification_text)
            push_results.append(result["success"])
        await self.db.log_event(
            "WORKER_NOTIFIED", intent,
            f"execution_id={execution_id}, targets={len(target_ids)}, "
            f"success_count={sum(push_results)}", client_id
        )

        return {
            "status": "DISPATCHED", "quote_id": quote["quote_id"], "execution_id": execution_id,
            "notified_workers": len(target_ids),
        }

    async def complete_dispatch(self, execution_id: str, field_status: str) -> Dict[str, Any]:
        """
        LINE Webhook経由でワーカーから完了/失敗報告を受けた際に呼ぶ。
        field_status: 'completed' | 'failed'
        completedならCapture(売上確定)、failedなら与信解放(Cancel)を行う。
        """
        dispatch = await self.execution_repo.get_dispatch(execution_id)
        if dispatch is None:
            return {"status": "NOT_FOUND", "execution_id": execution_id}
        if dispatch["status"] != "DISPATCHED":
            return {"status": "ALREADY_PROCESSED", "execution_id": execution_id, "current_status": dispatch["status"]}

        client_id = dispatch["client_id"]
        intent = dispatch["intent"]

        if field_status == "completed":
            capture_result = await self.stripe_service.capture_payment(dispatch["payment_intent_id"])
            if not capture_result["success"]:
                await self.db.log_event("PAYMENT_CAPTURE_FAILED", intent, capture_result["reason"], client_id)
                await self.concierge_service.notify_payment_failure(client_id, dispatch["quote_id"], capture_result["reason"])
                await self.execution_repo.update_status(execution_id, "CAPTURE_FAILED")
                return {"status": "CAPTURE_FAILED", "execution_id": execution_id, "reason": capture_result["reason"]}

            await self.db.log_event(
                "CAPTURED", intent, f"payment_intent={dispatch['payment_intent_id']}", client_id
            )
            await self.auth_gateway.record_order_completion(client_id, intent, dispatch["tier"])
            await self.execution_repo.update_status(execution_id, "COMPLETED")
            return {
                "status": "COMPLETED", "execution_id": execution_id,
                "revenue_captured_usd": dispatch["price_usd"],
                "net_profit_usd": round(dispatch["price_usd"] * dispatch["margin_percent"] / 100, 2),
            }
        else:
            cancel_result = await self.stripe_service.cancel_payment(
                dispatch["payment_intent_id"], reason="worker_reported_failure"
            )
            await self.db.log_event("PAYMENT_CANCELED", intent, "worker reported failure", client_id)
            await self.execution_repo.update_status(execution_id, "FAILED")
            return {"status": "FAILED", "execution_id": execution_id, "payment_canceled": cancel_result["success"]}

    async def handle_chargeback(self, dispute_id: str, payment_intent_id: str) -> Dict[str, Any]:
        """
        Stripeからのdispute(チャージバック)Webhook受信時に呼ぶ。
        dispute_idは証拠提出先の特定に、payment_intent_idはこちら側の監査ログ
        (dispatchレコード)の逆引きに使う。

        「サービスは実際に完了報告(LINE)を受けて提供済みである」ことを示す証拠を
        自動的に組み立ててStripeへ提出する。completed以外(FAILED等)のdispatchや、
        そもそも見つからない場合は、正当な異議申立ての可能性があるため自動証拠提出は
        行わず、要人力確認としてログのみ残す。
        """
        dispatch = await self.execution_repo.get_dispatch_by_payment_intent(payment_intent_id)
        if dispatch is None:
            await self.db.log_event(
                "CHARGEBACK_UNRESOLVED", "", f"payment_intent={payment_intent_id}: dispatch not found", "unknown"
            )
            return {"status": "NO_RECORD", "payment_intent_id": payment_intent_id}

        await self.db.log_event(
            "CHARGEBACK_RECEIVED", dispatch["intent"], f"execution_id={dispatch['execution_id']}",
            dispatch["client_id"]
        )

        if dispatch["status"] != "COMPLETED":
            # 完了報告が無いままの注文へのチャージバックは、こちら側にも非がある可能性があるため
            # 自動反論はせず、要人力レビューとして記録するに留める
            await self.db.log_event(
                "CHARGEBACK_NEEDS_MANUAL_REVIEW", dispatch["intent"],
                f"execution_id={dispatch['execution_id']}, dispatch_status={dispatch['status']}",
                dispatch["client_id"]
            )
            return {
                "status": "NEEDS_MANUAL_REVIEW", "payment_intent_id": payment_intent_id,
                "execution_id": dispatch["execution_id"], "dispatch_status": dispatch["status"],
            }

        # 完了報告(LINE経由)を受けて実際にCapture済みの注文 -> 自動で証拠を組み立てて提出
        evidence_text = (
            f"Gateway X-OS 業務実行記録\n"
            f"管理番号(execution_id): {dispatch['execution_id']}\n"
            f"依頼内容: {dispatch['intent']}\n"
            f"サービス階級(tier): {dispatch['tier']}\n"
            f"担当ワーカーLINEユーザーID: {dispatch['worker_line_user_id'] or '(一斉通知、応答者が担当)'}\n"
            f"完了報告受信日時(UTC): {dispatch['updated_at']}\n"
            f"決済確定(Capture)日時: 完了報告受信と同時に自動確定\n"
            f"本注文はLINE Messaging API経由でワーカーから明示的な完了報告を受けた後、"
            f"システムが自動的に決済を確定させたものです。"
        )
        result = await self.stripe_service.submit_dispute_evidence(dispute_id, evidence_text)
        await self.db.log_event(
            "CHARGEBACK_EVIDENCE_SUBMITTED", dispatch["intent"],
            f"execution_id={dispatch['execution_id']}, success={result.get('success')}",
            dispatch["client_id"]
        )
        return {
            "status": "EVIDENCE_SUBMITTED", "payment_intent_id": payment_intent_id,
            "execution_id": dispatch["execution_id"], "evidence_text": evidence_text,
            "submission_result": result,
        }

    async def execute_physical_task(
        self, client_id: str, quote: Dict[str, Any], payment_method_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        [レガシー/テスト用フォールバック] worker_line_user_id指定が無く、かつ
        登録済みワーカーが1人もいない場合の同期実行パス。
        実ワーカーとのLINE連携が無い環境での動作確認用に残している。
        本番でワーカーが登録されている場合は dispatch_to_worker() を使うこと。
        """
        intent = quote.get("intent", "")
        tier = quote.get("tier", "economy")

        auth_result = await self.stripe_service.authorize_payment(quote, payment_method_id)
        if not auth_result["success"]:
            await self.db.log_event("PAYMENT_AUTH_FAILED", intent, auth_result["reason"], client_id)
            await self.concierge_service.notify_payment_failure(client_id, quote["quote_id"], auth_result["reason"])
            return {"status": "PAYMENT_FAILED", "quote_id": quote["quote_id"], "reason": auth_result["reason"]}

        await self.db.log_event(
            "PAYMENT_AUTHORIZED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id
        )
        field_result = self.physical_router.route(tier, intent)

        if field_result["field_status"] != "PRE_INSPECTED_PASSED":
            cancel_result = await self.stripe_service.cancel_payment(
                auth_result["payment_intent_id"], reason=field_result["field_status"]
            )
            await self.db.log_event("PAYMENT_CANCELED", intent, f"field_status={field_result['field_status']}", client_id)
            return {
                "status": "EXECUTION_FAILED", "quote_id": quote["quote_id"],
                "reason": field_result["field_status"], "payment_canceled": cancel_result["success"],
            }

        capture_result = await self.stripe_service.capture_payment(auth_result["payment_intent_id"])
        if not capture_result["success"]:
            await self.db.log_event("PAYMENT_CAPTURE_FAILED", intent, capture_result["reason"], client_id)
            await self.concierge_service.notify_payment_failure(client_id, quote["quote_id"], capture_result["reason"])
            return {"status": "CAPTURE_FAILED", "quote_id": quote["quote_id"], "reason": capture_result["reason"]}

        await self.db.log_event("CAPTURED", intent, f"payment_intent={auth_result['payment_intent_id']}", client_id)
        await self.auth_gateway.record_order_completion(client_id, intent, tier)

        return {
            "status": "COMPLETED", "quote_id": quote["quote_id"],
            "execution_id": f"exec_{quote['quote_id']}",
            "assigned_to": field_result["assigned_to"],
            "revenue_captured_usd": quote["price_usd"],
            "net_profit_usd": round(quote["price_usd"] * quote.get("margin_percent", 0) / 100, 2),
        }

    # ---------- 営業エンジン(SalesEngine/OutreachService) ----------

    async def run_strategy_cycle(
        self, topic: str, context: str, constraint_ctx: ConstraintContext,
        target_client_ids: Optional[List[str]] = None, max_rounds: Optional[int] = None,
        skip_feature_detection: bool = False,
    ) -> Dict[str, Any]:
        """
        討論(StrategyPlanner) → 評価(StrategyExecutor) → 承認されれば実行(OutreachService)
        までを1回でまとめて行う。target_client_ids未指定の場合は、sales_repoのleadsテーブルから
        stage='lead'のクライアントを自動的に対象にする(新規リードへのトライアル案内が主目的のため)。

        max_roundsを指定すると、その回のみ討論のラウンド数上限を一時的に上書きする
        (複雑な議題で3ラウンドでは収束しないケースが実際に確認されたため追加)。

        skip_feature_detection=Trueにすると、承認時のdetect_feature_request呼び出し
        (LLM呼び出し1回分)を省略できる。応答速度を優先したい場合に使う
        (2026-08-20の応答遅延調査を受けて追加)。
        """
        original_max_rounds = self.strategy_planner.max_rounds
        if max_rounds is not None:
            self.strategy_planner.max_rounds = max_rounds
        try:
            result = await self.sales_engine.run_strategy_cycle(
                topic, context, constraint_ctx, skip_feature_detection=skip_feature_detection
            )
        finally:
            self.strategy_planner.max_rounds = original_max_rounds

        if result["stage"] != "approved":
            return {**result, "outreach": None}

        if target_client_ids is None:
            leads = await self.sales_repo.get_leads_by_stage("lead")
            target_client_ids = [lead["client_id"] for lead in leads]

        outreach_result = await self.outreach_service.run_from_strategy_result(result, target_client_ids)
        return {**result, "outreach": outreach_result}
