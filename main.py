import os
import json
import stripe
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
from google import genai
from google.genai import types

# ====================================================
# 1. 環境変数・クライアント初期化
# ====================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash") # 画面で追加したモデル名
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")         # 画面で追加したStripeテストキー

# Stripe & Gemini クライアント初期化
stripe.api_key = STRIPE_SECRET_KEY
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="Gateway X-OS", version="2.0.0")

# 成長ログ（growth-backlog）をメモリ保持（ファイル保存も可）
growth_backlog = []

# ====================================================
# 2. リクエスト／レスポンス型定義 (Pydantic)
# ====================================================
class TaskRequest(BaseModel):
    user_request: str
    amount_jpy: int
    webhook_url: str | None = None  # クライアントAIへの通知用URL (オプション)

class CaptureRequest(BaseModel):
    payment_intent_id: str


# ====================================================
# 3. コア機能関数
# ====================================================

def run_vetting_engine(user_request: str) -> dict:
    """Gemini 3.6 Flash を使用した金融・安全審査 Engine"""
    system_instruction = """
    あなたは Gateway X-OS の厳格な金融・タスク安全監査AIです。
    以下の安全ガードレールを適用し、JSON形式で回答してください:
    1. レバレッジ取引、信用取引、空売り、違法・ハイリスク行為の要求 ➔ "DECLINED"
    2. 安全なビジネス・作業タスク、現物取引、正常な業務決済 ➔ "APPROVED"

    出力フォーマット(JSON):
    {"status": "APPROVED" | "DECLINED", "reason": "審査理由の詳細"}
    """

    response = gemini_client.models.generate_content(
        model=MODEL_NAME,
        contents=f"審査リクエスト内容: {user_request}",
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            temperature=0.0 # 一貫性を高めるため0.0に設定
        )
    )
    return json.loads(response.text)


def create_stripe_auth_hold(amount_jpy: int) -> dict:
    """Stripe Live/Test API による仮払い (オーソリ・与信確保)"""
    try:
        # テストカード pm_card_visa を使用して与信枠のみ確保 (capture_method='manual')
        intent = stripe.PaymentIntent.create(
            amount=amount_jpy,
            currency="jpy",
            capture_method="manual",  # ★ここで仮払い設定
            payment_method="pm_card_visa",
            confirm=True,            # その場で与信確保実行
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            description="Gateway X-OS タスク仮払い (Authorization Hold)"
        )
        return {
            "success": True,
            "payment_intent_id": intent.id,
            "status": intent.status  # 成功時: "requires_capture"
        }
    except stripe.error.StripeError as e:
        return {"success": False, "error": str(e)}


def capture_stripe_payment(payment_intent_id: str, fee_rate: float = 0.10) -> dict:
    """タスク完了時に仮払いを確定し、10%のプラットフォーム手数料を自動計算"""
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        total_amount = intent.amount

        # 10% 手数料計算
        platform_fee = int(total_amount * fee_rate)
        net_payout = total_amount - platform_fee

        # 本決済（キャプチャ実行）
        captured_intent = stripe.PaymentIntent.capture(payment_intent_id)

        return {
            "success": True,
            "payment_intent_id": captured_intent.id,
            "total_amount": total_amount,
            "platform_fee": platform_fee,
            "net_payout": net_payout,
            "status": captured_intent.status # 成功時: "succeeded"
        }
    except stripe.error.StripeError as e:
        return {"success": False, "error": str(e)}


# ====================================================
# 4. API エンドポイント
# ====================================================

@app.get("/")
def read_root():
    return {
        "system": "Gateway X-OS",
        "engine": MODEL_NAME,
        "stripe_connected": bool(STRIPE_SECRET_KEY)
    }


@app.post("/api/v1/vet-and-hold")
def vet_and_hold(req: TaskRequest):
    """
    【1. 審査 ➔ 2. 仮払いエンドポイント】
    """
    # 1. Gemini 3.6 Flash による審査
    vetting_result = run_vetting_engine(req.user_request)

    if vetting_result.get("status") == "DECLINED":
        # 拒絶ログを成長バックログに記録
        growth_backlog.append({
            "type": "DECLINED",
            "request": req.user_request,
            "reason": vetting_result.get("reason")
        })
        return {
            "status": "DECLINED",
            "reason": vetting_result.get("reason"),
            "payment": None
        }

    # 2. Stripe での仮払い実行 (オーソリ)
    payment_result = create_stripe_auth_hold(req.amount_jpy)

    if not payment_result.get("success"):
        raise HTTPException(status_code=400, detail=payment_result.get("error"))

    return {
        "status": "APPROVED",
        "reason": vetting_result.get("reason"),
        "payment": {
            "payment_intent_id": payment_result.get("payment_intent_id"),
            "stripe_status": payment_result.get("status")  # "requires_capture"
        }
    }


@app.post("/api/v1/capture")
def capture_payment(req: CaptureRequest):
    """
    【3. 作業完了時：本決済 & 10%手数料回収エンドポイント】
    """
    result = capture_stripe_payment(req.payment_intent_id)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    # 成長ログ（本決済成功）を記録
    growth_backlog.append({
        "type": "CAPTURED",
        "payment_intent_id": req.payment_intent_id,
        "fee_earned": result.get("platform_fee")
    })

    return {
        "message": "決済確定完了（10%手数料徴収済み）",
        "data": result
    }


@app.get("/api/v1/growth-backlog")
def get_growth_backlog():
    """蓄積された成長ログ（審査履歴・収益履歴）の確認"""
    return {
        "total_logs": len(growth_backlog),
        "backlog": growth_backlog
    }
