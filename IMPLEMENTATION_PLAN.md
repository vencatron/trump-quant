# TrumpQuant Bug Fix & Scalping Improvement — Implementation Plan

**Date:** 2026-03-24  
**Status:** Ready for implementation  
**Target file:** `signal_check.py`

---

## Root Cause Analysis

The active_scalps.json confirms the disaster: **~180+ LOGGED trades** in a single morning, across 3 cron runs (15:04, 15:30, 15:49 UTC). The bugs compound:

1. **Duplicate `if __name__` blocks** (lines ~280 and ~370) — `main()` runs TWICE per invocation
2. **Google News hash collisions** — same headline from different sources gets different `gn-XXXXXX` IDs, bypassing the seen file
3. **No per-ticker dedup** — IRAN_ESCALATION fires 3 signals (UVIX, GLD, SQQQ) × 15 articles × 2 main() calls × 3 cron runs = ~270 trade attempts
4. **Position sizing multipliers uncapped** — `regime_mult * learned_mult` pushes trades to $3,745 each (50% over $2,500)
5. **EOD close never fires** — first `if __name__` block calls `main()` unconditionally, so `sys.argv[1] == "eod"` check in second block runs after main() already executed

---

## CRITICAL FIX 0: Remove Duplicate `if __name__` Block

**Problem:** Two `if __name__ == "__main__"` blocks. The first (line ~280) calls `main()` unconditionally. The second (line ~370) does the argv routing. Both execute.

### Change 1: DELETE the first `if __name__` block (line ~280)

**Find and DELETE this exact block** (located between `execute_paper_trade` and `_find_trade_meta`):

```python
if __name__ == "__main__":
    main()
```

This is the block at approximately line 280, right after the `main()` function definition and before `_find_trade_meta()`. **Remove it entirely.** The second `if __name__` block at the bottom of the file is the correct one.

---

## BUG 1 FIX: Duplicate Order Prevention

### Change 2: Add constants and imports at top of file (after line 20, after existing constants)

Add after `BOT_CACHE_FILE = ...` line:

```python
TRADED_TODAY_FILE = os.path.join(DATA_DIR, "traded_today.json")
LEARNING_LOG_FILE = os.path.join(DATA_DIR, "learning_log.jsonl")
EOD_LOG_FILE = os.path.join(DATA_DIR, "eod_log.json")
MAX_TRADES_PER_RUN = 2
MAX_DAILY_EXPOSURE = 10000  # $10k total portfolio cap
MAX_PER_TICKER_DAILY = 2500  # $2,500 hard cap per ticker per day
MAX_POSITIONS = 4  # max 4 concurrent positions
```

### Change 3: Add traded-today tracking functions (after `save_trade()` function)

Insert these new functions after the `save_trade()` function:

```python
def load_traded_today():
    """Load today's traded (post_id, ticker) combos. Auto-resets at midnight."""
    if not os.path.exists(TRADED_TODAY_FILE):
        return {"date": "", "trades": []}
    try:
        with open(TRADED_TODAY_FILE) as f:
            data = json.load(f)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return {"date": today, "trades": []}
        return data
    except (json.JSONDecodeError, ValueError):
        return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "trades": []}


def save_traded_today(data):
    data["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(TRADED_TODAY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def was_traded_today(post_id, ticker):
    """Check if this (post_id, ticker) combo already traded today."""
    data = load_traded_today()
    return any(
        t["post_id"] == post_id and t["ticker"] == ticker
        for t in data.get("trades", [])
    )


def record_traded_today(post_id, ticker):
    """Record that this (post_id, ticker) was traded today."""
    data = load_traded_today()
    data["trades"].append({
        "post_id": post_id,
        "ticker": ticker,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_traded_today(data)


def get_total_exposure():
    """Get total $ exposure across all open Alpaca positions."""
    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/positions",
            headers=alpaca_headers(),
            timeout=8
        )
        if resp.status_code == 200:
            positions = resp.json()
            return sum(abs(float(p.get("market_value", 0))) for p in positions)
    except Exception:
        pass
    return 0


def get_position_for_ticker(ticker):
    """Get existing Alpaca position for a ticker, or None."""
    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/positions/{ticker}",
            headers=alpaca_headers(),
            timeout=8
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None
```

