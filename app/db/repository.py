"""
app/db/repository.py
---------------------
Gateway X-OS 統合データベースリポジトリ
PostgreSQL (Supabase) / SQLite ハイブリッド永続化。
DATABASE_URL環境変数が設定されていればPostgres(Company Xと同一のSupabaseプロジェクトを
共有する運用を想定)、未設定ならSQLiteにフォールバックする。
Company Xの db/company_repository.py と同じパターンを踏襲している。
テーブル名はCompany X側(growth_backlog, system_config)と衝突しないことを確認済み。
"""

import os
import sqlite3
import asyncio
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("gateway_x.repository")

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False


class DatabaseRepository:
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

            if self.use_postgres:
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS quotes (
                    quote_id TEXT PRIMARY KEY,
                    client_id TEXT,
                    intent TEXT,
                    tier TEXT,
                    price_usd DOUBLE PRECISION,
                    cost_jpy DOUBLE PRECISION,
                    margin_percent DOUBLE PRECISION,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS vetting_logs (
                    id SERIAL PRIMARY KEY,
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
                    id SERIAL PRIMARY KEY,
                    rule_summary TEXT,
                    source TEXT,
                    applied_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS feedback_logs (
                    id SERIAL PRIMARY KEY,
                    quote_id TEXT,
                    client_id TEXT,
                    rating INTEGER,
                    feedback_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_logs (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT,
                    intent TEXT,
                    detail TEXT,
                    client_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS capability_rules (
                    id SERIAL PRIMARY KEY,
                    keyword TEXT NOT NULL,
                    allowed BOOLEAN NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT DEFAULT 'seed',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
            else:
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
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS capability_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    allowed BOOLEAN NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT DEFAULT 'seed',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)

            cursor.execute("SELECT COUNT(*) FROM capability_rules")
            row = cursor.fetchone()
            count = row["count"] if self.use_postgres else row[0]
            if count == 0:
                seed_rules = [
                    ("プログラミング", False, "ソフトウェア開発はタイミーワーカーが現地で遂行できる物理タスクではありません"),
                    ("コーディング", False, "コーディング作業はGateway Xの対応範囲外です(物理タスク専門)"),
                    ("ローカライズ", False, "文書・ソフトウェアのローカライズはリモート知的労働のため対応範囲外です"),
                    ("データ構造化", False, "データ処理・分析系のリモート知的労働は対応範囲外です"),
                    ("LLM", False, "AI/LLM関連の技術タスクはタイミーワーカーの物理タスクに変換できません"),
                    ("翻訳", False, "文書翻訳はリモート知的労働のため対応範囲外です"),
                    ("行列", True, "行列確認・順番待ちは典型的な物理タスクです"),
                    ("配達", True, "配達・受け取り代行は典型的な物理タスクです"),
                    ("買い物", True, "買い物代行は典型的な物理タスクです"),
                    ("確認", True, "現地確認・視察は典型的な物理タスクです"),
                ]
                if self.use_postgres:
                    cursor.executemany(
                        "INSERT INTO capability_rules (keyword, allowed, reason, source) VALUES (%s, %s, %s, 'seed')",
                        seed_rules,
                    )
                else:
                    cursor.executemany(
                        "INSERT INTO capability_rules (keyword, allowed, reason, source) VALUES (?, ?, ?, 'seed')",
                        seed_rules,
                    )

            conn.commit()
            mode = "PostgreSQL (Supabase, Company Xと共有)" if self.use_postgres else "SQLite"
            logger.info(f"🗄️ {mode} データベースを初期化完了しました。")

    async def get_capability_rules(self) -> List[Dict[str, Any]]:
        """オーナー対話(Concierge)向け: 現在の実行可能性ルール一覧をそのまま返す"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT keyword, allowed, reason FROM capability_rules ORDER BY id")
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    async def check_capability(self, intent: str) -> Dict[str, Any]:
        """
        依頼内容(intent)がGateway Xの実行可能な業務範囲かどうかを判定する。
        capability_rulesに登録されたキーワードと部分一致するかで判定する簡易実装。
        - allowed=False のキーワードに1つでも一致 → 対応不可(reasonを返す)
        - 何もヒットしない場合は「デフォルト許可」(過検知よりも見逃しを許容する設計)
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT keyword, allowed, reason FROM capability_rules")
                return [dict(row) for row in cursor.fetchall()]
        rules = await asyncio.to_thread(_execute)

        for rule in rules:
            if not rule["allowed"] and rule["keyword"] in intent:
                return {"feasible": False, "reason": rule["reason"], "matched_keyword": rule["keyword"]}
        return {"feasible": True, "reason": None, "matched_keyword": None}

    async def save_quote(self, quote_data: Dict[str, Any]) -> None:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                params = (
                    quote_data.get("quote_id"),
                    quote_data.get("client_id", "anonymous"),
                    quote_data.get("intent", ""),
                    quote_data.get("tier", "economy"),
                    quote_data.get("price_usd", 0.0),
                    quote_data.get("cost_jpy", 0.0),
                    quote_data.get("margin_percent", 0.0),
                    quote_data.get("status", "QUOTED"),
                )
                if self.use_postgres:
                    cursor.execute("""
                    INSERT INTO quotes
                    (quote_id, client_id, intent, tier, price_usd, cost_jpy, margin_percent, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (quote_id) DO UPDATE SET
                        client_id = EXCLUDED.client_id, intent = EXCLUDED.intent,
                        tier = EXCLUDED.tier, price_usd = EXCLUDED.price_usd,
                        cost_jpy = EXCLUDED.cost_jpy, margin_percent = EXCLUDED.margin_percent,
                        status = EXCLUDED.status
                    """, params)
                else:
                    cursor.execute("""
                    INSERT OR REPLACE INTO quotes
                    (quote_id, client_id, intent, tier, price_usd, cost_jpy, margin_percent, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, params)
                conn.commit()
        await asyncio.to_thread(_execute)

    async def save_vetting_log(self, vetting_data: Dict[str, Any]) -> None:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                flagged = json.dumps(vetting_data.get("flagged_keywords", []))
                params = (
                    vetting_data.get("client_id", "anonymous"),
                    vetting_data.get("intent", ""),
                    vetting_data.get("passed", False),
                    vetting_data.get("reason", ""),
                    flagged,
                )
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                INSERT INTO vetting_logs (client_id, intent, passed, reason, flagged_keywords)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph})
                """, params)
                conn.commit()
        await asyncio.to_thread(_execute)

    async def save_learned_rule(self, rule_summary: str, source: str = "feedback_loop") -> None:
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                INSERT INTO learned_rules (rule_summary, source)
                VALUES ({ph}, {ph})
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
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                INSERT INTO feedback_logs (quote_id, client_id, rating, feedback_text)
                VALUES ({ph}, {ph}, {ph}, {ph})
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
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(f"""
                INSERT INTO event_logs (event_type, intent, detail, client_id)
                VALUES ({ph}, {ph}, {ph}, {ph})
                """, (event_type, intent, detail, client_id))
                conn.commit()
        await asyncio.to_thread(_execute)

    async def get_recent_quotes(self, limit: int = 50) -> List[Dict[str, Any]]:
        """モニター用: 直近の見積(注文)一覧をステータス問わず新しい順で返す"""
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                cursor.execute(
                    f"SELECT * FROM quotes ORDER BY created_at DESC LIMIT {ph}", (limit,)
                )
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)

    async def get_recent_events(
        self, limit: int = 50, event_types: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        モニター用: 直近のイベントログを新しい順で返す。
        event_types を指定すると、その種類のみに絞り込む(アラート専用ビュー等で使用)。
        """
        def _execute():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                ph = "%s" if self.use_postgres else "?"
                if event_types:
                    placeholders = ",".join(ph for _ in event_types)
                    cursor.execute(
                        f"SELECT * FROM event_logs WHERE event_type IN ({placeholders}) "
                        f"ORDER BY created_at DESC LIMIT {ph}",
                        (*event_types, limit),
                    )
                else:
                    cursor.execute(
                        f"SELECT * FROM event_logs ORDER BY created_at DESC LIMIT {ph}", (limit,)
                    )
                return [dict(row) for row in cursor.fetchall()]
        return await asyncio.to_thread(_execute)
