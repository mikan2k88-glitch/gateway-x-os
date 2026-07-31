import sqlite3
import asyncio
from typing import Dict, Any, List

class DatabaseRepository:
    """Gateway X-OS SQLite 非同期永続化リポジトリ"""
    def __init__(self, db_path: str = "gateway_x.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT,
                    intent TEXT,
                    tier TEXT,
                    status TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learned_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    async def save_execution_log(self, data: Dict[str, Any]):
        def _save():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO execution_logs (client_id, intent, tier, status) VALUES (?, ?, ?, ?)",
                    (data.get("client_id"), data.get("intent"), data.get("tier"), data.get("status"))
                )
                conn.commit()
        await asyncio.to_thread(_save)

    async def get_learned_rules(self) -> List[str]:
        def _get():
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT rule FROM learned_rules ORDER BY id DESC LIMIT 5")
                return [row[0] for row in cursor.fetchall()]
        return await asyncio.to_thread(_get)
