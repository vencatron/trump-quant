"""MarketQuant Options Engine — Covered Calls.
Sells covered calls against existing swing positions to generate income."""

import fcntl
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from alpaca_utils import get_headers, get_price

logger = logging.getLogger("trumpquant.options_engine")

ALPACA_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OPTIONS_CACHE = os.path.join(DATA_DIR, "options_cache.json")
COVERED_CALLS_FILE = os.path.join(DATA_DIR, "covered_calls.json")
OPTIONS_INCOME_FILE = os.path.join(DATA_DIR, "options_income.json")
TELEGRAM_QUEUE = os.path.join(DATA_DIR, "telegram_queue.json")
SWING_POSITIONS_FILE = os.path.join(DATA_DIR, "swing_positions.json")

CACHE_TTL_SECONDS = 15 * 60  # 15 minutes


def _ensure_data_files():
    """Create data files if they don't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(COVERED_CALLS_FILE):
        with open(COVERED_CALLS_FILE, "w") as f:
            json.dump([], f)
    if not os.path.exists(OPTIONS_INCOME_FILE):
        with open(OPTIONS_INCOME_FILE, "w") as f:
            json.dump({"total_premium": 0, "trades": []}, f)


_ensure_data_files()


def _load_json(path, default=None):
    """Load JSON file, returning default on error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else []


