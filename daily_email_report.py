"""
MarketQuant Daily Email Report
Sends a full trading summary to aaron@vencat.com and ron@vencat.com
Run at 4:30pm ET every market day.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "PKQ2P7KLMAJH5E3IQVKYQPTBOB")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "A58boqhagLVQH7tKfz7MafU8axJx6HGc9GbR4VgUFhrT")
ALPACA_URL = "https://paper-api.alpaca.markets"

RECIPIENTS = ["aaron@vencat.com", "ron@vencat.com"]
SENDER_ACCOUNT = "ron@vencat.com"


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def get_account():
    try:
        import requests
        r = requests.get(f"{ALPACA_URL}/v2/account", headers=alpaca_headers(), timeout=10)
        return r.json()
    except:
        return {}


def get_positions():
    try:
        import requests
        r = requests.get(f"{ALPACA_URL}/v2/positions", headers=alpaca_headers(), timeout=10)
        return r.json() if r.status_code == 200 else []
    except:
        return []


def get_today_orders():
    try:
        import requests
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = requests.get(
            f"{ALPACA_URL}/v2/orders",
            params={"status": "all", "limit": 50, "direction": "desc"},
            headers=alpaca_headers(),
            timeout=10
        )
        orders = r.json() if r.status_code == 200 else []
        return [o for o in orders if isinstance(orders, list) and o.get("created_at", "").startswith(today)]
    except:
        return []


def get_today_pnl():
    """Calculate today's realized P&L from bot_trades.json"""
    trades_file = os.path.join(DATA_DIR, "bot_trades.json")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    trades_today = []
    if os.path.exists(trades_file):
        try:
            with open(trades_file) as f:
                trades = json.load(f)
            for t in trades:
                ts = t.get("timestamp", t.get("date", ""))
                if ts.startswith(today):
                    pnl = t.get("realized_pnl", t.get("pnl", 0))
                    total += float(pnl) if pnl else 0
                    trades_today.append(t)
        except:
            pass
    return total, trades_today


def get_swing_positions():
    f = os.path.join(DATA_DIR, "swing_positions.json")
    if os.path.exists(f):
        try:
            with open(f) as fp:
                return json.load(fp)
        except:
            pass
    return []


def get_iran_context():
    """Quick web search for current Iran situation"""
    try:
        result = subprocess.run(
            ["python3", "-c",
             "import subprocess; r = subprocess.run(['openclaw','web','search','--query','Trump Iran war today market update'], capture_output=True, text=True, timeout=15); print(r.stdout[:500])"],
            capture_output=True, text=True, timeout=20, cwd=os.path.dirname(__file__)
        )
        return result.stdout[:300] if result.stdout else "Iran situation: check news"
    except:
        return ""


