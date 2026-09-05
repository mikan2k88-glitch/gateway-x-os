import os
import secrets
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.db.repository import DatabaseRepository
from app.sales.sales import SalesRepository
from app.core.execution_repository import ExecutionRepository

security = HTTPBasic()

# アラート専用ビューに表示するイベント種別(通常のQUOTED/CAPTURED等の正常系は含めない)
ALERT_EVENT_TYPES = [
    "DECLINED",
    "PAYMENT_AUTH_FAILED",
    "PAYMENT_CAPTURE_FAILED",
    "PAYMENT_CANCELED",
    "CHARGEBACK_RECEIVED",
    "CHARGEBACK_NEEDS_MANUAL_REVIEW",
    "CHARGEBACK_EVIDENCE_SUBMITTED",
    "CHARGEBACK_UNRESOLVED",
]


def _check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    """
    モニターへのBasic認証チェック。
    MONITOR_PASSWORD環境変数が未設定の場合、誰でもアクセスできてしまうため
    安全側に倒してモニター自体を503で無効化する(認証をスキップして公開しない)。
    """
    expected_user = os.environ.get("MONITOR_USERNAME", "admin")
    expected_pass = os.environ.get("MONITOR_PASSWORD")
    if not expected_pass:
        raise HTTPException(
            status_code=503,
            detail=(
                "MONITOR_PASSWORD が未設定のため、モニターは無効化されています。"
                "Renderの環境変数に MONITOR_PASSWORD (任意でMONITOR_USERNAMEも) を設定してください。"
            ),
        )
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"}
        )