def _save_json(path, data):
    """Save data to JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _queue_telegram(message: str) -> None:
    """Append a message to the telegram queue with file locking to prevent races."""
    lock_file = TELEGRAM_QUEUE + ".lock"
    try:
        with open(lock_file, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                queue = _load_json(TELEGRAM_QUEUE, [])
                queue.append({
                    "message": message,
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                    "source": "options_engine",
                })
                _save_json(TELEGRAM_QUEUE, queue)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception as e:
        logger.error("Failed to queue telegram message: %s", e)


# ---------------------------------------------------------------------------
# 1. Options Chain
# ---------------------------------------------------------------------------

def get_options_chain(ticker: str, expiration_type: str = "weekly") -> list[dict]:
    """Fetch options chain for a ticker from Alpaca.

    Args:
        ticker: Underlying stock symbol.
        expiration_type: 'weekly' (default) looks 0-14 days out.

    Returns:
        List of contract dicts with symbol, strike_price, expiration_date,
        bid, ask, delta, volume.
    """
    # Check cache first
    cache = _load_json(OPTIONS_CACHE, {})
    cache_key = f"{ticker}_{expiration_type}"
    if isinstance(cache, dict) and cache_key in cache:
        cached = cache[cache_key]
        cached_at = cached.get("cached_at", 0)
        if time.time() - cached_at < CACHE_TTL_SECONDS:
            logger.debug("Options cache hit for %s", cache_key)
            return cached.get("contracts", [])

    today = datetime.now(timezone.utc).date()
    exp_gte = today.isoformat()
    exp_lte = (today + timedelta(days=14)).isoformat()

    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/options/contracts",
            headers=get_headers(),
            params={
                "underlying_symbols": ticker,
                "expiration_date_gte": exp_gte,
                "expiration_date_lte": exp_lte,
                "type": "call",
                "status": "active",
                "limit": 100,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Options chain fetch failed for %s: HTTP %d — %s",
                           ticker, resp.status_code, resp.text[:200])
            return []

        raw_contracts = resp.json()
        if isinstance(raw_contracts, dict):
            raw_contracts = raw_contracts.get("option_contracts", raw_contracts.get("contracts", []))
        if not isinstance(raw_contracts, list):
            logger.warning("Unexpected options response format for %s", ticker)
            return []

    except Exception as e:
        logger.error("Options chain error for %s: %s", ticker, e)
        return []

    # Enrich with bid/ask from snapshots
    contracts = []
    for c in raw_contracts:
        contract_symbol = c.get("symbol", "")
        strike = float(c.get("strike_price", 0))
        expiration = c.get("expiration_date", "")

        bid, ask, volume = 0.0, 0.0, 0
        try:
            snap_resp = requests.get(
                f"{ALPACA_DATA_URL}/v1beta1/options/snapshots/{contract_symbol}",
                headers=get_headers(),
                timeout=10,
            )
            if snap_resp.status_code == 200:
                snap = snap_resp.json()
                quote = snap.get("latestQuote", {})
                trade = snap.get("latestTrade", {})
                bid = float(quote.get("bp", 0))
                ask = float(quote.get("ap", 0))
                volume = int(trade.get("s", 0)) if trade else 0
                # Try greeks for delta
                greeks = snap.get("greeks", {})
                delta = float(greeks.get("delta", 0))
            else:
                delta = 0.0
        except Exception as e:
            logger.debug("Snapshot fetch failed for %s: %s", contract_symbol, e)
            delta = 0.0

        contracts.append({
            "symbol": contract_symbol,
            "strike_price": strike,
            "expiration_date": expiration,
            "bid": bid,
            "ask": ask,
            "delta": delta,
            "volume": volume,
        })

    # Update cache
    if not isinstance(cache, dict):
        cache = {}
    cache[cache_key] = {
        "cached_at": time.time(),
        "contracts": contracts,
    }
    _save_json(OPTIONS_CACHE, cache)

    logger.info("Fetched %d option contracts for %s", len(contracts), ticker)
    return contracts


# ---------------------------------------------------------------------------
# 2. Find Optimal Covered Call
# ---------------------------------------------------------------------------

def find_optimal_covered_call(
    ticker: str,
    current_price: float,
    target_premium_pct: float = 0.5,
) -> dict | None:
    """Find the best covered call to sell for a given ticker.

    Filters for 7-14 DTE, 3-5% OTM, volume > 10, then picks highest premium.

    Args:
        ticker: Underlying stock symbol.
        current_price: Current share price.
        target_premium_pct: Minimum premium as % of share price (for info only).

    Returns:
        Dict with contract details, or None if nothing suitable found.
    """
    chain = get_options_chain(ticker)
    if not chain:
        logger.info("No options chain available for %s", ticker)
        return None

    today = datetime.now(timezone.utc).date()
    otm_low = current_price * 1.03
    otm_high = current_price * 1.05

    candidates = []
    for c in chain:
        try:
            exp = datetime.strptime(c["expiration_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue

        dte = (exp - today).days
        if dte < 7 or dte > 14:
            continue

        strike = c["strike_price"]
        if strike < otm_low or strike > otm_high:
            continue

        if c["volume"] <= 10:
            continue

        bid = c["bid"]
        ask = c["ask"]
        if bid <= 0:
            continue

        mid = round((bid + ask) / 2, 2) if ask > 0 else bid
        candidates.append({**c, "mid": mid, "dte": dte})

    if not candidates:
        logger.info("No suitable covered call candidates for %s (price=%.2f)", ticker, current_price)
        return None

    # Pick highest premium (bid)
    best = max(candidates, key=lambda x: x["bid"])

    premium_per_share = best["bid"]
    annualized = (premium_per_share / current_price) * (365 / best["dte"]) * 100

    return {
        "contract_symbol": best["symbol"],
        "strike": best["strike_price"],
        "expiration": best["expiration_date"],
        "bid": best["bid"],
        "mid": best["mid"],
        "premium_per_share": premium_per_share,
        "annualized_yield_pct": round(annualized, 2),
    }


# ---------------------------------------------------------------------------
# 3. Sell Covered Call
# ---------------------------------------------------------------------------

def sell_covered_call(ticker: str, shares_held: int, contract: dict) -> dict | None:
    """Sell a covered call against held shares.

    Args:
        ticker: Underlying stock symbol.
        shares_held: Number of shares held.
        contract: Contract dict from find_optimal_covered_call().

    Returns:
        Order result dict, or None if insufficient shares or order fails.
    """
    num_contracts = shares_held // 100
    if num_contracts < 1:
        logger.info("Insufficient shares for covered call on %s (%d shares)", ticker, shares_held)
        return None

    contract_symbol = contract["contract_symbol"]
    strike = contract["strike"]
    expiration = contract["expiration"]
    premium = contract["premium_per_share"]

    # Submit sell-to-open via Alpaca options order
    payload = {
        "symbol": contract_symbol,
        "qty": str(num_contracts),
        "side": "sell",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": str(contract["bid"]),
    }

    try:
        resp = requests.post(
            f"{ALPACA_URL}/v2/orders",
            json=payload,
            headers=get_headers(),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            logger.warning("Covered call order failed for %s: HTTP %d — %s",
                           contract_symbol, resp.status_code, resp.text[:200])
            return None

        order = resp.json()
        order_id = order.get("id", "unknown")
    except Exception as e:
        logger.error("Covered call order error for %s: %s", contract_symbol, e)
        return None

    total_premium = round(premium * num_contracts * 100, 2)
    current_price = get_price(ticker) or 0
    breakeven = round(current_price + premium, 2) if current_price else strike

    # Log to covered_calls.json
    calls = _load_json(COVERED_CALLS_FILE, [])
    call_record = {
        "ticker": ticker,
        "contract_symbol": contract_symbol,
        "strike": strike,
        "expiration": expiration,
        "premium_per_share": premium,
        "num_contracts": num_contracts,
        "total_premium": total_premium,
        "order_id": order_id,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "OPEN",
    }
    calls.append(call_record)
    _save_json(COVERED_CALLS_FILE, calls)

    # Update income tracker
    income = _load_json(OPTIONS_INCOME_FILE, {"total_premium": 0, "trades": []})
    income["total_premium"] = round(income.get("total_premium", 0) + total_premium, 2)
    income["trades"].append({
        "ticker": ticker,
        "contract_symbol": contract_symbol,
        "premium": total_premium,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    _save_json(OPTIONS_INCOME_FILE, income)

    # Queue telegram alert
    _queue_telegram(
        f"\U0001F4DE Covered Call SOLD\n"
        f"{ticker} ${strike} call exp {expiration}\n"
        f"Premium: ${premium}/share = ${total_premium}\n"
        f"Breakeven: ${breakeven}"
    )

    logger.info("COVERED CALL SOLD: %s %dx %s $%.2f strike, $%.2f premium",
                ticker, num_contracts, contract_symbol, strike, total_premium)

    return {
        "order_id": order_id,
        "ticker": ticker,
        "contract_symbol": contract_symbol,
        "strike": strike,
        "expiration": expiration,
        "num_contracts": num_contracts,
        "premium_per_share": premium,
        "total_premium": total_premium,
    }


# ---------------------------------------------------------------------------
# 4. Monitor Covered Calls
# ---------------------------------------------------------------------------

def monitor_covered_calls():
    """Check open covered calls for expiry or assignment.

    Expired worthless -> log profit. Assigned -> log P&L including premium.
    Writes daily summary to telegram queue.
    """
    calls = _load_json(COVERED_CALLS_FILE, [])
    if not calls:
        return

    today = datetime.now(timezone.utc).date()
    updated = False
    summary_lines = []

    for call in calls:
        if call.get("status") != "OPEN":
            continue

        try:
            exp_date = datetime.strptime(call["expiration"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue

        if exp_date > today:
            # Still active — check current option price
            continue

        ticker = call["ticker"]
        strike = call["strike"]
        premium = call["total_premium"]
        current_price = get_price(ticker)

        if current_price is None:
            logger.warning("Cannot get price for %s to evaluate expiry", ticker)
            continue

        if current_price < strike:
            # Expired worthless — full profit
            call["status"] = "EXPIRED_WORTHLESS"
            call["closed_at"] = datetime.now(timezone.utc).isoformat()
            call["realized_pnl"] = premium
            summary_lines.append(
                f"  {ticker} ${strike}c EXPIRED worthless — kept ${premium} premium"
            )
            logger.info("Covered call EXPIRED WORTHLESS: %s $%.2f — profit $%.2f",
                        ticker, strike, premium)
        else:
            # Assigned — shares called away at strike
            call["status"] = "ASSIGNED"
            call["closed_at"] = datetime.now(timezone.utc).isoformat()
            call["realized_pnl"] = premium  # Premium is always kept
            call["assignment_price"] = strike
            summary_lines.append(
                f"  {ticker} ${strike}c ASSIGNED — shares sold at ${strike}, kept ${premium} premium"
            )
            logger.info("Covered call ASSIGNED: %s at $%.2f — premium $%.2f kept",
                        ticker, strike, premium)

        updated = True

    if updated:
        _save_json(COVERED_CALLS_FILE, calls)

    # Daily summary
    open_calls = [c for c in calls if c.get("status") == "OPEN"]
    if summary_lines or open_calls:
        summary = "\U0001F4CA Covered Calls Daily Summary\n"
        if summary_lines:
            summary += "Closed:\n" + "\n".join(summary_lines) + "\n"
        if open_calls:
            summary += f"Open positions: {len(open_calls)}\n"
            for oc in open_calls:
                summary += f"  {oc['ticker']} ${oc['strike']}c exp {oc['expiration']}\n"
        _queue_telegram(summary)


# ---------------------------------------------------------------------------
# 5. Covered Call Income Summary
# ---------------------------------------------------------------------------

def get_covered_call_income() -> dict:
    """Return income summary from covered call activity.

    Returns:
        Dict with total_premium, positions_open, annualized_yield.
    """
    income = _load_json(OPTIONS_INCOME_FILE, {"total_premium": 0, "trades": []})
    calls = _load_json(COVERED_CALLS_FILE, [])

    open_positions = [c for c in calls if c.get("status") == "OPEN"]
    total_premium = income.get("total_premium", 0)
    num_trades = len(income.get("trades", []))

    # Annualized yield estimate based on trade history
    trades = income.get("trades", [])
    if trades:
        first_trade = trades[0].get("date", "")
        try:
            first_date = datetime.strptime(first_trade, "%Y-%m-%d").date()
            days_active = max((datetime.now(timezone.utc).date() - first_date).days, 1)
            annualized_yield = (total_premium / days_active) * 365
        except (ValueError, TypeError):
            annualized_yield = 0
    else:
        annualized_yield = 0

    return {
        "total_premium": round(total_premium, 2),
        "positions_open": len(open_positions),
        "total_trades": num_trades,
        "annualized_premium": round(annualized_yield, 2),
        "open_positions": [
            {
                "ticker": c["ticker"],
                "strike": c["strike"],
                "expiration": c["expiration"],
                "premium": c["total_premium"],
            }
            for c in open_positions
        ],
    }


# ---------------------------------------------------------------------------
# 6. Integrate with Swing Positions
# ---------------------------------------------------------------------------

def integrate_with_swing():
    """Scan swing positions and sell covered calls where eligible.

    Rules:
    - Only long positions with >= 100 shares
    - Premium must be >= 0.3% of position value
    - Max 1 covered call per underlying (no stacking)
    """
    positions = _load_json(SWING_POSITIONS_FILE, [])
    existing_calls = _load_json(COVERED_CALLS_FILE, [])

    # Tickers that already have an open covered call
    tickers_with_calls = {
        c["ticker"] for c in existing_calls if c.get("status") == "OPEN"
    }

    for pos in positions:
        if pos.get("status") != "OPEN":
            continue
        if pos.get("direction", "").upper() != "BUY":
            continue

        ticker = pos["ticker"]
        shares = pos.get("shares", 0)

        if shares < 100:
            logger.debug("Skipping %s — only %d shares (need 100)", ticker, shares)
            continue

        if ticker in tickers_with_calls:
            logger.debug("Skipping %s — already has open covered call", ticker)
            continue

        current_price = get_price(ticker)
        if not current_price:
            logger.warning("Cannot get price for %s, skipping covered call scan", ticker)
            continue

        contract = find_optimal_covered_call(ticker, current_price)
        if not contract:
            logger.info("No suitable covered call found for %s", ticker)
            continue

        # Check minimum premium threshold: 0.3% of position value
        position_value = current_price * shares
        min_premium = position_value * 0.003
        num_contracts = shares // 100
        total_premium = contract["premium_per_share"] * num_contracts * 100

        if total_premium < min_premium:
            logger.info("Premium too low for %s: $%.2f < $%.2f min (0.3%%)",
                        ticker, total_premium, min_premium)
            continue

        result = sell_covered_call(ticker, shares, contract)
        if result:
            logger.info("Covered call placed for swing position %s", ticker)
        else:
            logger.warning("Failed to place covered call for %s", ticker)
