import sqlite3
import asyncio
from typing import Any, Dict, Optional

from typing import List


class ExecutionRepository:
    """
    ワーカーへのタスク派遣(dispatch)の状態を永続化する。

    現場での作業完了は「LINEでワーカーが返信するまで」という非同期の出来事なので、
    /mcp/v1/tools/execute の1リクエスト内では完結しない。dispatches テーブルに
    DISPATCHED状態で記録しておき、LINE Webhookが完了報告を受け取った時点で
    COMPLETED/FAILEDに更新する。

    repository.py(operations用)/sales.py(CRM用)と同じ設計方針(生SQLite、
    asyncio.to_threadで非同期化)を踏襲し、同じgateway_x.dbファイルを共有する想定。
    """

    def __init__(self, db_path: str = "gateway_x.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatches (
                execution_id TEXT PRIMARY KEY,
                client_id TEXT,
                quote_id TEXT,
                payment_intent_id TEXT,
                tier TEXT,
                intent TEXT,
                price_usd REAL,
                margin_percent REAL,
                worker_line_user_id TEXT,
                status TEXT DEFAULT 'DISPATCHED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.commit()

    async def create_dispatch(self, execution_id: str, data: Dict[str, Any]) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO dispatches
                (execution_id, client_id, quote_id, payment_intent_id, tier, intent,
                 price_usd, margin_percent, worker_line_user_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISPATCHED')
                """, (
                    execution_id, data.get("client_id"), data.get("quote_id"),
                    data.get("payment_intent_id"), data.get("tier"), data.get("intent"),
                    data.get("price_usd"), data.get("margin_percent"), data.get("worker_line_user_id"),
                ))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_dispatch(self, execution_id: str) -> Optional[Dict[str, Any]]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM dispatches WHERE execution_id = ?", (execution_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)

    async def update_status(self, execution_id: str, status: str) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                UPDATE dispatches SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE execution_id = ?
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
                cursor.execute("""
                SELECT * FROM dispatches
                WHERE worker_line_user_id = ? AND status = 'DISPATCHED'
                ORDER BY created_at DESC LIMIT 1
                """, (worker_line_user_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)

    async def get_dispatch_by_payment_intent(self, payment_intent_id: str) -> Optional[Dict[str, Any]]:
        """
        Stripeのチャージバック(dispute)Webhookはpayment_intent_idしか渡してこないため、
        そこから対応するdispatchレコード(証拠提出に使う監査ログ)を逆引きする。
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM dispatches WHERE payment_intent_id = ?", (payment_intent_id,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)