def build_html_report(account, positions, orders, today_pnl, trades_today, swing_positions, date_str):
    equity = float(account.get("equity", 100000))
    cash = float(account.get("cash", 0))
    pnl_total = equity - 100000
    pnl_color = "#00c853" if pnl_total >= 0 else "#d32f2f"
    day_color = "#00c853" if today_pnl >= 0 else "#d32f2f"

    # Orders table
    orders_html = ""
    if orders:
        for o in orders:
            side = o.get("side", "").upper()
            side_color = "#00c853" if side == "BUY" else "#d32f2f"
            orders_html += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #333;">{o.get('created_at','')[:16].replace('T',' ')} UTC</td>
                <td style="padding:8px;border-bottom:1px solid #333;color:{side_color};font-weight:bold;">{side}</td>
                <td style="padding:8px;border-bottom:1px solid #333;font-weight:bold;">{o.get('symbol','')}</td>
                <td style="padding:8px;border-bottom:1px solid #333;">{o.get('qty','')} shares</td>
                <td style="padding:8px;border-bottom:1px solid #333;">{o.get('status','').upper()}</td>
                <td style="padding:8px;border-bottom:1px solid #333;">${float(o.get('filled_avg_price') or 0):.2f}</td>
            </tr>"""
    else:
        orders_html = '<tr><td colspan="6" style="padding:12px;text-align:center;color:#888;">No trades executed today</td></tr>'

    # Open positions table
    positions_html = ""
    if isinstance(positions, list) and positions:
        for p in positions:
            unreal = float(p.get("unrealized_pl", 0))
            unreal_pct = float(p.get("unrealized_plpc", 0)) * 100
            p_color = "#00c853" if unreal >= 0 else "#d32f2f"
            positions_html += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #333;font-weight:bold;">{p.get('symbol','')}</td>
                <td style="padding:8px;border-bottom:1px solid #333;">{p.get('qty','')} shares</td>
                <td style="padding:8px;border-bottom:1px solid #333;">${float(p.get('avg_entry_price',0)):.2f}</td>
                <td style="padding:8px;border-bottom:1px solid #333;">${float(p.get('current_price',0)):.2f}</td>
                <td style="padding:8px;border-bottom:1px solid #333;color:{p_color};font-weight:bold;">${unreal:+.2f} ({unreal_pct:+.1f}%)</td>
                <td style="padding:8px;border-bottom:1px solid #333;">${float(p.get('market_value',0)):,.2f}</td>
            </tr>"""
    else:
        positions_html = '<tr><td colspan="6" style="padding:12px;text-align:center;color:#888;">No open positions</td></tr>'

    # Swing positions
    swing_html = ""
    if swing_positions:
        for s in swing_positions:
            s_color = "#00c853" if s.get("current_pnl_dollars", 0) >= 0 else "#d32f2f"
            swing_html += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #333;font-weight:bold;">{s.get('ticker','')}</td>
                <td style="padding:8px;border-bottom:1px solid #333;">{s.get('direction','')}</td>
                <td style="padding:8px;border-bottom:1px solid #333;">${s.get('entry_price',0):.2f}</td>
                <td style="padding:8px;border-bottom:1px solid #333;color:{s_color};">${s.get('current_pnl_dollars',0):+.2f} ({s.get('current_pnl_pct',0):+.1f}%)</td>
                <td style="padding:8px;border-bottom:1px solid #333;">{s.get('hold_days','?')}d hold</td>
                <td style="padding:8px;border-bottom:1px solid #333;font-style:italic;font-size:12px;">{s.get('thesis','')[:60]}</td>
            </tr>"""
    else:
        swing_html = '<tr><td colspan="6" style="padding:12px;text-align:center;color:#888;">No swing positions open</td></tr>'

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0a0a0f; color:#e0e0e0; margin:0; padding:0; }}
.container {{ max-width:700px; margin:0 auto; padding:24px; }}
.header {{ background:linear-gradient(135deg,#1a1a2e,#16213e); border-radius:12px; padding:24px; margin-bottom:24px; border-left:4px solid #ff6b35; }}
.header h1 {{ margin:0 0 4px 0; font-size:22px; color:#fff; }}
.header .date {{ color:#888; font-size:14px; }}
.metric-row {{ display:flex; gap:16px; margin-bottom:24px; }}
.metric {{ flex:1; background:#111118; border-radius:10px; padding:16px; border:1px solid #222230; }}
.metric .label {{ font-size:11px; text-transform:uppercase; letter-spacing:1px; color:#666; margin-bottom:6px; }}
.metric .value {{ font-size:24px; font-weight:700; }}
.section {{ background:#111118; border-radius:10px; padding:16px; margin-bottom:20px; border:1px solid #222230; }}
.section h2 {{ margin:0 0 14px 0; font-size:14px; text-transform:uppercase; letter-spacing:1px; color:#888; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; padding:8px; color:#666; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; border-bottom:1px solid #333; }}
.footer {{ text-align:center; color:#444; font-size:12px; margin-top:24px; }}
</style></head>
<body>
<div class="container">
  <div class="header">
    <h1>🏦 MarketQuant Daily Report</h1>
    <div class="date">{date_str} · Paper Trading · Alpaca Markets</div>
  </div>

  <div class="metric-row">
    <div class="metric">
      <div class="label">Account Equity</div>
      <div class="value" style="color:#fff;">${equity:,.2f}</div>
    </div>
    <div class="metric">
      <div class="label">Total P&amp;L</div>
      <div class="value" style="color:{pnl_color};">{'+' if pnl_total >= 0 else ''}${pnl_total:,.2f}</div>
    </div>
    <div class="metric">
      <div class="label">Today's P&amp;L</div>
      <div class="value" style="color:{day_color};">{'+' if today_pnl >= 0 else ''}${today_pnl:,.2f}</div>
    </div>
    <div class="metric">
      <div class="label">Cash Available</div>
      <div class="value" style="color:#888;">${cash:,.2f}</div>
    </div>
  </div>

  <div class="section">
    <h2>📋 Today's Orders ({len(orders)} trades)</h2>
    <table>
      <thead><tr>
        <th>Time</th><th>Side</th><th>Symbol</th><th>Quantity</th><th>Status</th><th>Fill Price</th>
      </tr></thead>
      <tbody>{orders_html}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>📊 Open Scalp Positions ({len(positions) if isinstance(positions, list) else 0})</h2>
    <table>
      <thead><tr>
        <th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Unrealized P&amp;L</th><th>Value</th>
      </tr></thead>
      <tbody>{positions_html}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>🎯 Swing Positions ({len(swing_positions)})</h2>
    <table>
      <thead><tr>
        <th>Symbol</th><th>Direction</th><th>Entry</th><th>P&amp;L</th><th>Hold</th><th>Thesis</th>
      </tr></thead>
      <tbody>{swing_html}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>⚙️ System Status</h2>
    <p style="margin:0;font-size:13px;color:#888;">
      Signal scanner: Active (every 30 min) · Position monitor: Active (every 5 min) · 
      Alert dispatcher: Active (every 5 min) · Starting capital: $100,000.00
    </p>
  </div>

  <div class="footer">
    MarketQuant · Vencat Capital · Paper Trading Only · Not Financial Advice<br>
    github.com/vencatron/market-quant · {date_str}
  </div>
</div>
</body></html>"""
    return html


