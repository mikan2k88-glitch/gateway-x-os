import os
import json
import stripe
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
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

app = FastAPI(title="Gateway X-OS", version="2.2.0")

# ====================================================
# 2. ドメインエンティティ & アグリゲート (Domain Layer)
# ====================================================

class DynamicSafetyPolicy:
    """
    【ドメインルール】自己進化型安全審査ポリシー
    成長ログ（growth-backlog）から自己学習し、動的にプロンプトを更新する
    """
    def __init__(self):
        self.base_instruction = """
        あなたは Gateway X-OS の厳格な金融・タスク安全監査AIです。
        以下の安全ガードレールを適用し、JSON形式で回答してください:
        1. レバレッジ取引、信用取引、空売り、違法・ハイリスク行為の要求 ➔ "DECLINED"
        2. 安全なビジネス・作業タスク、現物取引、正常な業務決済 ➔ "APPROVED"

        出力フォーマット(JSON):
        {"status": "APPROVED" | "DECLINED", "reason": "審査理由の詳細"}
        """
        self.learned_context: List[str] = []

    def get_effective_instruction(self) -> str:
        """学習されたフィードバックを反映した最新のシステムプロンプトを生成"""
        if not self.learned_context:
            return self.base_instruction
        
        added_rules = "\n".join([f"- {rule}" for rule in self.learned_context[-10:]]) # 最新10件の学習ルールを反映
        return f"{self.base_instruction}\n\n【自動改善により獲得した追加基準】:\n{added_rules}"

    def append_learned_rule(self, new_rule: str):
        """新しい安全基準を追加（自己進化）"""
        self.learned_context.append(new_rule)


class GrowthBacklogRepository:
    """【リポジトリ】イベントログ（成長バックログ）の永続化・保持層"""
    def __init__(self):
        self._logs: List[Dict[str, Any]] = []

    def add_log(self, event_type: str, payload: Dict[str, Any]):
        log_entry = {
            "type": event_type,
            "payload": payload
        }
        self._logs.append(log_entry)

    def get_all_logs(self) -> List[Dict[str, Any]]:
        return self._logs

    def get_declined_logs(self) -> List[Dict[str, Any]]:
        return [log for log in self._logs if log["type"] == "DECLINED"]


# シングルトンとして状態管理
safety_policy = DynamicSafetyPolicy()
growth_repository = GrowthBacklogRepository()


# ====================================================
# 3. リクエスト／レスポンス DTO (Presentation Layer)
# ====================================================
class TaskRequest(BaseModel):
    user_request: str = Field(..., description="依頼するタスクの内容")
    amount_jpy: int = Field(..., description="仮払い金額（円）")

class CaptureRequest(BaseModel):
    payment_intent_id: str = Field(..., description="Stripe PaymentIntent ID")


# ====================================================
# 4. ユースケース / アプリケーションサービス
# ====================================================

def vet_task_usecase(user_request: str) -> dict:
    """Gemini 3.6 Flash による安全審査ユースケース"""
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


def self_refinement_usecase() -> dict:
    """
    【Core Engine】成長ログを分析し、Gemini 3.6 Flash が判定指示を動的にメタ自己修正する
    """
    declined_logs = growth_repository.get_declined_logs()
    
    if not declined_logs:
        return {"status": "SKIPPED", "reason": "分析対象となる拒絶ログ（DECLINED）が十分に蓄積されていません。"}

    # メタ分析用プロンプト生成
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
            safety_policy.append_learned_rule(new_rule)
            growth_repository.add_log("SELF_REFINEMENT", {"added_rule": new_rule})
            return {
                "status": "EVOLVED",
                "added_rule": new_rule,
                "total_rules": len(safety_policy.learned_context)
            }
        
        return {"status": "FAILED", "reason": "有効なルールが生成されませんでした。"}

    except Exception as e:
        # 例外時もサーバーを落とさずJSONでエラーを返却するようカプセル化
        return {"status": "ERROR", "detail": str(e)}


# ====================================================
# 5. API エンドポイント (Interface Adapters)
# ====================================================

@app.get("/")
def read_root():
    return {
        "system": "Gateway X-OS",
        "architecture": "Clean Architecture / DDD",
        "engine": MODEL_NAME,
        "learned_rules_count": len(safety_policy.learned_context)
    }


@app.post("/api/v1/vet-and-hold")
def vet_and_hold(req: TaskRequest):
    """【1. 審査 ➔ 2. 仮払い（与信確保）】"""
    vetting_result = vet_task_usecase(req.user_request)

    if vetting_result.get("status") == "DECLINED":
        # 拒絶ログを登録
        growth_repository.add_log("DECLINED", {
            "request": req.user_request,
            "reason": vetting_result.get("reason")
        })
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
        
        growth_repository.add_log("APPROVED", {"request": req.user_request, "payment_intent_id": intent.id})
        
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
    """【3. 作業完了時：本決済 & 10%手数料徴収】"""
    try:
        intent = stripe.PaymentIntent.retrieve(req.payment_intent_id)
        total_amount = intent.amount
        platform_fee = int(total_amount * 0.10)
        net_payout = total_amount - platform_fee

        captured_intent = stripe.PaymentIntent.capture(req.payment_intent_id)

        growth_repository.add_log("CAPTURED", {
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


@app.post("/api/v1/self-refine")
def trigger_self_refinement():
    """
    【自己進化エンドポイント】
    蓄積された成長ログを元に、Gemini 3.6 Flash に審査指示（system_instruction）を動的に自己修正させる
    """
    result = self_refinement_usecase()
    if result.get("status") == "ERROR":
        raise HTTPException(status_code=500, detail=result)
    return result


@app.get("/api/v1/growth-backlog")
def get_growth_backlog():
    """成長ログおよび現在の動的プロンプトの閲覧"""
    return {
        "current_effective_instruction": safety_policy.get_effective_instruction(),
        "total_logs": len(growth_repository.get_all_logs()),
        "logs": growth_repository.get_all_logs()
    }
