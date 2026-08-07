import sqlite3
import asyncio
import json
from typing import Dict, Any, List, Optional


class SalesRepository:
    """
    Gateway X-OS 営業エンジン(SalesEngine) 用データ層

    既存の DatabaseRepository (repository.py / operations.py 想定) と同じ SQLite ファイルを
    共有し、責務だけを分離する。StrategyKnowledgeBase は独立ストアを持たず、このクラスが
    保持する accounts / leads / strategy_cycles / capacity_alerts の各テーブルがそれに当たる。

    - accounts: AuthGateway が発注パターンの認証状態を照会するためのテーブル
    - leads: OutreachService が更新する lead -> trial -> active のリード状態
    - strategy_cycles: StrategyPlanner の討論サイクル(提案/批判/修正)と、
      StrategyExecutor の判断結果・却下理由を保存する
    - capacity_alerts: 供給キャパシティ逼迫のログ(タイミーワーカー等の稼働可能量 vs 需要)
    """

    def __init__(self, db_path: str = "gateway_x.db"):
        # repository.py と同じ db_path を渡して同一DBファイルを共有する想定
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
            cursor = conn.cursor()

            # AuthGateway が「過去3回以上・同一パターンで承認済みか」を判定する際に参照する
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                client_id TEXT PRIMARY KEY,
                company_name TEXT,
                status TEXT DEFAULT 'lead',
                approved_pattern_count INTEGER DEFAULT 0,
                last_pattern_signature TEXT,
                auth_verified BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # OutreachService が lead -> trial -> active を更新
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT,
                source TEXT,
                stage TEXT DEFAULT 'lead',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # StrategyPlanner の討論サイクル + StrategyExecutor の判断結果
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_status TEXT DEFAULT 'debating',
                round_count INTEGER DEFAULT 0,
                proposal TEXT,
                critique TEXT,
                revision TEXT,
                executor_decision TEXT,
                executor_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 供給キャパシティ逼迫のアラートログ
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS capacity_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supply_channel TEXT,
                available_capacity REAL,
                demand REAL,
                alert_level TEXT,
                action_taken TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            conn.commit()

    # ---------- accounts (AuthGateway) ----------

    async def get_account(self, client_id: str) -> Optional[Dict[str, Any]]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM accounts WHERE client_id = ?", (client_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)

    async def is_routine_order(self, client_id: str, pattern_signature: str, threshold: int = 3) -> bool:
        """AuthGateway が定型ルート判定に使う。同一パターンで threshold 回以上承認済みか"""
        account = await self.get_account(client_id)
        if not account:
            return False
        return (
            account.get("auth_verified", False)
            and account.get("last_pattern_signature") == pattern_signature
            and account.get("approved_pattern_count", 0) >= threshold
        )

    async def record_approved_order(self, client_id: str, pattern_signature: str) -> None:
        """承認済み発注のたびに呼び、同一パターンならカウントを積み上げる"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM accounts WHERE client_id = ?", (client_id,))
                row = cursor.fetchone()
                if row is None:
                    conn.execute("""
                    INSERT INTO accounts (client_id, status, approved_pattern_count,
                        last_pattern_signature, auth_verified)
                    VALUES (?, 'active', 1, ?, 1)
                    """, (client_id, pattern_signature))
                else:
                    same_pattern = row["last_pattern_signature"] == pattern_signature
                    new_count = row["approved_pattern_count"] + 1 if same_pattern else 1
                    conn.execute("""
                    UPDATE accounts
                    SET approved_pattern_count = ?, last_pattern_signature = ?,
                        auth_verified = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE client_id = ?
                    """, (new_count, pattern_signature, client_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    # ---------- leads / OutreachService ----------

    async def create_lead(self, client_id: str, source: str, notes: str = "") -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO leads (client_id, source, stage, notes)
                VALUES (?, ?, 'lead', ?)
                """, (client_id, source, notes))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def update_lead_stage(self, client_id: str, stage: str) -> None:
        """stage は 'lead' | 'trial' | 'active' を想定"""
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                UPDATE leads SET stage = ?, updated_at = CURRENT_TIMESTAMP
                WHERE client_id = ?
                """, (stage, client_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_leads_by_stage(self, stage: str) -> List[Dict[str, Any]]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM leads WHERE stage = ? ORDER BY updated_at DESC", (stage,))
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    # ---------- strategy_cycles / Planner <-> Executor ----------

    async def start_strategy_cycle(self, proposal: str) -> int:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.execute("""
                INSERT INTO strategy_cycles (cycle_status, round_count, proposal)
                VALUES ('debating', 1, ?)
                """, (proposal,))
                conn.commit()
                return cursor.lastrowid
        return await asyncio.to_thread(_execute)

    async def update_debate_round(
        self, cycle_id: int, round_count: int, critique: str, revision: str
    ) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                UPDATE strategy_cycles
                SET round_count = ?, critique = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """, (round_count, critique, revision, cycle_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def resolve_cycle(self, cycle_id: int, status: str, decision: str, reason: str) -> None:
        """status は 'approved' | 'rejected' | 'pending'(3ラウンド未収束)を想定"""
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                UPDATE strategy_cycles
                SET cycle_status = ?, executor_decision = ?, executor_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """, (status, decision, reason, cycle_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_recent_cycles(self, limit: int = 20) -> List[Dict[str, Any]]:
        """次回のStrategyPlanner討論時に過去事例として参照する"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT * FROM strategy_cycles ORDER BY id DESC LIMIT ?
                """, (limit,))
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    async def get_pending_cycles(self) -> List[Dict[str, Any]]:
        """3ラウンド未収束で保留になったサイクルを次サイクルへ持ち越す際に使う"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM strategy_cycles WHERE cycle_status = 'pending'")
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    # ---------- capacity_alerts ----------

    async def log_capacity_alert(
        self,
        supply_channel: str,
        available_capacity: float,
        demand: float,
        alert_level: str,
        action_taken: str = "",
    ) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO capacity_alerts
                (supply_channel, available_capacity, demand, alert_level, action_taken)
                VALUES (?, ?, ?, ?, ?)
                """, (supply_channel, available_capacity, demand, alert_level, action_taken))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_latest_capacity_status(self, supply_channel: str) -> Optional[Dict[str, Any]]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT * FROM capacity_alerts WHERE supply_channel = ?
                ORDER BY created_at DESC LIMIT 1
                """, (supply_channel,))
                row = cursor.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_execute)