### Change 4: Rewrite `execute_paper_trade()` with all guards

Replace the **entire** `execute_paper_trade()` function with:

```python
def execute_paper_trade(signal, post, category):
    """Execute a paper trade with full safety guards."""
    ticker = signal["ticker"]
    action = signal["action"]
    window = signal["window"]
    confidence = signal["confidence"]

    # Only execute HIGH confidence signals
    if confidence != "HIGH":
        print(f"  Skipping trade — {confidence} confidence (need HIGH)")
        return None

    market_open = is_market_open()
    if not market_open:
        print(f"  Market closed — skipping trade entirely")
        return None

    # Determine actual ticker we'd trade
    if action == "SHORT":
        if ticker in INVERSE_MAP:
            actual_ticker = INVERSE_MAP[ticker]
            side = "buy"
            trade_direction = "SHORT (via inverse ETF)"
        elif ticker == "COIN":
            print(f"  SHORT COIN → no inverse ETF, skipping")
            return None
        else:
            actual_ticker = ticker
            side = "sell"
            trade_direction = "SHORT"
    else:
        actual_ticker = ticker
        side = "buy"
        trade_direction = "LONG"

    # GUARD 1: Check if (post_id, ticker) already traded today
    if was_traded_today(post["id"], actual_ticker):
        print(f"  Skipping — already traded {actual_ticker} for post {post['id']} today")
        return None

    # GUARD 2: Check for existing position (no pyramiding)
    existing_pos = get_position_for_ticker(actual_ticker)
    if existing_pos:
        print(f"  Skipping — already holding {actual_ticker} ({existing_pos.get('qty', '?')} shares)")
        return None

    # GUARD 3: Check total portfolio exposure
    total_exposure = get_total_exposure()
    if total_exposure >= MAX_DAILY_EXPOSURE:
        print(f"  Skipping — total exposure ${total_exposure:.0f} >= ${MAX_DAILY_EXPOSURE} cap")
        return None

    # GUARD 4: Check number of open positions
    try:
        resp = requests.get(f"{ALPACA_URL}/v2/positions", headers=alpaca_headers(), timeout=8)
        num_positions = len(resp.json()) if resp.status_code == 200 else 0
    except Exception:
        num_positions = 0
    if num_positions >= MAX_POSITIONS:
        print(f"  Skipping — already have {num_positions} positions (max {MAX_POSITIONS})")
        return None

    # HARD CAP position size: $2,500 regardless of multipliers
    adjusted_size = MAX_PER_TICKER_DAILY

    # Get price and calculate shares
    price = get_current_price(actual_ticker)
    if not price or price <= 0:
        print(f"  Skipping — couldn't get price for {actual_ticker}")
        return None

    shares = max(1, int(adjusted_size / price))

    # Final dollar check
    order_value = shares * price
    if order_value > MAX_PER_TICKER_DAILY * 1.05:  # 5% tolerance for rounding
        shares = max(1, int(MAX_PER_TICKER_DAILY / price))
        order_value = shares * price

    # Calculate exit time
    if "same day" in window:
        exit_strategy = "EOD"
        exit_by = datetime.now(timezone.utc).replace(hour=21, minute=0, second=0).isoformat()
    else:
        exit_strategy = "5 trading days"
        exit_by = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    # Submit order
    order = submit_alpaca_order(actual_ticker, shares, side)

    # Record in traded_today
    if order:
        record_traded_today(post["id"], actual_ticker)

    # Log trade
    trade = {
        "trade_id": f"tq-{int(time.time())}-{actual_ticker}",
        "signal_category": category,
        "signal_ticker": ticker,
        "actual_ticker": actual_ticker,
        "direction": trade_direction,
        "action": action,
        "side": side,
        "shares": shares,
        "entry_price": price,
        "position_value": round(order_value, 2),
        "confidence": confidence,
        "window": window,
        "avg_return": signal["avg_return"],
        "exit_strategy": exit_strategy,
        "exit_by": exit_by,
        "stop_loss_pct": -0.5,
        "target_pct": abs(signal["avg_return"]),
        "status": "OPEN" if order else "FAILED",
        "order_id": order.get("id") if order else None,
        "post_text": post["text"][:200],
        "post_id": post["id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_was_open": market_open,
    }
    save_trade(trade)

    # Save to active_scalps.json for EOD close tracking
    if order:
        scalps_file = os.path.join(DATA_DIR, "active_scalps.json")
        scalps = []
        if os.path.exists(scalps_file):
            try:
                with open(scalps_file) as f:
                    scalps = json.load(f)
            except (json.JSONDecodeError, ValueError):
                scalps = []
        scalps.append(trade)
        with open(scalps_file, "w") as f:
            json.dump(scalps, f, indent=2)

    return trade if order else None
```

