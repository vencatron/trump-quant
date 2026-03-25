"""
TrumpQuant Weekend War Engine
Pre-positions for major geopolitical events over weekends.
Runs Thursday 3pm and Friday 2pm ET.
Monday 9:25am: gap detector fires exit/entry on gap opens.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

sys.path.insert(0, os.path.dirname(__file__))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WEEKEND_POS_FILE = os.path.join(DATA_DIR, "weekend_positions.json")
VIX_CACHE_FILE = os.path.join(DATA_DIR, "vix_cache.json")
SEEN_FILE = os.path.join(DATA_DIR, "monitor_seen.json")
POSTS_FILE = os.path.join(DATA_DIR, "posts.json")

ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"


def _alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def _send_telegram(text):
    """Send Telegram alert via openclaw."""
    try:
        result = subprocess.run(
            ["openclaw", "message", "send", "--to", "8387647137",
             "--channel", "telegram", "--message", text],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False


def _get_price(ticker):
    """Get latest mid-price from Alpaca."""
    try:
        url = f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/quotes/latest"
        resp = requests.get(url, headers=_alpaca_headers(), timeout=10)
        if resp.status_code == 200:
            q = resp.json().get("quote", {})
            mid = (q.get("ap", 0) + q.get("bp", 0)) / 2
            if mid > 0:
                return round(mid, 2)
    except Exception as e:
        print(f"  Price fetch error for {ticker}: {e}")
    return None


def _submit_order(ticker, notional, side="buy"):
    """Submit a notional market order to Alpaca paper trading."""
    url = f"{ALPACA_URL}/v2/orders"
    payload = {
        "symbol": ticker,
        "notional": str(notional),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    try:
        resp = requests.post(url, json=payload, headers=_alpaca_headers(), timeout=15)
        if resp.status_code in (200, 201):
            order = resp.json()
            print(f"  ORDER: {side.upper()} ${notional} {ticker} — id={order.get('id', '?')}")
            return order
        else:
            print(f"  ORDER FAILED ({resp.status_code}): {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"  ORDER ERROR: {e}")
        return None


def _close_position(ticker):
    """Close an existing Alpaca position."""
    try:
        resp = requests.delete(
            f"{ALPACA_URL}/v2/positions/{ticker}",
            headers=_alpaca_headers(),
            timeout=10
        )
        if resp.status_code in (200, 204):
            print(f"  CLOSED position: {ticker}")
            return True
        else:
            print(f"  Failed to close {ticker}: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"  Failed to close {ticker}: {e}")
        return False


def _load_json(filepath, default=None):
    if default is None:
        default = {}
    if os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return default


def _save_json(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def assess_weekend_risk() -> dict:
    """
    Returns a risk score 0-100 for the weekend.
    Factors: VIX level, Trump post frequency, Iran activity, day of week.
    """
    score = 0
    vix_level = None
    post_freq = 0
    iran_active = False

    # --- Factor 1: VIX level ---
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d")
        if not hist.empty:
            vix_level = float(hist["Close"].iloc[-1])
            if vix_level < 20:
                score += 10
            elif vix_level < 25:
                score += 25
            elif vix_level < 30:
                score += 40
            else:
                score += 60
    except Exception as e:
        print(f"  VIX fetch error: {e}")
        # Fallback: try cached VIX
        cache = _load_json(VIX_CACHE_FILE)
        if cache.get("vix"):
            vix_level = cache["vix"]
            if vix_level < 20:
                score += 10
            elif vix_level < 25:
                score += 25
            elif vix_level < 30:
                score += 40
            else:
                score += 60

    # --- Factor 2: Trump post frequency (last 48h) ---
    try:
        posts = _load_json(POSTS_FILE, default=[])
        if isinstance(posts, list):
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            recent = 0
            for p in posts:
                try:
                    post_date = p.get("date", "")
                    if post_date:
                        # Try ISO format first
                        dt = datetime.fromisoformat(post_date.replace("Z", "+00:00"))
                        if dt > cutoff:
                            recent += 1
                except (ValueError, TypeError):
                    pass
            post_freq = recent
            if post_freq > 20:
                score += 30
            elif post_freq > 10:
                score += 20
    except Exception as e:
        print(f"  Post frequency check error: {e}")

    # --- Factor 3: Iran activity (IRAN_ESCALATION in last 7 days) ---
    try:
        seen_data = _load_json(SEEN_FILE, default=[])
        # monitor_seen.json is a list of post IDs; check posts_categorized for Iran
        cat_file = os.path.join(DATA_DIR, "posts_categorized.json")
        categorized = _load_json(cat_file, default=[])
        if isinstance(categorized, list):
            cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
            iran_count = 0
            for p in categorized:
                cats = p.get("categories", [])
                if "IRAN_ESCALATION" in cats:
                    try:
                        dt = datetime.fromisoformat(
                            p.get("date", "").replace("Z", "+00:00")
                        )
                        if dt > cutoff_7d:
                            iran_count += 1
                    except (ValueError, TypeError):
                        iran_count += 1  # count it if date parsing fails
            if iran_count > 5:
                score += 20
                iran_active = True
    except Exception as e:
        print(f"  Iran activity check error: {e}")

    # --- Factor 4: Day of week bonus ---
    now_et = datetime.now(timezone(timedelta(hours=-4)))
    if now_et.weekday() in (3, 4):  # Thursday=3, Friday=4
        score += 10

    # Clamp to 0-100
    score = max(0, min(100, score))

    # Determine recommendation
    if score < 30:
        recommendation = "HOLD"
    elif score <= 60:
        recommendation = "LIGHT"
    else:
        recommendation = "FULL"

    result = {
        "score": score,
        "vix": vix_level,
        "post_freq": post_freq,
        "iran_active": iran_active,
        "recommendation": recommendation,
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }

    print(f"  Weekend Risk Assessment: score={score}, vix={vix_level}, "
          f"posts_48h={post_freq}, iran={iran_active} → {recommendation}")
    return result


def enter_weekend_positions(recommendation: str):
    """
    Enter pre-weekend positions based on risk assessment.
    LIGHT: GLD $5,000
    FULL:  GLD $5,000 + UVIX $3,000 + SQQQ $2,500
    """
    if recommendation == "HOLD":
        print("  HOLD recommendation — no positions to enter")
        return

    positions_entered = []
    now = datetime.now(timezone.utc)

    if recommendation in ("LIGHT", "FULL"):
        # BUY GLD $5,000
        price = _get_price("GLD")
        order = _submit_order("GLD", 5000, "buy")
        if order:
            positions_entered.append({
                "ticker": "GLD",
                "notional": 5000,
                "entry_price": price,
                "order_id": order.get("id"),
                "side": "buy",
            })

    if recommendation == "FULL":
        # BUY UVIX $3,000
        price = _get_price("UVIX")
        order = _submit_order("UVIX", 3000, "buy")
        if order:
            positions_entered.append({
                "ticker": "UVIX",
                "notional": 3000,
                "entry_price": price,
                "order_id": order.get("id"),
                "side": "buy",
            })

        # BUY SQQQ $2,500
        price = _get_price("SQQQ")
        order = _submit_order("SQQQ", 2500, "buy")
        if order:
            positions_entered.append({
                "ticker": "SQQQ",
                "notional": 2500,
                "entry_price": price,
                "order_id": order.get("id"),
                "side": "buy",
            })

    # Save weekend positions
    weekend_data = {
        "recommendation": recommendation,
        "positions": positions_entered,
        "entry_time": now.isoformat(),
        "status": "OPEN",
        "thesis": f"Weekend war pre-position ({recommendation})",
    }
    _save_json(WEEKEND_POS_FILE, weekend_data)

    # Build position summary for alert
    pos_lines = []
    total = 0
    for p in positions_entered:
        pos_lines.append(f"  {p['ticker']}: ${p['notional']:,}")
        total += p["notional"]

    positions_text = "\n".join(pos_lines)
    msg = (
        f"🌙 *Weekend War Position — {recommendation}*\n\n"
        f"Positions entered:\n{positions_text}\n"
        f"Total: ${total:,}\n\n"
        f"Hold until Monday open"
    )
    _send_telegram(msg)
    print(f"  Entered {len(positions_entered)} weekend positions ({recommendation})")


def monday_gap_detector():
    """
    Run at 9:25am Monday ET (5 min before open).
    Detects weekend gaps and takes action.
    """
    print("=== MONDAY GAP DETECTOR ===")

    # Load weekend positions
    weekend_data = _load_json(WEEKEND_POS_FILE)
    if not weekend_data or weekend_data.get("status") != "OPEN":
        print("  No open weekend positions — nothing to do")
        _send_telegram("📊 Monday Gap Check — no weekend positions open")
        return

    positions = weekend_data.get("positions", [])
    if not positions:
        print("  Weekend positions file exists but empty")
        return

    # Fetch SPY pre-market price via Alpaca
    spy_premarket = None
    try:
        url = f"{ALPACA_DATA_URL}/v2/stocks/SPY/quotes/latest"
        resp = requests.get(url, headers=_alpaca_headers(), timeout=10)
        if resp.status_code == 200:
            q = resp.json().get("quote", {})
            spy_premarket = (q.get("ap", 0) + q.get("bp", 0)) / 2
    except Exception as e:
        print(f"  SPY premarket fetch error: {e}")

    # Fetch SPY Friday close via yfinance
    spy_friday_close = None
    try:
        import yfinance as yf
        spy = yf.Ticker("SPY")
        hist = spy.history(period="5d")
        if not hist.empty:
            spy_friday_close = float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"  SPY Friday close fetch error: {e}")

    if not spy_premarket or not spy_friday_close:
        msg = "⚠️ Monday Gap Detector — couldn't get SPY prices. Manual review needed."
        _send_telegram(msg)
        print(f"  Missing prices: premarket={spy_premarket}, friday_close={spy_friday_close}")
        return

    gap_pct = (spy_premarket - spy_friday_close) / spy_friday_close * 100
    print(f"  SPY: Friday close=${spy_friday_close:.2f}, Premarket=${spy_premarket:.2f}, Gap={gap_pct:+.2f}%")

    if gap_pct <= -1.5:
        # GAP DOWN — war happened, hold positions
        print(f"  WAR GAP: SPY gapping {gap_pct:.1f}% — holding war positions")

        # Estimate P&L on war positions
        pnl_lines = []
        for p in positions:
            ticker = p["ticker"]
            current = _get_price(ticker)
            entry = p.get("entry_price")
            if current and entry and entry > 0:
                pnl_pct = ((current - entry) / entry) * 100
                pnl_lines.append(f"  {ticker}: {pnl_pct:+.1f}% (${current:.2f} vs ${entry:.2f})")
            else:
                pnl_lines.append(f"  {ticker}: price unavailable")

        pnl_text = "\n".join(pnl_lines)
        msg = (
            f"☢️ *WAR GAP DETECTED*\n\n"
            f"SPY gapping *{gap_pct:.1f}%* from Friday close\n"
            f"Pre-market: ${spy_premarket:.2f} | Friday: ${spy_friday_close:.2f}\n\n"
            f"📊 Weekend Position P&L:\n{pnl_text}\n\n"
            f"🎯 Action: *HOLDING* all war positions\n"
            f"War thesis confirmed — let winners run"
        )
        _send_telegram(msg)

    elif gap_pct >= 1.5:
        # GAP UP — peace happened, close UVIX, buy the rally
        print(f"  PEACE GAP: SPY gapping +{gap_pct:.1f}% — closing UVIX, buying rally")

        # Close UVIX immediately (peace kills UVIX)
        uvix_closed = False
        for p in positions:
            if p["ticker"] == "UVIX":
                uvix_closed = _close_position("UVIX")

        # Buy the peace rally
        rally_orders = []
        qqq_order = _submit_order("QQQ", 5000, "buy")
        if qqq_order:
            rally_orders.append("QQQ $5,000")
        spy_order = _submit_order("SPY", 5000, "buy")
        if spy_order:
            rally_orders.append("SPY $5,000")

        rally_text = ", ".join(rally_orders) if rally_orders else "Failed to enter"

        msg = (
            f"🕊️ *PEACE GAP DETECTED*\n\n"
            f"SPY gapping *+{gap_pct:.1f}%* from Friday close\n"
            f"Pre-market: ${spy_premarket:.2f} | Friday: ${spy_friday_close:.2f}\n\n"
            f"⚠️ UVIX {'CLOSED' if uvix_closed else 'close FAILED'} — peace kills UVIX\n"
            f"🚀 Rally buys: {rally_text}\n\n"
            f"Riding the peace rally 🕊️"
        )
        _send_telegram(msg)

        # Update weekend positions status
        weekend_data["status"] = "PEACE_EXIT"
        weekend_data["gap_pct"] = gap_pct
        weekend_data["exit_time"] = datetime.now(timezone.utc).isoformat()
        _save_json(WEEKEND_POS_FILE, weekend_data)

    else:
        # Quiet weekend — hold positions
        print(f"  Quiet weekend: SPY gap {gap_pct:+.1f}%")
        msg = (
            f"😴 *Quiet Weekend — Maintaining Positions*\n\n"
            f"SPY gap: {gap_pct:+.1f}% (within ±1.5% threshold)\n"
            f"Pre-market: ${spy_premarket:.2f} | Friday: ${spy_friday_close:.2f}\n\n"
            f"Weekend positions held — scalp engine takes over at open"
        )
        _send_telegram(msg)


def close_weekend_positions():
    """
    Close all weekend positions — called Friday 3:50pm if risk is off,
    or manually when we want to exit pre-weekend positions.
    """
    print("=== CLOSING WEEKEND POSITIONS ===")

    weekend_data = _load_json(WEEKEND_POS_FILE)
    if not weekend_data or weekend_data.get("status") != "OPEN":
        print("  No open weekend positions to close")
        return

    positions = weekend_data.get("positions", [])
    closed = []
    total_pnl = 0

    for p in positions:
        ticker = p["ticker"]
        entry = p.get("entry_price", 0)
        current = _get_price(ticker)

        success = _close_position(ticker)
        pnl = 0
        if current and entry and entry > 0:
            pnl_pct = ((current - entry) / entry) * 100
            pnl = pnl_pct * p.get("notional", 0) / 100
        else:
            pnl_pct = 0

        closed.append({
            "ticker": ticker,
            "entry": entry,
            "exit": current,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollars": round(pnl, 2),
            "closed": success,
        })
        total_pnl += pnl

    # Update file
    weekend_data["status"] = "CLOSED"
    weekend_data["closed_at"] = datetime.now(timezone.utc).isoformat()
    weekend_data["close_results"] = closed
    weekend_data["total_pnl"] = round(total_pnl, 2)
    _save_json(WEEKEND_POS_FILE, weekend_data)

    # Build summary
    lines = []
    for c in closed:
        emoji = "✅" if c["pnl_dollars"] >= 0 else "❌"
        lines.append(f"{emoji} {c['ticker']}: {c['pnl_pct']:+.1f}% (${c['pnl_dollars']:+.2f})")

    summary = "\n".join(lines)
    msg = (
        f"📊 *Weekend Positions Closed*\n\n"
        f"{summary}\n\n"
        f"*Net P&L: ${total_pnl:+.2f}*"
    )
    _send_telegram(msg)
    print(f"  Closed {len(closed)} weekend positions. Net P&L: ${total_pnl:+.2f}")


def run_friday_assessment():
    """
    Main entry point for Thursday 3pm / Friday 2pm cron.
    Assesses weekend risk and enters positions if warranted.
    """
    print("=== FRIDAY/THURSDAY WEEKEND ASSESSMENT ===")

    # Step 1: Assess risk
    risk = assess_weekend_risk()

    # Step 2: Check if we already have weekend positions open
    weekend_data = _load_json(WEEKEND_POS_FILE)
    already_positioned = (
        weekend_data.get("status") == "OPEN"
        and len(weekend_data.get("positions", [])) > 0
    )

    if already_positioned:
        # Already have positions — update alert with current P&L
        positions = weekend_data.get("positions", [])
        pnl_lines = []
        total_pnl = 0
        for p in positions:
            ticker = p["ticker"]
            current = _get_price(ticker)
            entry = p.get("entry_price", 0)
            if current and entry and entry > 0:
                pnl_pct = ((current - entry) / entry) * 100
                pnl = pnl_pct * p.get("notional", 0) / 100
                pnl_lines.append(f"  {ticker}: {pnl_pct:+.1f}% (${pnl:+.2f})")
                total_pnl += pnl
            else:
                pnl_lines.append(f"  {ticker}: price unavailable")

        pnl_text = "\n".join(pnl_lines)
        msg = (
            f"📊 *Weekend Risk Update*\n\n"
            f"Score: {risk['score']}/100 → {risk['recommendation']}\n"
            f"VIX: {risk['vix']:.1f if risk['vix'] else 'N/A'}\n"
            f"Posts (48h): {risk['post_freq']}\n"
            f"Iran active: {'🔴 YES' if risk['iran_active'] else '🟢 No'}\n\n"
            f"Current positions:\n{pnl_text}\n"
            f"*Net P&L: ${total_pnl:+.2f}*\n\n"
            f"Already positioned — holding"
        )
        _send_telegram(msg)
        print(f"  Already positioned. Current P&L: ${total_pnl:+.2f}")
        return

    # Step 3: Enter positions if score warrants it
    if risk["score"] > 30:
        enter_weekend_positions(risk["recommendation"])
    else:
        msg = (
            f"📊 *Weekend Risk Assessment: HOLD*\n\n"
            f"Score: {risk['score']}/100 (below 30 threshold)\n"
            f"VIX: {risk['vix']:.1f if risk['vix'] else 'N/A'}\n"
            f"Posts (48h): {risk['post_freq']}\n"
            f"Iran active: {'🔴 YES' if risk['iran_active'] else '🟢 No'}\n\n"
            f"No weekend positions — risk too low"
        )
        _send_telegram(msg)
        print(f"  Score {risk['score']} < 30 — no positions entered")

    print("=== ASSESSMENT COMPLETE ===")
