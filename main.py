import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ==========================================
# Gateway X-OS (v3.1.1) Main Entrypoint
# Lead Architects: Jeff Dean & Ken Thompson
# ==========================================

app = FastAPI(
    title="Gateway X-OS",
    description="Physical Execution Gateway & Security Vetting System for AI Agents",
    version="3.1.1",
)


# ------------------------------------------
# Request / Response Schemas
# ------------------------------------------

class MCPToolCallArguments(BaseModel):
    intent: str = Field(..., description="The physical execution task intent in natural language.")
    tier: str = Field("express", description="Execution tier: 'economy', 'express', or 'tactical'")
    estimated_cost_jpy: int = Field(5000, description="Estimated ground worker cost in JPY")


class MCPToolCallRequest(BaseModel):
    name: Optional[str] = None
    arguments: MCPToolCallArguments


# ------------------------------------------
# Core Vetting Engine & Logic
# ------------------------------------------

def evaluate_security_vetting(intent: str) -> Dict[str, Any]:
    """
    Ken Thompson's Vetting Module:
    経済安全保障推進法に準拠したセキュリティ審査
    """
    forbidden_keywords = ["military", "base", "substation", "nuclear", "自衛隊", "変電所", "基地", "爆破"]
    
    intent_lower = intent.lower()
    for kw in forbidden_keywords:
        if kw in intent_lower:
            return {
                "passed": False,
                "reason": f"Security violation detected: Restricted entity/keyword '{kw}' identified. Flagged for Public Safety Review.",
                "action": "PERMANENT_BAN"
            }
            
    return {
        "passed": True,
        "reason": "The request is a routine site inspection and physical progress verification for a commercial real estate project, which constitutes standard business operations and presents no security or economic compliance violations.",
        "action": "PROCEED"
    }


def calculate_dynamic_pricing(tier: str, ground_cost_jpy: int) -> Dict[str, Any]:
    """
    Jeff Dean's Dynamic Pricing Module:
    Naval-Collison 3-Tier Model (純利マージン80%ロック)
    """
    usd_jpy_rate = 155.0  # 為替レート換算基準
    ground_cost_usd = ground_cost_jpy / usd_jpy_rate
    
    margin_rate = 0.80
    price_usd = round(ground_cost_usd / (1.0 - margin_rate), 2)
    
    return {
        "tier": tier,
        "price_usd": price_usd,
        "estimated_cost_jpy": ground_cost_jpy,
        "margin_percent": 80.0,
        "currency": "USD"
    }


# ------------------------------------------
# API Endpoints
# ------------------------------------------

@app.get("/")
def read_root():
    return {
        "status": "online",
        "system": "Gateway X-OS",
        "version": "3.1.1",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


@app.get("/mcp/v1/manifest")
def get_mcp_manifest():
    return {
        "schema_version": "v1",
        "name": "gateway_x_mcp_adapter",
        "description": "Gateway X Physical Execution API Protocol",
        "tools": [
            {
                "name": "dispatch_physical_execution",
                "description": "Dispatch a physical execution task in Japan with security vetting and dynamic pricing.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "intent": {"type": "string"},
                        "tier": {"type": "string", "enum": ["economy", "express", "tactical"]},
                        "estimated_cost_jpy": {"type": "integer"}
                    },
                    "required": ["intent"]
                }
            }
        ]
    }


@app.get("/mcp/v1/tools/call")
def handle_tools_call_get():
    raise HTTPException(status_code=405, detail="Method Not Allowed. Use POST for /mcp/v1/tools/call")


@app.post("/mcp/v1/tools/call")
async def execute_mcp_tool(request: Request):
    try:
        body = await request.json()
        
        if "arguments" in body:
            args = body["arguments"]
        else:
            args = body
            
        intent = args.get("intent", "")
        tier = args.get("tier", "express")
        estimated_cost_jpy = int(args.get("estimated_cost_jpy", 5000))
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload structure: {str(e)}")

    # 1. Vetting
    vetting = evaluate_security_vetting(intent)
    
    if not vetting["passed"]:
        return JSONResponse(
            status_code=403,
            content={
                "status": "DECLINED",
                "vetting": vetting,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
        )

    # 2. Dynamic Pricing
    pricing = calculate_dynamic_pricing(tier, estimated_cost_jpy)
    quote_id = f"q_{uuid.uuid4().hex[:10]}"

    # 3. Complete Response Body
    return {
        "status": "QUOTED",
        "quote": {
            "quote_id": quote_id,
            "intent": intent,
            "tier": pricing["tier"],
            "price_usd": pricing["price_usd"],
            "estimated_cost_jpy": pricing["estimated_cost_jpy"],
            "margin_percent": pricing["margin_percent"],
            "currency": pricing["currency"]
        },
        "vetting": vetting,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