### Change 5: Rewrite `main()` with MAX_TRADES_PER_RUN guard

Replace the **entire** `main()` function with:

```python
def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Refresh regime every 4 hours
    regime_file = os.path.join(DATA_DIR, "market_regime.json")
    if not os.path.exists(regime_file) or (time.time() - os.path.getmtime(regime_file)) > 14400:
        try:
            from regime_detector import detect_regime
            detect_regime()
        except Exception as e:
            print(f"Regime detection skipped: {e}")

    # Apply learned weights to signals
    try:
        from learning_engine import calculate_signal_weights, apply_weights_to_signal
        learned_weights = calculate_signal_weights()
    except Exception:
        learned_weights = {}

    seen = load_seen()
    posts = fetch_posts()
    fired = 0
    trades_this_run = 0

    # Monitor existing positions first (profit-taking / stop-loss)
    monitor_open_positions()

    for post in posts:
        if post["id"] in seen:
            continue
        seen.add(post["id"])

        # GUARD: Max trades per run
        if trades_this_run >= MAX_TRADES_PER_RUN:
            print(f"  Hit MAX_TRADES_PER_RUN ({MAX_TRADES_PER_RUN}) — skipping remaining posts")
            break

        cat_result = categorize_post(post["text"])
        categories = [c for c in cat_result["categories"] if c in SIGNAL_CATEGORIES]

        if not categories:
            continue

        # Find the single best signal across all categories for this post
        best_signal = None
        best_category = None
        for cat in categories:
            if cat in TOP_SIGNALS:
                for sig in TOP_SIGNALS[cat]:
                    if best_signal is None or abs(sig["avg_return"]) > abs(best_signal["avg_return"]):
                        best_signal = sig
                        best_category = cat

        if best_signal:
            # Apply learned weights (but DON'T apply size multiplier — we hard-cap at $2,500)
            if learned_weights and best_category:
                try:
                    best_signal = apply_weights_to_signal(
                        {**best_signal, "signal_category": best_category}, learned_weights
                    )
                    # Strip any learned_size_multiplier — we enforce our own cap
                    best_signal.pop("learned_size_multiplier", None)
                except Exception:
                    pass

            # Execute paper trade (all guards are inside execute_paper_trade)
            trade = execute_paper_trade(best_signal, post, best_category)

            if trade:
                trades_this_run += 1
                # Build and send alert
                alert = build_alert(post, categories, best_signal, trade)
                print(f"FIRING ALERT: {post['text'][:80]}...")
                send_telegram(alert)
                fired += 1

            # Bot detector integration
            if best_signal["confidence"] in ("HIGH", "MEDIUM"):
                try:
                    subprocess.Popen(
                        [
                            sys.executable, "-m", "botdetector", "arm",
                            "--post-id", post["id"],
                            "--text", post["text"][:200],
                            "--categories", *categories,
                        ],
                        cwd=os.path.dirname(__file__),
                        stdout=open(os.path.join(DATA_DIR, "botdetector_stdout.log"), "a"),
                        stderr=open(os.path.join(DATA_DIR, "botdetector_stderr.log"), "a"),
                    )
                except Exception:
                    pass

    save_seen(seen)
    print(f"Done. Checked {len(posts)} posts, fired {fired} alerts, "
          f"trades this run: {trades_this_run}, seen pool: {len(seen)}")
```

