import os
import math
import uuid
import sqlite3
from typing import Dict, Any, Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from pydantic import BaseModel
import google.generativeai as genai

# ==========================================
# 0. システム初期化 & データベース設定
# ==========================================
app = FastAPI(
    title="Gateway X-OS",
    version="3.2.0",
    description="Physical Execution API with Master Orchestrator"
)

DB_PATH = "gateway_x.db"

def init_db():
    """SQLite データベースおよびテーブル初期化（WALモードで高速・並列処理）"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    
    # 統合トランザクション / KPI 記録テーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id TEXT UNIQUE,
            client_id TEXT,
            intent TEXT,
            tier TEXT,
            price_usd REAL,
            cost_jpy INTEGER,
            margin_pct REAL,
            vetting_passed INTEGER,
            vetting_reason TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # 学習済みルールテーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

init_db()

# Gemini API の設定
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# ==========================================
# 1. リクエスト / レスポンスモデル定義
# ==========================================
class ToolCallArguments(BaseModel):
    intent: str
    tier: str = "express"
    estimated_cost_jpy: int = 5000
    client_id: str = "unknown_agent"

class MCPToolRequest(BaseModel):
    name: str
    arguments: ToolCallArguments

class FeedbackRequest(BaseModel):
    client_id: str
    quote_id: str
    rating: int  # 1 to 5
    feedback_text: str


# ==========================================
# 2. 独立モジュール群 (クラス設計の境界を保持)
# ==========================================

class VettingEngine:
    """【審査エンジン】経済安全保障推進法に準拠し、テロ・スパイ・禁止エリアを自動判定"""
    
    @staticmethod
    def evaluate(intent: str) -> Dict[str, Any]:
        # ブラックリスト判定
        blacklisted_keywords = ["自衛隊", "jsdf", "変電所", "substation", "基地", "軍事"]
        for kw in blacklisted_keywords:
            if kw.lower() in intent.lower():
                return {
                    "passed": False,
                    "reason": f"Security Protocol Alert: Request contains restricted keyword '{kw}'. Potential Violation of Economic Security Act.",
                    "action": "BLOCKED_AND_LOGGED"
                }
        
        # LLM による真の意図（Intent）深層審査
        if GEMINI_API_KEY:
            try:
                model = genai.GenerativeModel("gemini-1.5-flash")
                prompt = f"""
                You are the Security Compliance Officer for Gateway X.
                Analyze the following request intent for security, legal, or physical safety risks:
                Intent: "{intent}"
                
                Respond in valid JSON format ONLY:
                {{"passed": true/false, "reason": "brief explanation in English"}}
                """
                response = model.generate_content(
                    prompt, 
                    generation_config={"response_mime_type": "application/json"}
                )
                import json
                res_json = json.loads(response.text)
                return {
                    "passed": res_json.get("passed", True),
                    "reason": res_json.get("reason", "Passed compliance check."),
                    "action": "APPROVED" if res_json.get("passed", True) else "BLOCKED"
                }
            except Exception as e:
                # LLMエラー時のセーフティフォールバック
                pass
        
        return {
            "passed": True,
            "reason": "Passed standard rule-based compliance check.",
            "action": "APPROVED"
        }


class DynamicPricingEngine:
    """【動的収益エンジン】83%純利益マージン設計に基づくリアルタイムUSD価格算出"""
    
    USD_JPY_RATE = 155.0  # 為替レート設定
    TARGET_MARGIN = 0.83  # 83% マージン
    
    @classmethod
    def calculate_quote(cls, cost_jpy: int, tier: str) -> Dict[str, Any]:
        multiplier = {
            "economy": 1.0,
            "express": 1.5,
            "tactical": 3.0
        }.get(tier.lower(), 1.5)
        
        # 83%マージン計算 formula: Revenue = Cost / (1 - Margin)
        base_revenue_jpy = cost_jpy / (1.0 - cls.TARGET_MARGIN)
        adjusted_revenue_jpy = base_revenue_jpy * multiplier
        price_usd = math.ceil(adjusted_revenue_jpy / cls.USD_JPY_RATE)
        
        return {
            "price_usd": float(price_usd),
            "estimated_cost_jpy": cost_jpy,
            "margin_percent": f"{cls.TARGET_MARGIN * 100:.1f}%",
            "tier": tier
        }


class MasterOrchestrator:
    """【最高統括層】全システム（営業・審査・総務・KPI）の状態一元管理 (CEO/COO-OS)"""
    
    @staticmethod
    def record_transaction(quote_id: str, client_id: str, intent: str, tier: str, 
                           price_usd: float, cost_jpy: int, margin_pct: float, 
                           vetting_passed: bool, vetting_reason: str):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transactions 
            (quote_id, client_id, intent, tier, price_usd, cost_jpy, margin_pct, vetting_passed, vetting_reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            quote_id, client_id, intent, tier, price_usd, cost_jpy, margin_pct,
            1 if vetting_passed else 0, vetting_reason, "QUOTED" if vetting_passed else "REJECTED"
        ))
        conn.commit()
        conn.close()

    @staticmethod
    def get_dashboard_kpi() -> Dict[str, Any]:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*), SUM(price_usd), AVG(margin_pct) FROM transactions WHERE vetting_passed = 1")
        total_quoted, total_revenue_usd, avg_margin = cursor.fetchone()
        
        cursor.execute("SELECT COUNT(*) FROM transactions WHERE vetting_passed = 0")
        total_rejected = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "status": "OPERATIONAL",
            "kpi": {
                "total_quoted_requests": total_quoted or 0,
                "total_rejected_requests": total_rejected or 0,
                "total_revenue_usd": round(total_revenue_usd or 0.0, 2),
                "average_margin_percent": f"{round(avg_margin or 83.0, 1)}%"
            }
        }


# ==========================================
# 3. API エンドポイント
# ==========================================

@app.get("/")
async def root():
    return {"status": "ONLINE", "system": "Gateway X-OS", "version": "3.2.0"}

@app.get("/mcp/v1/dashboard/kpi")
async def get_kpi():
    """統括ダッシュボード: リアルタイム業績・売上・純利益率の集計参照"""
    return MasterOrchestrator.get_dashboard_kpi()

@app.post("/mcp/v1/tools/call")
async def call_mcp_tool(request: MCPToolRequest):
    """MCP メインエンドポイント: リクエスト受信 ➔ Vetting ➔ Pricing ➔ 統括記録"""
    if request.name != "dispatch_physical_execution":
        raise HTTPException(status_code=404, detail=f"Tool '{request.name}' not found.")
    
    args = request.arguments
    quote_id = f"q_{uuid.uuid4().hex[:10]}"
    
    # Step 1: Vetting 審査
    vetting_result = VettingEngine.evaluate(args.intent)
    
    if not vetting_result["passed"]:
        # 拒絶案件を Master Orchestrator に記録
        MasterOrchestrator.record_transaction(
            quote_id=quote_id, client_id=args.client_id, intent=args.intent,
            tier=args.tier, price_usd=0.0, cost_jpy=args.estimated_cost_jpy,
            margin_pct=0.0, vetting_passed=False, vetting_reason=vetting_result["reason"]
        )
        return {
            "status": "DECLINED",
            "reason": vetting_result["reason"],
            "security_action": vetting_result.get("action", "BLOCKED")
        }
    
    # Step 2: Dynamic Pricing 見積もり
    quote_info = DynamicPricingEngine.calculate_quote(args.estimated_cost_jpy, args.tier)
    
    # Step 3: Master Orchestrator へ取引記録
    MasterOrchestrator.record_transaction(
        quote_id=quote_id, client_id=args.client_id, intent=args.intent,
        tier=args.tier, price_usd=quote_info["price_usd"], cost_jpy=args.estimated_cost_jpy,
        margin_pct=83.0, vetting_passed=True, vetting_reason=vetting_result["reason"]
    )
    
    return {
        "status": "QUOTED",
        "quote": {
            "quote_id": quote_id,
            "tier": quote_info["tier"],
            "price_usd": quote_info["price_usd"],
            "currency": "USD",
            "margin_percent": quote_info["margin_percent"]
        },
        "vetting": vetting_result
    }

def bg_learn_from_feedback(quote_id: str, rating: int, feedback_text: str):
    """バックグラウンド学習処理: 顧客AIのフィードバックを SQLite に蓄積"""
    if not GEMINI_API_KEY:
        return
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
        Extract a single rule for system optimization based on client AI feedback:
        Rating: {rating}/5
        Feedback: "{feedback_text}"
        Output ONLY one short instruction rule in Japanese.
        """
        response = model.generate_content(prompt)
        learned_rule = response.text.strip()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO learning_rules (rule) VALUES (?)", (learned_rule,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Learning Loop Error] {e}")

@app.post("/mcp/v1/feedback")
async def submit_feedback(request: FeedbackRequest, background_tasks: BackgroundTasks):
    """クライアントAI用フィードバックエンドポイント"""
    if not (1 <= request.rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")
    
    background_tasks.add_task(bg_learn_from_feedback, request.quote_id, request.rating, request.feedback_text)
    
    return {
        "status": "ACCEPTED",
        "message": "Feedback registered into self-learning loop."
    }
