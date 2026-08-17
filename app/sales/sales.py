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
    - quote_attempts: カーディング攻撃検知用。全ての見積発行試行(金額・時刻)を記録し、
      短時間に少額の見積を大量発行するパターン(盗難カードの有効性検証によく使われる手口)
      を検知するために使う
    - workers: LINE経由でタスクを受ける現場ワーカーの登録簿。ワーカーがボットに「登録」と
      送るとここに記録され、worker_line_user_id未指定でのタスク発行時に一斉通知の宛先となる
    - feature_requests: StrategyPlannerの討論中に「既存機能では対応できず、新機能が必要」と
      判断された場合に記録する。営業活動の副産物として開発ロードマップのヒントを蓄積する
    - concierge_messages: ConciergeServiceの会話履歴。クライアントごとのやり取りを永続化し、
      次回以降の対話で過去の文脈を引き継げるようにする
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

            # カーディング攻撃検知用: 見積発行試行のログ
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS quote_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT,
                price_usd REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # LINE経由の現場ワーカー登録簿
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                line_user_id TEXT PRIMARY KEY,
                display_name TEXT,
                active BOOLEAN DEFAULT 1,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 営業エンジンの討論から生まれた機能要望の記録簿
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS feature_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER,
                title TEXT,
                description TEXT,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # ConciergeServiceの会話履歴
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS concierge_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT,
                role TEXT,
                message TEXT,
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

    async def get_lead_by_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """指定クライアントの直近のleadレコードを1件返す(無ければNone)。重複作成の防止用"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM leads WHERE client_id = ? ORDER BY updated_at DESC LIMIT 1", (client_id,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
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

    # ---------- quote_attempts / カーディング検知 ----------

    async def log_quote_attempt(self, client_id: str, price_usd: float) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO quote_attempts (client_id, price_usd) VALUES (?, ?)
                """, (client_id, price_usd))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def count_recent_small_quotes(
        self, client_id: str, window_seconds: float, price_threshold_usd: float
    ) -> int:
        """
        直近 window_seconds 秒以内に発行された、price_threshold_usd 円未満の
        見積試行の件数を返す。カーディング攻撃(少額見積の大量発行)の検知に使う。
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT COUNT(*) as cnt FROM quote_attempts
                WHERE client_id = ?
                  AND price_usd < ?
                  AND created_at >= datetime('now', ?)
                """, (client_id, price_threshold_usd, f"-{window_seconds} seconds"))
                row = cursor.fetchone()
                return row["cnt"] if row else 0
        return await asyncio.to_thread(_execute)

    # ---------- workers / LINE経由のワーカー登録 ----------

    async def register_worker(self, line_user_id: str, display_name: str = "") -> None:
        """既に登録済みなら再登録扱い(display_name更新、activeをTrueに戻す)にする"""
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO workers (line_user_id, display_name, active)
                VALUES (?, ?, 1)
                ON CONFLICT(line_user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    active = 1
                """, (line_user_id, display_name))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_active_workers(self) -> List[Dict[str, Any]]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM workers WHERE active = 1")
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    async def deactivate_worker(self, line_user_id: str) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("UPDATE workers SET active = 0 WHERE line_user_id = ?", (line_user_id,))
                conn.commit()
        await asyncio.to_thread(_execute)

    # ---------- feature_requests / 営業エンジンからの機能要望 ----------

    async def create_feature_request(self, cycle_id: int, title: str, description: str) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO feature_requests (cycle_id, title, description, status)
                VALUES (?, ?, ?, 'open')
                """, (cycle_id, title, description))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_open_feature_requests(self) -> List[Dict[str, Any]]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM feature_requests WHERE status = 'open' ORDER BY created_at DESC"
                )
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    # ---------- concierge_messages / 会話履歴 ----------

    async def append_concierge_message(self, client_id: str, role: str, message: str) -> None:
        """role は 'user' | 'concierge' を想定"""
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO concierge_messages (client_id, role, message) VALUES (?, ?, ?)
                """, (client_id, role, message))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_concierge_history(self, client_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """直近の会話履歴を古い順で返す(promptに時系列で組み込みやすくするため)"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                SELECT * FROM (
                    SELECT * FROM concierge_messages WHERE client_id = ?
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """, (client_id, limit))
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)