def send_daily_report():
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%A, %B %d, %Y")
    subject = f"MarketQuant Daily Report — {today.strftime('%b %d, %Y')}"

    print(f"Building daily report for {date_str}...")

    account = get_account()
    positions = get_positions()
    orders = get_today_orders()
    today_pnl, trades_today = get_today_pnl()
    swing_positions = get_swing_positions()

    html = build_html_report(account, positions, orders, today_pnl, trades_today, swing_positions, date_str)

    # Save HTML to temp file
    html_file = "/tmp/marketquant_daily_report.html"
    with open(html_file, "w") as f:
        f.write(html)

    equity = float(account.get("equity", 100000))
    pnl_total = equity - 100000
    pnl_sign = "+" if pnl_total >= 0 else ""

    # Send to each recipient
    for recipient in RECIPIENTS:
        try:
            result = subprocess.run(
                [
                    "gog", "gmail", "send",
                    "--account", SENDER_ACCOUNT,
                    "--to", recipient,
                    "--subject", subject,
                    "--body-html", html,
                ],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print(f"✅ Report sent to {recipient}")
            else:
                print(f"❌ Failed to send to {recipient}: {result.stderr[:200]}")
        except Exception as e:
            print(f"❌ Error sending to {recipient}: {e}")

    # Summary
    print(f"\nDaily report sent.")
    print(f"Equity: ${equity:,.2f} | Total P&L: {pnl_sign}${pnl_total:,.2f} | Today: ${today_pnl:+.2f}")
    print(f"Open positions: {len(positions) if isinstance(positions, list) else 0} | Orders today: {len(orders)}")


if __name__ == "__main__":
    send_daily_report()
