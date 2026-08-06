import sqlite3
import asyncio
import json
from typing import Dict, Any, List, Optional


class DatabaseRepository:
    """
    Gateway X-OS 統合データベースリポジトリ
    SQLite (WALモード) を使用し、非同期・高並列での高速永続化・ログ・学習ルールの保存を担う
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
            cursor = conn.cursor()

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS quotes (
                quote_id TEXT PRIMARY KEY,
                client_id TEXT,
                intent TEXT,
                tier TEXT,
                price_usd REAL,
                cost_jpy REAL,
                margin_percent REAL,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS vetting_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT,
                intent TEXT,
                passed BOOLEAN,
                reason TEXT,
                flagged_keywords TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS learned_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_summary TEXT,
                source TEXT,
                applied_count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id TEXT,
                client_id TEXT,
                rating INTEGER,
                feedback_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 汎用イベントログ（DECLINED/QUOTED等の状態遷移を一元管理）
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                intent TEXT,
                detail TEXT,
                client_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            conn.commit()

    async def save_quote(self, quote_data: Dict[str, Any]) -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT OR REPLACE INTO quotes
                (quote_id, client_id, intent, tier, price_usd, cost_jpy, margin_percent, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    quote_data.get("quote_id"),
                    quote_data.get("client_id", "anonymous"),
                    quote_data.get("intent", ""),
                    quote_data.get("tier", "economy"),
                    quote_data.get("price_usd", 0.0),
                    quote_data.get("cost_jpy", 0.0),
                    quote_data.get("margin_percent", 0.0),
                    quote_data.get("status", "QUOTED")
                ))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def save_vetting_log(self, vetting_data: Dict[str, Any]) -> None:
        def _execute():
            with self._get_connection() as conn:
                flagged = json.dumps(vetting_data.get("flagged_keywords", []))
                conn.execute("""
                INSERT INTO vetting_logs (client_id, intent, passed, reason, flagged_keywords)
                VALUES (?, ?, ?, ?, ?)
                """, (
                    vetting_data.get("client_id", "anonymous"),
                    vetting_data.get("intent", ""),
                    vetting_data.get("passed", False),
                    vetting_data.get("reason", ""),
                    flagged
                ))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def save_learned_rule(self, rule_summary: str, source: str = "feedback_loop") -> None:
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO learned_rules (rule_summary, source)
                VALUES (?, ?)
                """, (rule_summary, source))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_learned_rules(self) -> List[str]:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT rule_summary FROM learned_rules ORDER BY id DESC LIMIT 50")
                rows = cursor.fetchall()
                return [row["rule_summary"] for row in rows]
        return await asyncio.to_thread(_execute)

    async def save_feedback(
        self,
        task_id: str,
        client_id: str,
        rating: int,
        feedback_text: str
    ) -> None:
        """クライアントAIからのフィードバック保存（位置引数版）"""
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO feedback_logs (quote_id, client_id, rating, feedback_text)
                VALUES (?, ?, ?, ?)
                """, (task_id, client_id, rating, feedback_text))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def optimize_instructions_from_feedback(self, feedback_text: str) -> None:
        """フィードバックからシステムプロンプト/ナレッジへの還元（現状はルール化して保存するのみ）"""
        if not feedback_text:
            return
        summary = feedback_text.strip()[:280]
        await self.save_learned_rule(summary, source="feedback_loop")

    async def log_event(self, event_type: str, intent: str, detail: str, client_id: str = "anonymous") -> None:
        """MasterOrchestratorからの汎用イベントログ"""
        def _execute():
            with self._get_connection() as conn:
                conn.execute("""
                INSERT INTO event_logs (event_type, intent, detail, client_id)
                VALUES (?, ?, ?, ?)
                """, (event_type, intent, detail, client_id))
                conn.commit()
        await asyncio.to_thread(_execute)
