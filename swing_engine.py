"""
TrumpQuant Swing Engine
Long-term positions (3-10 days) based on high-conviction macro signals.
Runs independently from scalp engine. Larger size, wider stops.
"""

import json
import os
import requests
import time
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ALPACA_URL = "https://paper-api.alpaca.markets"
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "PKQ2P7KLMAJH5E3IQVKYQPTBOB")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "A58boqhagLVQH7tKfz7MafU8axJx6HGc9GbR4VgUFhrT")

SWING_POSITIONS_FILE = os.path.join(DATA_DIR, "swing_positions.json")
SWING_LOG_FILE = os.path.join(DATA_DIR, "swing_log.jsonl")

MAX_SWING_POSITIONS = 2
SWING_POSITION_SIZE = 5000  # $5k per swing trade

# High-conviction swing signals based on 35k post dataset
# These play out over days/weeks, not minutes
SWING_SIGNALS = {
    "IRAN_ESCALATION": [
        {"ticker": "XLE",  "direction": "BUY", "target_pct": 6.0,  "stop_pct": 2.0, "hold_days": 5,  "conviction": "VERY_HIGH", "thesis": "Oil supply shock — Hormuz closure risk drives energy for days"},
        {"ticker": "LMT",  "direction": "BUY", "target_pct": 5.0,  "stop_pct": 2.0, "hold_days": 7,  "conviction": "VERY_HIGH", "thesis": "Defense contracts — war = Lockheed revenue for months"},
    ],
    "TARIFFS": [
        {"ticker": "GLD",  "direction": "BUY", "target_pct": 2.3,  "stop_pct": 1.5, "hold_days": 7,  "conviction": "VERY_HIGH", "thesis": "Gold +2.3% weekly on any tariff post — 95% win rate, 1740 data points"},
    ],
    "FED_ATTACK": [
        {"ticker": "TLT",  "direction": "BUY", "target_pct": 2.0,  "stop_pct": 1.0, "hold_days": 3,  "conviction": "HIGH",      "thesis": "Bond rally on Fed uncertainty — flight to safety"},
        {"ticker": "GLD",  "direction": "BUY", "target_pct": 2.3,  "stop_pct": 1.5, "hold_days": 7,  "conviction": "HIGH",      "thesis": "Gold benefits from dollar weakness when Fed is attacked"},
    ],
    "IRAN_DEESCALATION": [
        {"ticker": "QQQ",  "direction": "BUY", "target_pct": 4.0,  "stop_pct": 1.5, "hold_days": 3,  "conviction": "VERY_HIGH", "thesis": "Peace = sustained tech rally, not just same-day pop"},
        {"ticker": "XLE",  "direction": "SHORT","target_pct": 3.0, "stop_pct": 1.5, "hold_days": 3,  "conviction": "HIGH",      "thesis": "Oil drops multi-day as Hormuz risk fades"},
    ],
    "TRADE_DEAL": [
        {"ticker": "QQQ",  "direction": "BUY", "target_pct": 3.0,  "stop_pct": 1.5, "hold_days": 5,  "conviction": "HIGH",      "thesis": "Tech multi-day rally on supply chain relief"},
        {"ticker": "XLB",  "direction": "BUY", "target_pct": 3.5,  "stop_pct": 1.5, "hold_days": 5,  "conviction": "HIGH",      "thesis": "Materials sector recovers over days on trade relief"},
    ],
    "WAR_ESCALATION": [
        {"ticker": "LMT",  "direction": "BUY", "target_pct": 5.0,  "stop_pct": 2.0, "hold_days": 7,  "conviction": "VERY_HIGH", "thesis": "Defense multi-week rally on sustained conflict"},
        {"ticker": "GLD",  "direction": "BUY", "target_pct": 3.0,  "stop_pct": 1.5, "hold_days": 7,  "conviction": "HIGH",      "thesis": "Safe haven demand sustained during war"},
    ],
}

def alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