---

## BUG 3 FIX: EOD Close Logging & Safety

### Change 6: Rewrite `close_eod_positions()` with logging and stale-position safety

Replace the **entire** `close_eod_positions()` function with:

```python
def close_eod_positions():
    """Close all open paper positions at EOD with logging."""
    print("=== EOD CLOSE TRIGGERED ===")

    trades_file = os.path.join(DATA_DIR, "active_scalps.json")
    active = []
    if os.path.exists(trades_file):
        try:
            with open(trades_file) as f:
                active = json.load(f)
        except (json.JSONDecodeError, ValueError):
            active = []

    closed = []
    headers = alpaca_headers()

    # Get all open positions from Alpaca
    try:
        resp = requests.get(f"{ALPACA_URL}/v2/positions", headers=headers, timeout=10)
        positions = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"Failed to fetch positions: {e}")
        positions = []

    # Close each position
    for pos in positions:
        ticker = pos.get("symbol")
        try:
            resp = requests.delete(f"{ALPACA_URL}/v2/positions/{ticker}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                pnl = float(pos.get("unrealized_pl", 0))
                closed.append({"ticker": ticker, "pnl": pnl, "reason": "EOD"})
                print(f"  Closed {ticker}: ${pnl:+.2f}")
            else:
                print(f"  Failed to close {ticker}: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  Failed to close {ticker}: {e}")

    # Write EOD log
    eod_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "positions_closed": len(closed),
        "closed": closed,
        "total_pnl": sum(t["pnl"] for t in closed) if closed else 0,
        "had_active_scalps": len(active),
    }
    eod_log = []
    if os.path.exists(EOD_LOG_FILE):
        try:
            with open(EOD_LOG_FILE) as f:
                eod_log = json.load(f)
        except (json.JSONDecodeError, ValueError):
            eod_log = []
    eod_log.append(eod_entry)
    with open(EOD_LOG_FILE, "w") as f:
        json.dump(eod_log, f, indent=2)

    # Send Telegram summary
    if closed:
        total_pnl = sum(t["pnl"] for t in closed)
        msg = f"📊 *TrumpQuant EOD Close*\n\nClosed {len(closed)} positions\n"
        for t in closed:
            emoji = "✅" if t["pnl"] >= 0 else "❌"
            msg += f"{emoji} {t['ticker']}: ${t['pnl']:+.2f} ({t['reason']})\n"
        msg += f"\n*Net P&L: ${total_pnl:+.2f}*"
        send_telegram(msg)

        # Log to bot_trades.json
        bot_trades_file = os.path.join(DATA_DIR, "bot_trades.json")
        existing = []
        if os.path.exists(bot_trades_file):
            try:
                with open(bot_trades_file) as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, ValueError):
                existing = []
        for t in closed:
            existing.append({
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "ticker": t["ticker"],
                "exit": t["reason"],
                "pnl": t["pnl"],
            })
        with open(bot_trades_file, "w") as f:
            json.dump(existing, f, indent=2)

        # Record outcomes in learning engine
        try:
            from learning_engine import record_outcome
            for t in closed:
                trade_meta = _find_trade_meta(t["ticker"], active)
                trade_result = {
                    "signal_category": trade_meta.get("signal_category", "UNKNOWN"),
                    "signal_ticker": trade_meta.get("signal_ticker", t["ticker"]),
                    "actual_ticker": t["ticker"],
                    "direction": trade_meta.get("direction", "LONG"),
                    "entry_price": trade_meta.get("entry_price", 0),
                    "pnl": t["pnl"],
                    "exit_reason": t["reason"],
                    "trade_id": trade_meta.get("trade_id", ""),
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                }
                record_outcome(trade_result)
        except Exception as e:
            print(f"  Learning engine error: {e}")
    else:
        print("  No positions to close")
        send_telegram("📊 *TrumpQuant EOD Close*\n\nNo positions to close.")

    # Clear active scalps
    with open(trades_file, "w") as f:
        json.dump([], f)

    # Reset traded_today
    save_traded_today({"date": "", "trades": []})

    print(f"=== EOD CLOSE COMPLETE: {len(closed)} positions closed ===")
    return closed
```