def create_monitor_router(
    db_repo: DatabaseRepository,
    sales_repo: SalesRepository,
    execution_repo: ExecutionRepository,
) -> APIRouter:
    """
    モニター用ルーターのファクトリ。main.py側で既存のorchestrator.db/sales_repo/execution_repo
    をそのまま渡すことで、他のエンドポイントと同じDB接続設定を共有する。
    """
    router = APIRouter()

    @router.get("/monitor/api/data")
    async def get_monitor_data(_: None = Depends(_check_auth)) -> Dict[str, Any]:
        quotes = await db_repo.get_recent_quotes(limit=50)
        dispatches = await execution_repo.get_recent_dispatches(limit=50)
        dispatch_by_quote = {d["quote_id"]: d for d in dispatches}

        orders = []
        for q in quotes:
            dispatch = dispatch_by_quote.get(q["quote_id"])
            orders.append({
                "quote_id": q["quote_id"],
                "client_id": q["client_id"],
                "intent": q["intent"],
                "tier": q["tier"],
                "price_usd": q["price_usd"],
                "quote_status": q["status"],
                "dispatch_status": dispatch["status"] if dispatch else None,
                "execution_id": dispatch["execution_id"] if dispatch else None,
                "created_at": q["created_at"],
            })

        strategy_cycles = await sales_repo.get_recent_cycles(limit=20)
        workers = await sales_repo.get_active_workers()
        alerts = await db_repo.get_recent_events(limit=50, event_types=ALERT_EVENT_TYPES)

        return {
            "orders": orders,
            "strategy_cycles": strategy_cycles,
            "workers": workers,
            "alerts": alerts,
        }

    @router.get("/monitor", response_class=HTMLResponse)
    async def get_monitor_dashboard(_: None = Depends(_check_auth)) -> str:
        return _DASHBOARD_HTML

    return router


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Gateway X-OS Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #262b36; --text: #e6e8ec;
    --muted: #8a90a0; --accent: #4f8cff; --ok: #35c47a; --warn: #e0a83a; --bad: #e05a4f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", "Hiragino Sans", sans-serif; font-size: 14px;
  }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  #status { color: var(--muted); font-size: 12px; }
  main { padding: 20px 24px; display: grid; gap: 20px; grid-template-columns: 1fr 1fr; }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  section.full { grid-column: 1 / -1; }
  h2 { font-size: 13px; margin: 0 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); font-size: 13px; }
  th { color: var(--muted); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  .badge { padding: 2px 8px; border-radius: 999px; font-size: 11px; white-space: nowrap; }
  .b-ok { background: rgba(53,196,122,0.15); color: var(--ok); }
  .b-warn { background: rgba(224,168,58,0.15); color: var(--warn); }
  .b-bad { background: rgba(224,90,79,0.15); color: var(--bad); }
  .b-muted { background: rgba(138,144,160,0.15); color: var(--muted); }
  .empty { color: var(--muted); padding: 8px 0; }
  .mono { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Gateway X-OS Monitor</h1>
  <span id="status">読み込み中...</span>
</header>
<main>
  <section>
    <h2>進行中の注文/タスク</h2>
    <div id="orders"></div>
  </section>
  <section>
    <h2>営業エンジン(戦略サイクル)</h2>
    <div id="cycles"></div>
  </section>
  <section>
    <h2>稼働中ワーカー</h2>
    <div id="workers"></div>
  </section>
  <section class="full">
    <h2>アラート(エラー/セキュリティ/決済失敗)</h2>
    <div id="alerts"></div>
  </section>
</main>
<script>
function badge(text, cls) { return '<span class="badge ' + cls + '">' + text + '</span>'; }

function orderBadge(o) {
  if (o.dispatch_status === 'COMPLETED') return badge('COMPLETED', 'b-ok');
  if (o.dispatch_status === 'FAILED' || o.dispatch_status === 'CAPTURE_FAILED') return badge(o.dispatch_status, 'b-bad');
  if (o.dispatch_status === 'DISPATCHED') return badge('DISPATCHED', 'b-warn');
  if (o.quote_status === 'QUOTED') return badge('QUOTED (未決済)', 'b-muted');
  return badge(o.quote_status || '-', 'b-muted');
}

function cycleBadge(status) {
  if (status === 'approved') return badge('承認', 'b-ok');
  if (status === 'rejected') return badge('却下', 'b-bad');
  if (status === 'pending') return badge('保留', 'b-warn');
  return badge(status || 'debating', 'b-muted');
}

function alertBadge(type) {
  if (type && type.startsWith('CHARGEBACK')) return badge(type, 'b-bad');
  if (type === 'DECLINED') return badge(type, 'b-warn');
  return badge(type || '-', 'b-bad');
}

function renderTable(rows, cols, rowFn) {
  if (!rows || rows.length === 0) return '<div class="empty">データがありません</div>';
  var html = '<table><thead><tr>';
  cols.forEach(function(c) { html += '<th>' + c + '</th>'; });
  html += '</tr></thead><tbody>';
  rows.forEach(function(r) { html += '<tr>' + rowFn(r) + '</tr>'; });
  html += '</tbody></table>';
  return html;
}

async function refresh() {
  var statusEl = document.getElementById('status');
  try {
    var res = await fetch('/monitor/api/data', { credentials: 'same-origin' });
    if (!res.ok) { statusEl.textContent = 'エラー: HTTP ' + res.status; return; }
    var data = await res.json();

    document.getElementById('orders').innerHTML = renderTable(
      data.orders, ['状態', 'quote_id', 'client_id', '内容', 'tier', '$', '日時'],
      function(o) {
        return '<td>' + orderBadge(o) + '</td>' +
          '<td class="mono">' + o.quote_id + '</td>' +
          '<td>' + o.client_id + '</td>' +
          '<td>' + (o.intent || '').slice(0, 40) + '</td>' +
          '<td>' + o.tier + '</td>' +
          '<td>' + o.price_usd + '</td>' +
          '<td class="mono">' + o.created_at + '</td>';
      }
    );

    document.getElementById('cycles').innerHTML = renderTable(
      data.strategy_cycles, ['状態', 'ラウンド', '判定理由', '日時'],
      function(c) {
        return '<td>' + cycleBadge(c.cycle_status) + '</td>' +
          '<td>' + c.round_count + '</td>' +
          '<td>' + (c.executor_reason || '-').slice(0, 60) + '</td>' +
          '<td class="mono">' + c.updated_at + '</td>';
      }
    );

    document.getElementById('workers').innerHTML = renderTable(
      data.workers, ['ワーカー', '登録日'],
      function(w) {
        return '<td>' + (w.display_name || w.line_user_id) + '</td>' +
          '<td class="mono">' + w.registered_at + '</td>';
      }
    );

    document.getElementById('alerts').innerHTML = renderTable(
      data.alerts, ['種別', 'client_id', '詳細', '日時'],
      function(a) {
        return '<td>' + alertBadge(a.event_type) + '</td>' +
          '<td>' + a.client_id + '</td>' +
          '<td>' + (a.detail || '').slice(0, 80) + '</td>' +
          '<td class="mono">' + a.created_at + '</td>';
      }
    );

    statusEl.textContent = '最終更新: ' + new Date().toLocaleTimeString('ja-JP');
  } catch (e) {
    statusEl.textContent = '通信エラー: ' + e;
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