def load_swing_positions():
    if os.path.exists(SWING_POSITIONS_FILE):
        try:
            with open(SWING_POSITIONS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []

def save_swing_positions(positions):
    with open(SWING_POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

def get_current_price(ticker):
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
        r = requests.get(url, headers=alpaca_headers(), timeout=8)
        if r.status_code == 200:
            q = r.json().get("quote", {})
            mid = (q.get("ap", 0) + q.get("bp", 0)) / 2
            if mid > 0:
                return round(mid, 2)
    except Exception:
        pass
    return None

def open_swing_position(ticker, direction, signal_category, thesis, target_pct, stop_pct, hold_days, conviction):
    """Open a new swing position."""
    positions = load_swing_positions()

    # Check limits
    if len(positions) >= MAX_SWING_POSITIONS:
        print(f"  [SWING] Max positions reached ({MAX_SWING_POSITIONS}) — skipping {ticker}")
        return None

    # No duplicate tickers
    held = {p["ticker"] for p in positions}
    if ticker in held:
        print(f"  [SWING] Already holding {ticker} — skipping")
        return None

    price = get_current_price(ticker)
    if not price:
        print(f"  [SWING] Cannot get price for {ticker}")
        return None

    shares = max(1, int(SWING_POSITION_SIZE / price))
    side = "buy" if direction == "BUY" else "sell"

    # Submit order
    try:
        payload = {"symbol": ticker, "qty": shares, "side": side, "type": "market", "time_in_force": "day"}
        r = requests.post(f"{ALPACA_URL}/v2/orders", json=payload, headers=alpaca_headers(), timeout=10)
        if r.status_code not in (200, 201):
            print(f"  [SWING] Order failed: {r.text[:200]}")
            return None
        order = r.json()
    except Exception as e:
        print(f"  [SWING] Order error: {e}")
        return None

    exit_date = (datetime.now(timezone.utc) + timedelta(days=hold_days)).isoformat()
    position = {
        "position_id": f"swing-{int(time.time())}-{ticker}",
        "ticker": ticker,
        "direction": direction,
        "shares": shares,
        "entry_price": price,
        "position_value": price * shares,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "hold_days": hold_days,
        "exit_by": exit_date,
        "conviction": conviction,
        "thesis": thesis,
        "signal_category": signal_category,
        "order_id": order.get("id"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "OPEN",
    }

    positions.append(position)
    save_swing_positions(positions)

    print(f"  [SWING] Opened: {direction} {shares}x {ticker} @ ${price:.2f} | target +{target_pct}% | hold {hold_days}d")
    print(f"  [SWING] Thesis: {thesis}")

    # Log
    with open(SWING_LOG_FILE, "a") as f:
        f.write(json.dumps({**position, "event": "OPEN"}) + "\n")

    return position

def monitor_swing_positions():
    """Check all open swing positions. Close if target/stop/time hit."""
    positions = load_swing_positions()
    if not positions:
        return []

    closed = []
    remaining = []
    now = datetime.now(timezone.utc)

    for pos in positions:
        ticker = pos["ticker"]
        current = get_current_price(ticker)
        if not current:
            remaining.append(pos)
            continue

        entry = pos["entry_price"]
        if pos["direction"] == "BUY":
            pnl_pct = ((current - entry) / entry) * 100
        else:
            pnl_pct = ((entry - current) / entry) * 100

        pnl_dollars = (pnl_pct / 100) * pos["position_value"]

        # Check exit conditions
        should_close = False
        close_reason = ""

        if pnl_pct >= pos["target_pct"]:
            should_close = True
            close_reason = f"TARGET_HIT (+{pnl_pct:.2f}%)"
        elif pnl_pct <= -pos["stop_pct"]:
            should_close = True
            close_reason = f"STOP_LOSS ({pnl_pct:.2f}%)"
        else:
            # Check time exit
            exit_dt = datetime.fromisoformat(pos["exit_by"].replace("Z", "+00:00"))
            if now >= exit_dt:
                should_close = True
                close_reason = f"TIME_EXIT ({pos['hold_days']}d hold, {pnl_pct:+.2f}%)"

        if should_close:
            # Close the position
            try:
                r = requests.delete(f"{ALPACA_URL}/v2/positions/{ticker}", headers=alpaca_headers(), timeout=10)
                status = "CLOSED" if r.status_code in (200, 204) else "CLOSE_FAILED"
            except Exception:
                status = "CLOSE_FAILED"

            result = {**pos, "close_reason": close_reason, "exit_price": current,
                     "pnl_pct": pnl_pct, "pnl_dollars": pnl_dollars, "status": status,
                     "closed_at": now.isoformat()}
            closed.append(result)

            with open(SWING_LOG_FILE, "a") as f:
                f.write(json.dumps({**result, "event": "CLOSE"}) + "\n")

            emoji = "+" if pnl_dollars >= 0 else "-"
            print(f"  [SWING] {emoji} Closed {ticker}: {close_reason} | P&L: ${pnl_dollars:+.2f}")
        else:
            pos["current_price"] = current
            pos["current_pnl_pct"] = pnl_pct
            pos["current_pnl_dollars"] = pnl_dollars
            remaining.append(pos)

    save_swing_positions(remaining)
    return closed

def process_signal_for_swing(category, post_text=""):
    """Called by signal_check.py when a signal fires. Opens swing positions if appropriate."""
    if category not in SWING_SIGNALS:
        return []

    opened = []
    for sig in SWING_SIGNALS[category]:
        pos = open_swing_position(
            ticker=sig["ticker"],
            direction=sig["direction"],
            signal_category=category,
            thesis=sig["thesis"],
            target_pct=sig["target_pct"],
            stop_pct=sig["stop_pct"],
            hold_days=sig["hold_days"],
            conviction=sig["conviction"],
        )
        if pos:
            opened.append(pos)
            if len(load_swing_positions()) >= MAX_SWING_POSITIONS:
                break

    return opened

def get_swing_summary():
    """Returns current swing portfolio summary."""
    positions = load_swing_positions()
    if not positions:
        return {"positions": 0, "total_value": 0, "total_pnl": 0, "holdings": []}

    total_pnl = sum(p.get("current_pnl_dollars", 0) for p in positions)
    return {
        "positions": len(positions),
        "total_value": sum(p["position_value"] for p in positions),
        "total_pnl": round(total_pnl, 2),
        "holdings": [{"ticker": p["ticker"], "direction": p["direction"],
                      "pnl_pct": p.get("current_pnl_pct", 0),
                      "thesis": p["thesis"], "days_left": p["hold_days"]} for p in positions]
    }
