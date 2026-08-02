# Gateway X Physical API Specification (v3.2 Protocol)

**The Universal Bridge for Autonomous AI Agents to Physical World Execution in Japan.**

---

## 1. Executive Summary

Gateway X-OS is an automated API gateway middleware that allows autonomous AI agents (OpenAI, Claude/MCP, Quant Funds) to safely inspect, gather data, and execute physical tasks in Japan.
All requests undergo real-time Economic Security Vetting, Dynamic Pricing (USD-denominated, high-margin model), and automated two-phase settlement.

- **Base URL:** `https://gateway-x-os.onrender.com`
- **Protocol:** REST API / OpenAPI 3.1 & MCP (Model Context Protocol) Adapter
- **Default Currency:** USD ($)
- **SLA & Routing Tiers:**
  - `economy`: 24-hour SLA via aggregated ground worker network
  - `express`: Immediate dispatch via pre-pooled ground personnel
  - `tactical`: Specialized, NDA-signed elite team for high-value / sensitive field operations

---

## 2. Authentication & Rate Limits

- **Header:** `Authorization: Bearer <YOUR_GATEWAY_X_API_KEY>`
- **Content-Type:** `application/json`
- **Rate Limit:** 5 requests per hour per client ID (DoS / runaway agent protection)

---

## 3. Core API Endpoints

### 3.1 Call Physical Execution Tool (`dispatch_physical_execution`)

Dispatches a physical verification or execution task in Japan. Evaluated through the **Gemini Flash Vetting Engine** (Economic Security Act compliance) and **Naval-Collison Dynamic Pricing Engine**.

- **HTTP Method:** `POST`
- **Endpoint:** `/mcp/v1/tools/call`

#### Request Body (JSON)

```json
{
  "name": "dispatch_physical_execution",
  "arguments": {
    "intent": "Verify construction progress of new commercial complex in Shibuya, Tokyo.",
    "tier": "express",
    "estimated_cost_jpy": 5000,
    "client_id": "agent_quant_fund_01"
  }
}
