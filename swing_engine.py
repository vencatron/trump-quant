"""
TrumpQuant Swing Engine
Long-term positions (3-10 days) based on high-conviction macro signals.
Runs independently from scalp engine. Larger size, wider stops.
"""

import json
import logging
import os
import requests
import time
from datetime import datetime, timezone, timedelta

from alpaca_utils import get_headers, get_price, submit_order, close_position as alpaca_close_position

logger = logging.getLogger("trumpquant.swing_engine")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ALPACA_URL = "https://paper-api.alpaca.markets"

SWING_POSITIONS_FILE = os.path.join(DATA_DIR, "swing_positions.json")
SWING_LOG_FILE = os.path.join(DATA_DIR, "swing_log.jsonl")
SWING_TRAILING_FILE = os.path.join(DATA_DIR, "swing_trailing_stops.json")

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
    """Return Alpaca API headers. Delegates to shared alpaca_utils."""
    return get_headers()

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
    """Get latest price for a ticker. Delegates to shared alpaca_utils."""
    return get_price(ticker)

def load_swing_trailing_stops():
    if os.path.exists(SWING_TRAILING_FILE):
        try:
            with open(SWING_TRAILING_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_swing_trailing_stops(stops):
    with open(SWING_TRAILING_FILE, 'w') as f:
        json.dump(stops, f, indent=2)


def open_swing_position(ticker, direction, signal_category, thesis, target_pct, stop_pct, hold_days, conviction):
    """Open a new swing position."""
    positions = load_swing_positions()

    # Check limits
    if len(positions) >= MAX_SWING_POSITIONS:
        logger.info("[SWING] Max positions reached ({MAX_SWING_POSITIONS}) — skipping {ticker}")
        return None

    # No duplicate tickers
    held = {p["ticker"] for p in positions}
    if ticker in held:
        logger.info("[SWING] Already holding {ticker} — skipping")
        return None

    price = get_current_price(ticker)
    if not price:
        logger.info("[SWING] Cannot get price for {ticker}")
        return None

    shares = max(1, int(SWING_POSITION_SIZE / price))
    side = "buy" if direction == "BUY" else "sell"

    # Submit order via shared utility with retry logic
    order = submit_order(ticker, shares, side)
    if not order:
        logger.warning("[SWING] Order failed for %s %s %s", side, shares, ticker)
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

    logger.info("[SWING] Opened: {direction} {shares}x {ticker} @ ${price:.2f} | target +{target_pct}% | hold {hold_days}d")
    logger.info("[SWING] Thesis: {thesis}")

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

        # Load trailing stops state
        swing_trailing = load_swing_trailing_stops()
        trail_state = swing_trailing.get(ticker, {'high_pct': 0, 'trail_stop_pct': None, 'partial_taken': False})

        # Update high water mark
        if pnl_pct > trail_state.get('high_pct', 0):
            trail_state['high_pct'] = pnl_pct

        high_pct = trail_state['high_pct']

        # Swing trailing stop tiers (wider than scalp):
        # Tier 1: Up +2% → trail at breakeven (0%)
        # Tier 2: Up +3% → trail at 1% below high + partial exit 50%
        # Tier 3: Up +4% → trail at 1.5% below high
        if high_pct >= 4.0:
            trail_state['trail_stop_pct'] = high_pct - 1.5
        elif high_pct >= 3.0:
            trail_state['trail_stop_pct'] = high_pct - 1.0
            # Partial exit: close 50% at +3% if not already done
            if not trail_state.get('partial_taken', False):
                total_qty = pos.get('shares', 0)
                partial_qty = max(1, total_qty // 2)
                print(f'  [SWING] PARTIAL EXIT: Closing {partial_qty}/{total_qty} of {ticker} at +{pnl_pct:.2f}%')
                try:
                    side = 'sell' if pos['direction'] == 'BUY' else 'buy'
                    order = submit_order(ticker, partial_qty, side)
                    if order:
                        trail_state['partial_taken'] = True
                        pos['shares'] = total_qty - partial_qty
                        pos['position_value'] = pos['entry_price'] * pos['shares']
                except Exception as e:
                    logger.error("[SWING] Partial exit error: %s", e)
        elif high_pct >= 2.0:
            trail_state['trail_stop_pct'] = 0  # breakeven

        # Save trailing stop state
        swing_trailing[ticker] = trail_state
        save_swing_trailing_stops(swing_trailing)

        # Check exit conditions
        should_close = False
        close_reason = ""

        # Rule 1: Target hit (hard ceiling)
        if pnl_pct >= pos["target_pct"]:
            should_close = True
            close_reason = f"TARGET_HIT (+{pnl_pct:.2f}%)"

        # Rule 2: Trailing stop hit
        elif trail_state.get('trail_stop_pct') is not None and pnl_pct <= trail_state['trail_stop_pct']:
            should_close = True
            close_reason = f'TRAILING_STOP ({pnl_pct:.2f}%, trail at {trail_state["trail_stop_pct"]:.1f}%)'

        # Rule 3: Hard stop loss
        elif pnl_pct <= -pos["stop_pct"]:
            should_close = True
            close_reason = f"STOP_LOSS ({pnl_pct:.2f}%)"

        # Rule 4: Time exit
        else:
            exit_dt = datetime.fromisoformat(pos["exit_by"].replace("Z", "+00:00"))
            if now >= exit_dt:
                should_close = True
                close_reason = f"TIME_EXIT ({pos['hold_days']}d hold, {pnl_pct:+.2f}%)"

        if should_close:
            # Close the position via shared utility
            success = alpaca_close_position(ticker)
            status = "CLOSED" if success else "CLOSE_FAILED"

            result = {**pos, "close_reason": close_reason, "exit_price": current,
                     "pnl_pct": pnl_pct, "pnl_dollars": pnl_dollars, "status": status,
                     "closed_at": now.isoformat()}
            closed.append(result)

            # Clean up trailing stop state
            if ticker in swing_trailing:
                del swing_trailing[ticker]
                save_swing_trailing_stops(swing_trailing)

            with open(SWING_LOG_FILE, "a") as f:
                f.write(json.dumps({**result, "event": "CLOSE"}) + "\n")

            emoji = "+" if pnl_dollars >= 0 else "-"
            logger.info("[SWING] {emoji} Closed {ticker}: {close_reason} | P&L: ${pnl_dollars:+.2f}")
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
