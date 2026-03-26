# REFACTOR_NOTES.md — Grumpy Senior Dev Pass

**Date:** 2026-03-26  
**Scope:** MarketQuant / TrumpQuant trading system  
**Status:** All 136 tests passing ✅

---

## Critical Bugs Fixed

### 1. swing_engine.py — Broken f-strings in ALL log messages
**Bug:** Every single `logger.info(...)` and `logger.warning(...)` call in
`open_swing_position()` and `monitor_swing_positions()` used `{variable}` syntax
inside a plain string literal (NOT an f-string). The logs printed literally
`{ticker}`, `{direction}`, `{pnl_dollars}` instead of actual values.

**Severity:** CRITICAL — 9:31am you have no idea what's happening.

**Fix:** Converted all to proper `logger.info("... %s", value)` format.

### 2. daily_email_report.py — Hardcoded API keys as fallback defaults
**Bug:** `ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "PKQ2P7KLMAJH5E3IQVKYQPTBOB")`
— live API keys were baked in as default fallback values. Anyone reading the
source code (or a git log) gets free access to the trading account.

**Severity:** CRITICAL SECURITY — real API keys in source code.

**Fix:** Removed hardcoded defaults. Keys must come from environment or fail with
a clear warning. Added `from alpaca_utils import get_headers` — use the single
shared auth mechanism.

### 3. signal_check.py — DST-broken after-hours gate
**Bug:** `datetime.now(timezone(timedelta(hours=-4)))` uses a fixed -4 UTC offset.
This is EDT. During EST (November–March), the real offset is -5. The after-hours
gate was off by 1 hour for half the year, potentially executing trades in
pre-market or post-market.

**Severity:** HIGH — wrong timezone = trades executing at wrong times.

**Fix:** `datetime.now(ZoneInfo("America/New_York"))` — proper DST-aware Eastern
time. Also fixed the market-open check to use the same.

### 4. signal_check.py — Duplicate Alpaca auth/order/price functions
**Bug:** `signal_check.py` defined its own `alpaca_headers()`, `get_current_price()`,
and `submit_alpaca_order()` — completely separate from `alpaca_utils.py`. Three
copies of auth code = maintenance nightmare. A key rotation means updating 3 files.

**Fix:** Removed all three duplicate functions. Replaced with imports from
`alpaca_utils`: `get_headers`, `alpaca_get_price`, `alpaca_submit_order`. Public
wrapper names kept for backward compatibility.

### 5. Telegram queue race condition
**Bug:** `send_telegram()`, `options_engine._queue_telegram()`, and
`weekly_puts_engine._queue_telegram()` all wrote to the same
`telegram_queue.json` without any file locking. Multiple cron jobs running
simultaneously (signal scan every 15min + monitor every 5min) could corrupt
the queue — read-modify-write with no atomicity.

**Fix:** All three functions now use `fcntl.flock(LOCK_EX)` around the
read-modify-write cycle. Writes use atomic `os.replace(tmp, dest)`.

---

## Reliability Fixes

### 6. signal_check.py — Bare `except:` swallowing all errors
**Bug:** Multiple `except: pass` blocks throughout `get_total_exposure()`,
`get_current_price()`, `submit_alpaca_order()`, and EOD functions. Silent
failures mean you don't know your position fetch failed.

**Fix:** Replaced with `except Exception as e: logger.error(...)`. Errors are
now logged with context.

### 7. daily_email_report.py — Bare `except:` everywhere
**Bug:** `get_account()`, `get_positions()`, `get_today_orders()`,
`get_today_pnl()`, `get_swing_positions()` all had `except: pass` — zero
visibility into failures.

**Fix:** All replaced with typed exceptions and `logger.error()` calls.

### 8. swing_engine.py — Non-atomic file writes
**Bug:** `save_swing_positions()` and `save_swing_trailing_stops()` wrote directly
to the target file. A crash mid-write leaves a corrupt JSON file; next load
silently resets to `[]`, losing all position state.

**Fix:** Write to `.tmp` file first, then `os.replace()` for atomic swap.

### 9. dead `get_iran_context()` in daily_email_report.py
**Bug:** Function existed, called `subprocess.run` to run a nested subprocess,
but was never called from `send_daily_report()`. Dead code with a security smell
(subprocess calling subprocess).

**Fix:** Removed.

---

## Architecture Notes (Not Fixed — Documented)

### 10. regime_detector.py — Position multiplier is dead code
**Status:** DOCUMENTED, not breaking-changed yet.

`detect_regime()` calculates `recommended_position_multiplier` (0.5x, 1.0x,
or 1.5x) based on VIX + volatility + post correlation. `signal_check.py`
calls `detect_regime()` to refresh the file, but **never reads the multiplier
back and never applies it to position sizing**. The regime system does nothing
except record a JSON file.

`get_regime_multiplier()` is never called from the trading path.

**Recommendation:** If you want regime-aware position sizing, add this to
`execute_paper_trade()`:
```python
from regime_detector import get_regime_multiplier
regime_mult = get_regime_multiplier()
adjusted_size = min(MAX_PER_TICKER_DAILY, int(MAX_PER_TICKER_DAILY * regime_mult))
```
But this would multiply positions up to $3,750 in HIGH_SENSITIVITY — make sure
that's intended before enabling.

### 11. learning_engine.py — record_outcome() IS being called
Confirmed: called from `monitor_open_positions()` (line ~967) and
`close_eod_positions()` (line ~1307). Learning engine is wired correctly.

### 12. options_engine.integrate_with_swing() IS being called
Confirmed: called from `swing_engine.open_swing_position()` when direction is
BUY. Works end-to-end.

### 13. weekly_puts_engine — 40% cash cap IS enforced
Confirmed: `MAX_PUTS_EXPOSURE_PCT = 0.40` applied at:
```python
max_puts_cash = equity * MAX_PUTS_EXPOSURE_PCT
remaining_budget = max_puts_cash - current_exposure
if contract["cash_required"] > remaining_budget: continue
```
Cap is correctly enforced.

---

## What Was NOT Changed

- Trading signal logic and signal thresholds (not in scope)
- Test files (all tests pass as-is after refactoring)
- botdetector/ package (separate module, not in scope)
- Data files in data/
- HTML template in daily_email_report.py (functional, just ugly)

---

## Final Test Results

```
136 passed, 1 warning in 2.93s
```

All tests pass. The warning is a third-party websockets deprecation notice,
not our code.
