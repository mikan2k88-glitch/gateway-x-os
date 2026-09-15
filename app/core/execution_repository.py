import os
import sqlite3
import asyncio
import logging
from typing import Any, Dict, Optional, List

logger = logging.getLogger("gateway_x.execution_repository")

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False


class ExecutionRepository:
    """
    ワーカーへのタスク派遣(dispatch)の状態を永続化する。

    現場での作業完了は「LINEでワーカーが返信するまで」という非同期の出来事なので、
    /mcp/v1/tools/execute の1リクエスト内では完結しない。dispatches テーブルに
    DISPATCHED状態で記録しておき、LINE Webhookが完了報告を受け取った時点で
    COMPLETED/FAILEDに更新する。

    repository.py(operations用)/sales.py(CRM用)と同じ設計方針(DATABASE_URLが
    あればPostgres/Supabase、無ければSQLiteのハイブリッド、asyncio.to_threadで
    非同期化)を踏襲している。
    """

    def __init__(self, db_path: str = "gateway_x.db"):
        self.db_path = db_path
        self.db_url = os.getenv("DATABASE_URL", "").strip().strip('"').strip("'")
        if self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)
        self.use_postgres = bool(self.db_url) and POSTGRES_AVAILABLE
        self._init_db()

    def _get_connection(self):
        if self.use_postgres:
            return psycopg2.connect(self.db_url, cursor_factory=RealDictCursor)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            price_type = "DOUBLE PRECISION" if self.use_postgres else "REAL"
            cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS dispatches (
                execution_id TEXT PRIMARY KEY,
                client_id TEXT,
                quote_id TEXT,
                payment_intent_id TEXT,
                tier TEXT,
                intent TEXT,
                channel TEXT DEFAULT 'physical',
                deliverable TEXT,
                price_usd {price_type},
                margin_percent {price_type},
                worker_line_user_id TEXT,
                status TEXT DEFAULT 'DISPATCHED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            # 既存(稼働中)のdispatchesテーブルには無い可能性があるため、マイグレーションで補う。
            if self.use_postgres:
                cursor.execute("ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS channel TEXT DEFAULT 'physical'")
                cursor.execute("ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS deliverable TEXT")
            else:
                for stmt in (
                    "ALTER TABLE dispatches ADD COLUMN channel TEXT DEFAULT 'physical'",
                    "ALTER TABLE dispatches ADD COLUMN deliverable TEXT",
                ):
                    try:
                        cursor.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # 既に列が存在する場合はスキップ
            conn.commit()
            mode = "PostgreSQL (Supabase, Company Xと共有)" if self.use_postgres else "SQLite"
            logger.info(f"🗄️ ExecutionRepository: {mode} で初期化完了しました。")

    async def create_dispatch(self, execution_id: str, data: Dict[str, Any]) -> None:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                INSERT INTO dispatches
                (execution_id, client_id, quote_id, payment_intent_id, tier, intent, channel,
                 price_usd, margin_percent, worker_line_user_id, status)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'DISPATCHED')
                """, (
                    execution_id, data.get("client_id"), data.get("quote_id"),
                    data.get("payment_intent_id"), data.get("tier"), data.get("intent"),
                    data.get("channel", "physical"),
                    data.get("price_usd"), data.get("margin_percent"), data.get("worker_line_user_id"),
                ))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def complete_digital_dispatch(self, execution_id: str, deliverable: str) -> None:
        """
        DigitalTaskEngineが即座に成果物を生成できた場合に呼ぶ。物理タスクのような
        DISPATCHED→(LINE Webhook経由の非同期報告)→COMPLETEDという2段階を経ず、
        1回でCOMPLETEDまで進める。
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                UPDATE dispatches SET status = 'COMPLETED', deliverable = {ph}, updated_at = CURRENT_TIMESTAMP
                WHERE execution_id = {ph}
                """, (deliverable, execution_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_dispatch(self, execution_id: str) -> Optional[Dict[str, Any]]:
        def _execute():

            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"SELECT * FROM dispatches WHERE execution_id = {ph}", (execution_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)

    async def update_status(self, execution_id: str, status: str) -> None:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                UPDATE dispatches SET status = {ph}, updated_at = CURRENT_TIMESTAMP
                WHERE execution_id = {ph}
                """, (status, execution_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def find_latest_dispatched_by_worker(self, worker_line_user_id: str) -> Optional[Dict[str, Any]]:
        """
        指定ワーカーの直近のDISPATCHED状態(未完了)の派遣を1件返す。
        LINE Webhookでワーカーからの返信を受けた際、どのタスクへの返信かを
        特定するために使う(返信メッセージに管理番号が含まれない簡易ケースの救済用)。
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                SELECT * FROM dispatches
                WHERE worker_line_user_id = {ph} AND status = 'DISPATCHED'
                ORDER BY created_at DESC LIMIT 1
                """, (worker_line_user_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)

    async def get_recent_dispatches(self, limit: int = 50) -> List[Dict[str, Any]]:
        """モニター用: 直近の派遣(dispatch)一覧をステータス問わず新しい順で返す"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(
                    f"SELECT * FROM dispatches ORDER BY created_at DESC LIMIT {ph}", (limit,)
                )
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    async def get_dispatch_by_payment_intent(self, payment_intent_id: str) -> Optional[Dict[str, Any]]:
        """
        Stripeのチャージバック(dispute)Webhookはpayment_intent_idしか渡してこないため、
        そこから対応するdispatchレコード(証拠提出に使う監査ログ)を逆引きする。
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(
                    f"SELECT * FROM dispatches WHERE payment_intent_id = {ph}", (payment_intent_id,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)