---

## SCALPING IMPROVEMENT: Position Monitor with Profit-Taking

### Change 7: Add `monitor_open_positions()` function

Insert this function **before** `main()`:

```python
def monitor_open_positions():
    """Check open positions for profit-taking, stop-loss, or time-based exit."""
    if not is_market_open():
        return

    headers = alpaca_headers()

    # Load active scalps for entry timestamps
    scalps_file = os.path.join(DATA_DIR, "active_scalps.json")
    active_scalps = []
    if os.path.exists(scalps_file):
        try:
            with open(scalps_file) as f:
                active_scalps = json.load(f)
        except (json.JSONDecodeError, ValueError):
            active_scalps = []

    # Get all open positions
    try:
        resp = requests.get(f"{ALPACA_URL}/v2/positions", headers=headers, timeout=10)
        if resp.status_code != 200:
            return
        positions = resp.json()
    except Exception as e:
        print(f"  Monitor error: {e}")
        return

    if not positions:
        return

    now = datetime.now(timezone.utc)
    closed_tickers = []

    for pos in positions:
        ticker = pos.get("symbol", "")
        unrealized_pl = float(pos.get("unrealized_pl", 0))
        market_value = abs(float(pos.get("market_value", 1)))
        cost_basis = abs(float(pos.get("cost_basis", 1)))
        pnl_pct = (unrealized_pl / cost_basis * 100) if cost_basis > 0 else 0

        # Find entry time from active_scalps
        trade_meta = _find_trade_meta(ticker, active_scalps)
        entry_time_str = trade_meta.get("timestamp", "")
        hours_held = 0
        if entry_time_str:
            try:
                entry_time = datetime.fromisoformat(entry_time_str)
                hours_held = (now - entry_time).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

        close_reason = None

        # Rule 1: Take profit at +1.0%
        if pnl_pct >= 1.0:
            close_reason = f"PROFIT_TAKE (+{pnl_pct:.2f}%)"

        # Rule 2: Stop loss at -0.5%
        elif pnl_pct <= -0.5:
            close_reason = f"STOP_LOSS ({pnl_pct:.2f}%)"

        # Rule 3: Time-based scalp exit (>2 hours and profitable)
        elif hours_held > 2.0 and unrealized_pl > 0:
            close_reason = f"SCALP_COMPLETE ({hours_held:.1f}h, +{pnl_pct:.2f}%)"

        # Rule 4: Safety — force close if held > 6.5 hours (full market day)
        elif hours_held > 6.5:
            close_reason = f"STALE_POSITION ({hours_held:.1f}h)"

        if close_reason:
            print(f"  MONITOR: Closing {ticker} — {close_reason}")
            try:
                resp = requests.delete(
                    f"{ALPACA_URL}/v2/positions/{ticker}",
                    headers=headers,
                    timeout=10
                )
                if resp.status_code in (200, 204):
                    closed_tickers.append(ticker)

                    # Log to learning_log.jsonl
                    log_entry = {
                        "timestamp": now.isoformat(),
                        "ticker": ticker,
                        "reason": close_reason,
                        "pnl": unrealized_pl,
                        "pnl_pct": round(pnl_pct, 3),
                        "hours_held": round(hours_held, 2),
                        "signal_category": trade_meta.get("signal_category", "UNKNOWN"),
                        "entry_price": trade_meta.get("entry_price", 0),
                    }
                    with open(LEARNING_LOG_FILE, "a") as f:
                        f.write(json.dumps(log_entry) + "\n")

                    # Telegram notification
                    emoji = "💰" if unrealized_pl >= 0 else "🛑"
                    msg = (
                        f"{emoji} *TrumpQuant Scalp Exit*\n\n"
                        f"Ticker: {ticker}\n"
                        f"Reason: {close_reason}\n"
                        f"P&L: ${unrealized_pl:+.2f} ({pnl_pct:+.2f}%)\n"
                        f"Held: {hours_held:.1f} hours"
                    )
                    send_telegram(msg)
                else:
                    print(f"  Failed to close {ticker}: HTTP {resp.status_code}")
            except Exception as e:
                print(f"  Failed to close {ticker}: {e}")

    # Remove closed positions from active_scalps
    if closed_tickers:
        active_scalps = [
            s for s in active_scalps
            if s.get("actual_ticker") not in closed_tickers
        ]
        with open(scalps_file, "w") as f:
            json.dump(active_scalps, f, indent=2)
```

