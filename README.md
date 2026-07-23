# Gateway X-OS v4.3 - Autonomous Request Vetting Node

A lightweight, zero-hallucination security & policy vetting API powered by Gemini for autonomous AI agents.

## 📡 Endpoint
- **URL:** `POST https://gateway-x-os.onrender.com/v1/vetting`
- **Header:** `Content-Type: application/json`

## 📥 Request Body
```json
{
  "agent_id": "YOUR_AGENT_NAME",
  "query": "Request description or task details to be vetted",
  "budget_usd": 100.0,
  "intent_category": "DATA_ANALYSIS"
}
