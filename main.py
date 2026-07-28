import json
import uuid
from datetime import datetime

# ==========================================
# Gateway X-OS (v3.1.1) Pipeline Engine
# Lead Architect: Jeff Dean
# ==========================================

class GatewayXPipeline:
    def __init__(self, quote_data: dict):
        self.quote = quote_data
        self.execution_id = f"exec_{uuid.uuid4().hex[:10]}"
        self.status = "INITIALIZED"

    def step_1_authorize_payment(self) -> bool:
        """Phase A: Stripe USD 2-Phase Settlement (与信仮押さえ)"""
        print(f"\n[層④ 金融決済] Phase A: Auth (与信仮押さえ) 開始...")
        print(f" └ 対象Quote ID : {self.quote['quote_id']}")
        print(f" └ 請求金額 : ${self.quote['price_usd']} {self.quote['currency']}")
        
        # 与信枠確保の模擬処理
        self.status = "PAYMENT_AUTHORIZED"
        print(f" [SUCCESS] クレジットカード与信を確保しました (Status: {self.status})")
        return True

    def step_2_route_physical_execution(self) -> dict:
        """層⑤ 現場物理実行層への動的ルーティング"""
        tier = self.quote['tier']
        intent = self.quote['intent']
        print(f"\n[現場実行層] 物理タスクをルーティング中 (Tier: {tier})...")
        
        if tier == "express":
            target_org = "既存インフラハック (タイミー等 SLA制御便)"
            agent_role = "自社AI『優しい現場監督』"
        elif tier == "tactical":
            target_org = "Gateway X - Tactical Force (元プロ将校・精鋭部隊)"
            agent_role = "暗号化通信＆100%セキュア現地捜査"
        else:
            target_org = "Economy Service (24時間猶予便)"
            agent_role = "標準ガイドラインナビ"

        print(f" └ アサイン先 : {target_org}")
        print(f" └ サポートAI  : {agent_role}")
        print(f" └ 執行内容   : '{intent}'")
        
        return {
            "assigned_to": target_org,
            "field_status": "PRE_INSPECTED_PASSED", # プレ検収完了
            "evidence_photos": ["site_photo_01.jpg", "site_photo_02.jpg"]
        }

    def step_3_capture_payment(self, execution_result: dict) -> dict:
        """Phase B: Capture（売上確定 & 83%マージンロック）"""
        print(f"\n[層④ 金融決済] Phase B: Capture (売上確定) 開始...")
        if execution_result["field_status"] == "PRE_INSPECTED_PASSED":
            gross_usd = self.quote['price_usd']
            margin_rate = self.quote['margin_percent'] / 100
            
            net_profit_usd = round(gross_usd * margin_rate, 2)
            ground_payout_jpy = self.quote['estimated_cost_jpy']

            self.status = "COMPLETED"
            print(f" [SUCCESS] 現場プレ検収をパスしました。")
            print(f" [収益確定] 売上: ${gross_usd} | 純利益 (純利マージン80%): ${net_profit_usd}")
            print(f" [地上支払] ワーカー原資: ¥{ground_payout_jpy}")
            
            return {
                "execution_id": self.execution_id,
                "status": "SUCCESS",
                "revenue_captured_usd": gross_usd,
                "net_profit_usd": net_profit_usd,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }

# --- テスト実行 ---
if __name__ == "__main__":
    # 画像のレスポンスデータをそのまま注入
    sample_quote_from_api = {
        "quote_id": "q_b4fa228f72",
        "intent": "Verify construction progress of new commercial complex in Shibuya",
        "tier": "express",
        "price_usd": 161.29,
        "estimated_cost_jpy": 5000,
        "margin_percent": 80.0,
        "currency": "USD"
    }

    print("==================================================")
    print(" GATEWAY X-OS: Physical Execution E2E Test")
    print("==================================================")

    pipeline = GatewayXPipeline(sample_quote_from_api)
    
    # 1. 与信確保
    if pipeline.step_1_authorize_payment():
        # 2. 現場ルーティング＆実行
        exec_res = pipeline.step_2_route_physical_execution()
        # 3. 売上確定
        final_result = pipeline.step_3_capture_payment(exec_res)
        
        print("\n--- 最終出力レスポンス (Client AIへ返却) ---")
        print(json.dumps(final_result, indent=2, ensure_ascii=False))
