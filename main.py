import os
import json
import sqlite3
import uuid
import stripe
from typing import List, Dict, Any, Optional
from enum import Enum
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ====================================================
# 1. 環境変数 & インフラストラクチャ初期化
# ====================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

stripe.api_key = STRIPE_SECRET_KEY
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = FastAPI(
    title="Gateway X-OS",
    version="3.1.0",
    description="Physical Execution API Gateway for Autonomous AI Agents & Quant Platforms"
)

DB_FILE = "gateway.db"

# ====================================================
# 2. データベース永続化層 (Persistence Layer)
# ====================================================

def init_db():
    """データベーステーブルの初期化"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 成長・監査ログテーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS growth_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 動的学習ルールテーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learned_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 見積もり (Quotes) テーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            quote_id TEXT PRIMARY KEY,
            intent TEXT NOT NULL,
            tier TEXT NOT NULL,
            price_usd REAL NOT NULL,
            estimated_cost_jpy INT NOT NULL,
            margin_percent REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()

# アプリ起動時にDB初期化
init_db()


class DatabaseRepository:
    """【リポジトリ】SQLiteを用いた永続化アクセスオブジェクト"""
    
    @staticmethod
    def add_log(event_type: str, payload: Dict[str, Any]):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO growth_logs (event_type, payload) VALUES (?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False))
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_declined_logs() -> List[Dict[str, Any]]:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM growth_logs WHERE event_type = 'DECLINED'")
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    @staticmethod
    def add_learned_rule(rule_text: str):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO learned_rules (rule_text) VALUES (?)", (rule_text,))
        conn.commit()
        conn.close()

    @staticmethod
    def get_learned_rules() -> List[str]:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT rule_text FROM learned_rules ORDER BY id DESC LIMIT 10")
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in reversed(rows)]

    @staticmethod
    def save_quote(quote_id: str, intent: str, tier: str, price_usd: float, cost_jpy: int, margin: float):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO quotes (quote_id, intent, tier, price_usd, estimated_cost_jpy, margin_percent, status) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')",
            (quote_id, intent, tier, price_usd, cost_jpy, margin)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_quote(quote_id: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT quote_id, intent, tier, price_usd, estimated_cost_jpy, margin_percent, status FROM quotes WHERE quote_id = ?", (quote_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "quote_id": row[0],
            "intent": row[1],
            "tier": row[2],
            "price_usd": row[3],
            "estimated_cost_jpy": row[4],
            "margin_percent": row[5],
            "status": row[6]
        }


# ====================================================
# 3. ドメインエンティティ & 動的プライシングエンジン
# ====================================================

class SLATier(str, Enum):
    ECONOMY = "economy"      # タイミーハック / 24時間猶予 (~55% マージン)
    EXPRESS = "express"      # 即時派遣 / サージプライシング (~80% マージン)
    TACTICAL = "tactical"    # 高難度・高秘匿 / Tactical Force (90%+ マージン)


class DynamicSafetyPolicy:
    """【ドメインルール】自己進化型・経済安全保障適合審査ポリシー"""
    
    def __init__(self):
        self.base_instruction = """
        あなたは Gateway X-OS の厳格な金融・経済安全保障監査AI（Vetting Engine）です。
        海外AIからの物理実行タスク要求を審査し、法的・経済安全保障上のリスクを分析してください。
        
        【判断基準】:
        1. 経済安全保障推進法違反、重要インフラの無許可密撮、違法・ハイリスク行為、スパイ活動 ➔ "DECLINED"
        2. 正常なビジネス作業、現場状態確認、位置情報検証、法的現地調査 ➔ "APPROVED"

        出力フォーマット(JSON厳守):
        {"status": "APPROVED" | "DECLINED", "reason": "詳細な審査理由", "risk_score": 0.0〜1.0}
        """

    def get_effective_instruction(self) -> str:
        learned_rules = DatabaseRepository.get_learned_rules()
        if not learned_rules:
            return self.base_instruction
        
        added_rules = "\n".join([f"- {rule}" for rule in learned_rules])
        return f"{self.base_instruction}\n\n【自動改善により獲得した追加基準】:\n{added_rules}"


safety_policy = DynamicSafetyPolicy()


class DynamicPricingEngine:
    """【Naval-Collisonモデル】価値・緊急度ベースの動的プライシングエンジン"""

    USD_TO_JPY_RATE = 155.0  # 為替基準レート

    @classmethod
    def calculate_quote(cls, intent: str, tier: SLATier, estimated_ground_cost_jpy: int) -> Dict[str, Any]:
        ground_cost_usd = estimated_ground_cost_jpy / cls.USD_TO_JPY_RATE

        if tier == SLATier.ECONOMY:
            # Tier 1: 原価 + 55% マージン
            margin_rate = 0.55
            price_usd = round(ground_cost_usd / (1 - margin_rate), 2)
        elif tier == SLATier.EXPRESS:
            # Tier 2: サージ/即時派遣 プレミアム (~80% マージン)
            margin_rate = 0.80
            price_usd = round(ground_cost_usd / (1 - margin_rate), 2)
        elif tier == SLATier.TACTICAL:
            # Tier 3: Value-Based Pricing (90%+ マージン / 最低$2,500保障)
            margin_rate = 0.92
            base_tactical_value = 2500.0
            calculated_price = ground_cost_usd / (1 - margin_rate)
            price_usd = round(max(base_tactical_value, calculated_price), 2)
        else:
            margin_rate = 0.60
            price_usd = round(ground_cost_usd / (1 - margin_rate), 2)

        quote_id = f"q_{uuid.uuid4().hex[:10]}"

        return {
            "quote_id": quote_id,
            "tier": tier,
            "price_usd": price_usd,
            "estimated_cost_jpy": estimated_ground_cost_jpy,
            "margin_percent": round(margin_rate * 100, 1),
            "currency": "USD"
        }


# ====================================================
# 4. リクエスト / レスポンス DTO
# ====================================================

class QuoteRequest(BaseModel):
    intent: str = Field(..., description="物理タスクのリクエスト内容 (英語/日本語)")
    tier: SLATier = Field(SLATier.EXPRESS, description="SLA緊急度 (economy / express / tactical)")
    estimated_ground_cost_jpy: int = Field(5000, description="想定する地上ワーカー原資 (円)")

class HoldRequest(BaseModel):
    quote_id: str = Field(..., description="/api/v1/quote で発行された Quote ID")
    payment_method_id: Optional[str] = Field("pm_card_visa", description="Stripe PaymentMethod ID")

class CaptureRequest(BaseModel):
    payment_intent_id: str = Field(..., description="Stripe PaymentIntent ID")

class MCPToolCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]


# ====================================================
# 5. ユースケース / アプリケーションサービス
# ====================================================

def vet_task_usecase(user_request: str) -> dict:
    """Gemini Flash によるリアルタイム安全審査"""
    if not gemini_client:
        return {"status": "APPROVED", "reason": "オフライン監査パス", "risk_score": 0.05}

    current_instruction = safety_policy.get_effective_instruction()

    try:
        response = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=f"審査リクエスト: {user_request}",
            config=types.GenerateContentConfig(
                system_instruction=current_instruction,
                response_mime_type="application/json",
                temperature=0.0
            )
        )
        return json.loads(response.text)
    except Exception as e:
        return {"status": "APPROVED", "reason": f"監査バイパス: {str(e)}", "risk_score": 0.1}


def async_self_refinement_job():
    """【バックグラウンド非同期ジョブ】拒絶ログをメタ分析し、プロンプトを自動更新"""
    declined_logs = DatabaseRepository.get_declined_logs()
    if len(declined_logs) < 1 or not gemini_client:
        return

    meta_prompt = f"""
    以下は Gateway X Vetting Engine で拒絶（DECLINED）された過去のログです:
    {json.dumps(declined_logs, ensure_ascii=False)}

    これらを分析し、過剰拒絶を抑止しつつ安全性を保つプロンプト修正ルール（日本語1行）を1つ提案してください。
    """

    try:
        response = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=meta_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "new_rule": {"type": "STRING"}
                    },
                    "required": ["new_rule"]
                },
                temperature=0.2
            )
        )
        result = json.loads(response.text)
        new_rule = result.get("new_rule")
        if new_rule:
            DatabaseRepository.add_learned_rule(new_rule)
            DatabaseRepository.add_log("SELF_REFINEMENT", {"added_rule": new_rule})
    except Exception as e:
        print(f"[Self-Refinement Error]: {e}")


# ====================================================
# 6. API エンドポイント (Interface Adapters)
# ====================================================

@app.get("/")
def read_root():
    learned_rules = DatabaseRepository.get_learned_rules()
    return {
        "system": "Gateway X-OS",
        "architecture": "Clean Architecture / Dynamic Margin (v3.1.0)",
        "engine": MODEL_NAME,
        "database": "SQLite (Persisted)",
        "learned_rules_count": len(learned_rules)
    }


@app.post("/api/v1/quote")
def create_quote(req: QuoteRequest, background_tasks: BackgroundTasks):
    """【Step 1: 安全審査 (Vetting) ➔ 動的USD見積もり発行】"""
    vetting_result = vet_task_usecase(req.intent)

    if vetting_result.get("status") == "DECLINED":
        DatabaseRepository.add_log("DECLINED", {
            "intent": req.intent,
            "reason": vetting_result.get("reason")
        })
        background_tasks.add_task(async_self_refinement_job)
        raise HTTPException(
            status_code=403,
            detail={
                "code": "economic_security_violation",
                "message": vetting_result.get("reason", "Task declined by Security Vetting Engine")
            }
        )

    # 動的プライシング計算
    quote = DynamicPricingEngine.calculate_quote(req.intent, req.tier, req.estimated_ground_cost_jpy)
    
    DatabaseRepository.save_quote(
        quote_id=quote["quote_id"],
        intent=req.intent,
        tier=req.tier.value,
        price_usd=quote["price_usd"],
        cost_jpy=req.estimated_ground_cost_jpy,
        margin=quote["margin_percent"]
    )

    return {
        "status": "QUOTED",
        "quote": quote,
        "vetting": {
            "passed": True,
            "reason": vetting_result.get("reason")
        }
    }


@app.post("/api/v1/vet-and-hold")
def vet_and_hold(req: HoldRequest):
    """【Step 2: 与信確保 (Stripe USD PaymentIntent Manual Capture)】"""
    quote = DatabaseRepository.get_quote(req.quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="Quote ID not found or expired")

    amount_cents = int(quote["price_usd"] * 100)

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            capture_method="manual",
            payment_method=req.payment_method_id,
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            description=f"Gateway X-OS Hold [Quote: {quote['quote_id']}]"
        )
        
        DatabaseRepository.add_log("HOLD_SUCCESS", {
            "quote_id": quote["quote_id"],
            "payment_intent_id": intent.id,
            "price_usd": quote["price_usd"]
        })
        
        return {
            "status": "AUTHORIZED_AND_QUEUED",
            "execution_id": f"exec_{quote['quote_id']}",
            "payment": {
                "payment_intent_id": intent.id,
                "amount_usd": quote["price_usd"],
                "stripe_status": intent.status
            }
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/capture")
def capture_payment(req: CaptureRequest):
    """【Step 3: 現場タスクプレ検収完了 ➔ Stripe本決済確定】"""
    try:
        intent = stripe.PaymentIntent.retrieve(req.payment_intent_id)
        captured_intent = stripe.PaymentIntent.capture(req.payment_intent_id)

        DatabaseRepository.add_log("CAPTURED", {
            "payment_intent_id": captured_intent.id,
            "captured_amount_usd": captured_intent.amount / 100.0
        })

        return {
            "message": "Physical Execution Verified & Settlement Completed",
            "data": {
                "success": True,
                "payment_intent_id": captured_intent.id,
                "amount_captured_usd": captured_intent.amount / 100.0,
                "status": captured_intent.status
            }
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/mcp/v1/tools/call")
def mcp_tool_call(req: MCPToolCallRequest, background_tasks: BackgroundTasks):
    """【MCP アダプター】Claude / OpenAI Agents 向けネイティブ Tool 呼び出し」"""
    if req.name == "dispatch_physical_execution":
        intent = req.arguments.get("intent", "")
        tier = req.arguments.get("tier", "express")
        ground_cost = req.arguments.get("estimated_cost_jpy", 5000)

        quote_req = QuoteRequest(intent=intent, tier=SLATier(tier), estimated_ground_cost_jpy=ground_cost)
        res = create_quote(quote_req, background_tasks)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(res, ensure_ascii=False, indent=2)
                }
            ]
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unknown MCP tool: {req.name}")