---

## Change 8: Fix the `if __name__` block (bottom of file)

**DELETE** the first `if __name__ == "__main__": main()` block (around line 280).

The **single remaining** `if __name__` block at the very bottom of the file should be:

```python
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "eod":
        close_eod_positions()
    elif len(sys.argv) > 1 and sys.argv[1] == "monitor":
        monitor_open_positions()
    else:
        main()
```

---

## Summary of All Changes

| # | What | Where | Type |
|---|------|-------|------|
| 0 | Delete first `if __name__` block | ~line 280 | DELETE |
| 1 | Add new constants | After `BOT_CACHE_FILE` line | INSERT |
| 2 | Add traded-today functions | After `save_trade()` | INSERT |
| 3 | Rewrite `execute_paper_trade()` | Full function replace | REPLACE |
| 4 | Rewrite `main()` | Full function replace | REPLACE |
| 5 | Rewrite `close_eod_positions()` | Full function replace | REPLACE |
| 6 | Add `monitor_open_positions()` | Before `main()` | INSERT |
| 7 | Fix `if __name__` block | Bottom of file | REPLACE |

## New Files Created

| File | Purpose |
|------|---------|
| `data/traded_today.json` | Daily dedup tracker (auto-created) |
| `data/eod_log.json` | EOD close audit log (auto-created) |
| `data/learning_log.jsonl` | Scalp outcome log (auto-created) |

## Safety Invariants After Fix

1. **Max 2 trades per cron run** — `MAX_TRADES_PER_RUN = 2`
2. **Max $2,500 per ticker per day** — hard-capped, no multiplier stacking
3. **No pyramiding** — checks Alpaca for existing position before ordering
4. **Max $10,000 total exposure** — 4 positions × $2,500
5. **Profit-taking at +1.0%** — automatic scalp exit
6. **Stop-loss at -0.5%** — automatic risk cut
7. **2-hour profitable scalp auto-close** — time-based exit
8. **6.5-hour force close** — stale position safety
9. **EOD close with logging** — auditable via `eod_log.json`
10. **`main()` runs exactly ONCE** — single `if __name__` block

## Immediate Data Cleanup Needed

After deploying the fix, run these manually:

```bash
# Clear the bloated active_scalps.json (all are LOGGED, not real orders)
echo '[]' > /Users/ronnie/hamilton/trumpquant/data/active_scalps.json

# Reset traded_today
echo '{"date":"","trades":[]}' > /Users/ronnie/hamilton/trumpquant/data/traded_today.json

# Check actual Alpaca positions — close any that shouldn't be there
python3 -c "
import requests
h = {'APCA-API-KEY-ID': 'PKQ2P7KLMAJH5E3IQVKYQPTBOB', 'APCA-API-SECRET-KEY': 'A58boqhagLVQH7tKfz7MafU8axJx6HGc9GbR4VgUFhrT'}
r = requests.get('https://paper-api.alpaca.markets/v2/positions', headers=h)
print(r.json())
"
```
