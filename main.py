import os
import json
import sqlite3
import stripe
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ====================================================
# 1. 環境変数 & インフラストラクチャ初期化
# ====================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

stripe.api_key = STRIPE_SECRET_KEY
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="Gateway X-OS", version="3.0.0")

DB_FILE = "gateway.db"

# ====================================================
# 2. データベース永続化層 (Persistence Layer)
# ====================================================

def init_db():
    """データベーステーブルの初期化"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 成長ログテーブル
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


# ====================================================
# 3. ドメインエンティティ (Domain Layer)
# ====================================================

class DynamicSafetyPolicy:
    """【ドメインルール】永続化DBと同期する自己進化型安全審査ポリシー"""
    
    def __init__(self):
        self.base_instruction = """
        あなたは Gateway X-OS の厳格な金融・タスク安全監査AIです。
        以下の安全ガードレールを適用し、JSON形式で回答してください:
        1. レバレッジ取引、信用取引、空売り、違法・ハイリスク行為の要求 ➔ "DECLINED"
        2. 安全なビジネス・作業タスク、現物取引、正常な業務決済 ➔ "APPROVED"

        出力フォーマット(JSON):
        {"status": "APPROVED" | "DECLINED", "reason": "審査理由の詳細"}
        """

    def get_effective_instruction(self) -> str:
        learned_rules = DatabaseRepository.get_learned_rules()
        if not learned_rules:
            return self.base_instruction
        
        added_rules = "\n".join([f"- {rule}" for rule in learned_rules])
        return f"{self.base_instruction}\n\n【自動改善により獲得した追加基準】:\n{added_rules}"


safety_policy = DynamicSafetyPolicy()


# ====================================================
# 4. リクエスト／レスポンス DTO (Presentation Layer)
# ====================================================

class TaskRequest(BaseModel):
    user_request: str = Field(..., description="依頼するタスクの内容")
    amount_jpy: int = Field(..., description="仮払い金額（円）")

class CaptureRequest(BaseModel):
    payment_intent_id: str = Field(..., description="Stripe PaymentIntent ID")


# ====================================================
# 5. ユースケース / アプリケーションサービス
# ====================================================

def vet_task_usecase(user_request: str) -> dict:
    """Gemini 3.6 Flash による安全審査"""
    current_instruction = safety_policy.get_effective_instruction()

    response = gemini_client.models.generate_content(
        model=MODEL_NAME,
        contents=f"審査リクエスト内容: {user_request}",
        config=types.GenerateContentConfig(
            system_instruction=current_instruction,
            response_mime_type="application/json",
            temperature=0.0
        )
    )
    return json.loads(response.text)


def async_self_refinement_job():
    """【バックグラウンド非同期ジョブ】ログをメタ分析し、プロンプトを自動更新"""
    declined_logs = DatabaseRepository.get_declined_logs()
    
    # スロットリング：拒絶ログがある程度蓄積された場合のみ実行
    if len(declined_logs) < 1:
        return

    meta_prompt = f"""
    以下は、Gateway X-OS の安全審査エンジンで過去に「拒絶（DECLINED）」判定されたタスクのログです：
    {json.dumps(declined_logs, ensure_ascii=False)}

    これらを分析し、過剰な拒絶（誤検知）を抑えつつ、安全性を極限まで高めるための「プロンプトの微修正ルール（日本語1行）」を1つ提案してください。
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
            print(f"[Self-Refinement Success] New Rule Added: {new_rule}")

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
        "architecture": "Clean Architecture / DDD (v3.0.0)",
        "engine": MODEL_NAME,
        "database": "SQLite (Persisted)",
        "learned_rules_count": len(learned_rules)
    }


@app.post("/api/v1/vet-and-hold")
def vet_and_hold(req: TaskRequest, background_tasks: BackgroundTasks):
    """【1. 審査 ➔ 2. 仮払い（与信確保） ➔ 3. 裏で非同期自己学習】"""
    vetting_result = vet_task_usecase(req.user_request)

    if vetting_result.get("status") == "DECLINED":
        # 拒絶ログをDBへ登録
        DatabaseRepository.add_log("DECLINED", {
            "request": req.user_request,
            "reason": vetting_result.get("reason")
        })
        
        # ⚡ バックグラウンドで非同期に自己学習を発火（レスポンスを待たせない）
        background_tasks.add_task(async_self_refinement_job)

        return {
            "status": "DECLINED",
            "reason": vetting_result.get("reason"),
            "payment": None
        }

    # Stripe 仮払い
    try:
        intent = stripe.PaymentIntent.create(
            amount=req.amount_jpy,
            currency="jpy",
            capture_method="manual",
            payment_method="pm_card_visa",
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            description="Gateway X-OS タスク仮払い"
        )
        
        DatabaseRepository.add_log("APPROVED", {"request": req.user_request, "payment_intent_id": intent.id})
        
        return {
            "status": "APPROVED",
            "reason": vetting_result.get("reason"),
            "payment": {
                "payment_intent_id": intent.id,
                "stripe_status": intent.status
            }
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/capture")
def capture_payment(req: CaptureRequest):
    """【作業完了時：本決済 & 10%手数料徴収】"""
    try:
        intent = stripe.PaymentIntent.retrieve(req.payment_intent_id)
        total_amount = intent.amount
        platform_fee = int(total_amount * 0.10)
        net_payout = total_amount - platform_fee

        captured_intent = stripe.PaymentIntent.capture(req.payment_intent_id)

        DatabaseRepository.add_log("CAPTURED", {
            "payment_intent_id": captured_intent.id,
            "fee_earned": platform_fee
        })

        return {
            "message": "決済確定完了（10%手数料徴収済み）",
            "data": {
                "success": True,
                "payment_intent_id": captured_intent.id,
                "total_amount": total_amount,
                "platform_fee": platform_fee,
                "net_payout": net_payout,
                "status": captured_intent.status
            }
        }
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/growth-backlog")
def get_growth_backlog():
    """成長ログおよび永続化されたプロンプト状態の閲覧"""
    return {
        "current_effective_instruction": safety_policy.get_effective_instruction(),
        "learned_rules": DatabaseRepository.get_learned_rules()
    }
