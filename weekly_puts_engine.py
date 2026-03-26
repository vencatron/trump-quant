"""MarketQuant Weekly Puts Engine — Cash-Secured Puts.
Sells weekly put options on target tickers to collect premium
and get paid to wait for entry.
"""

import fcntl
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from alpaca_utils import get_headers, get_price, ALPACA_URL, ALPACA_DATA_URL

logger = logging.getLogger("trumpquant.weekly_puts_engine")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PUTS_FILE = os.path.join(DATA_DIR, "weekly_puts.json")
CACHE_FILE = os.path.join(DATA_DIR, "options_cache.json")
QUEUE_FILE = os.path.join(DATA_DIR, "telegram_queue.json")
SWING_FILE = os.path.join(DATA_DIR, "swing_positions.json")

CACHE_TTL = 900  # 15 minutes

MAX_PUTS_EXPOSURE_PCT = 0.40  # 40% of equity
MIN_PREMIUM_PCT = 0.002  # 0.2% of cash required

PUT_TARGETS = {
    "GLD":  {"entry_discount": 0.03, "max_contracts": 2, "thesis": "Gold safe haven — happy to own at 3% below"},
    "QQQ":  {"entry_discount": 0.03, "max_contracts": 1, "thesis": "Tech index — happy to own on dip"},
    "XLE":  {"entry_discount": 0.05, "max_contracts": 1, "thesis": "Energy — want to own on 5% dip"},
    "SPY":  {"entry_discount": 0.03, "max_contracts": 2, "thesis": "S&P500 — always happy to own cheaper"},
    "LMT":  {"entry_discount": 0.05, "max_contracts": 1, "thesis": "Defense — want on war escalation dip"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> list:
    """Load a JSON list file, return [] if missing or corrupt."""
    try:
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Could not load %s: %s", path, e)
    return []


def _save_json(path: str, data) -> None:
    """Atomically write JSON data to file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _queue_telegram(message: str) -> None:
    """Append a message to the Telegram send queue with file locking."""
    lock_file = QUEUE_FILE + ".lock"
    try:
        with open(lock_file, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                queue = _load_json(QUEUE_FILE)
                queue.append({
                    "message": message,
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                    "sent": False,
                })
                _save_json(QUEUE_FILE, queue)
                logger.info("Telegram queued: %s", message[:80])
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception as e:
        logger.error("Failed to queue telegram message: %s", e)


def _get_account() -> dict | None:
    """Fetch Alpaca account info."""
    try:
        resp = requests.get(f"{ALPACA_URL}/v2/account", headers=get_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error("Account fetch error: %s", e)
    return None


def _get_open_puts() -> list[dict]:
    """Return currently open puts from the puts log."""
    puts = _load_json(PUTS_FILE)
    return [p for p in puts if p.get("status") == "OPEN"]


def _total_puts_exposure() -> float:
    """Sum of cash_required across all open puts."""
    return sum(float(p.get("cash_required", 0)) for p in _get_open_puts())


# ---------------------------------------------------------------------------
# 1. get_options_chain
# ---------------------------------------------------------------------------

def get_options_chain(ticker: str, option_type: str = "put",
                      expiration_type: str = "weekly") -> list[dict]:
    """Fetch options contracts from Alpaca with 15-min cache.

    Args:
        ticker: Underlying symbol (e.g. 'SPY')
        option_type: 'put' or 'call'
        expiration_type: 'weekly' (7-14 days out)

    Returns:
        List of dicts with: symbol, strike_price, expiration_date, bid, ask, volume
    """
    now = time.time()
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, IOError):
            cache = {}

    cache_key = f"{ticker}_{option_type}_{expiration_type}"
    cached = cache.get(cache_key)
    if cached and now - cached.get("ts", 0) < CACHE_TTL:
        logger.debug("Cache hit for %s", cache_key)
        return cached["data"]

    today = datetime.now(timezone.utc).date()
    exp_gte = today
    exp_lte = today + timedelta(days=14)

    # Fetch contracts
    params = {
        "underlying_symbols": ticker,
        "expiration_date_gte": exp_gte.isoformat(),
        "expiration_date_lte": exp_lte.isoformat(),
        "type": option_type,
        "status": "active",
        "limit": 100,
    }

    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/options/contracts",
            headers=get_headers(),
            params=params,
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Options chain fetch failed for %s: HTTP %d — %s",
                           ticker, resp.status_code, resp.text[:200])
            return []
        contracts = resp.json().get("option_contracts", resp.json() if isinstance(resp.json(), list) else [])
    except Exception as e:
        logger.error("Options chain error for %s: %s", ticker, e)
        return []

    if not contracts:
        logger.info("No %s contracts found for %s", option_type, ticker)
        return []

    # Fetch snapshots for quotes (bid/ask/volume)
    symbols = [c.get("symbol", "") for c in contracts if c.get("symbol")]
    quotes = _fetch_option_snapshots(symbols)

    results = []
    for c in contracts:
        sym = c.get("symbol", "")
        snap = quotes.get(sym, {})
        latest_quote = snap.get("latestQuote", {})
        latest_trade = snap.get("latestTrade", {})

        results.append({
            "symbol": sym,
            "strike_price": float(c.get("strike_price", 0)),
            "expiration_date": c.get("expiration_date", ""),
            "bid": float(latest_quote.get("bp", 0)),
            "ask": float(latest_quote.get("ap", 0)),
            "volume": int(snap.get("dailyBar", {}).get("v", 0)
                          or latest_trade.get("s", 0)),
        })

    # Cache results
    cache[cache_key] = {"ts": now, "data": results}
    _save_json(CACHE_FILE, cache)

    logger.info("Fetched %d %s options for %s", len(results), option_type, ticker)
    return results


def _fetch_option_snapshots(symbols: list[str]) -> dict:
    """Fetch option snapshots in batches from Alpaca data API.

    Returns dict mapping symbol -> snapshot data.
    """
    all_snaps = {}
    batch_size = 20
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            resp = requests.get(
                f"{ALPACA_DATA_URL}/v1beta1/options/snapshots",
                headers=get_headers(),
                params={"symbols": ",".join(batch)},
                timeout=15,
            )
            if resp.status_code == 200:
                snaps = resp.json().get("snapshots", resp.json())
                if isinstance(snaps, dict):
                    all_snaps.update(snaps)
            else:
                logger.warning("Option snapshots batch failed: HTTP %d", resp.status_code)
        except Exception as e:
            logger.error("Option snapshots error: %s", e)
    return all_snaps


# ---------------------------------------------------------------------------
# 2. find_weekly_put
# ---------------------------------------------------------------------------

def find_weekly_put(ticker: str, current_price: float,
                    entry_discount: float) -> dict | None:
    """Find the best weekly put to sell for a given ticker.

    Targets a strike at current_price * (1 - entry_discount), picks
    the highest-premium option with volume > 10.

    Returns:
        Dict with contract details or None if nothing suitable found.
    """
    chain = get_options_chain(ticker, option_type="put")
    if not chain:
        logger.info("No put chain available for %s", ticker)
        return None

    target_strike = current_price * (1 - entry_discount)

    # Filter to 7-14 day expirations with meaningful volume
    today = datetime.now(timezone.utc).date()
    min_exp = today + timedelta(days=5)   # at least ~1 week out
    max_exp = today + timedelta(days=14)

    candidates = []
    for opt in chain:
        exp_str = opt.get("expiration_date", "")
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if not (min_exp <= exp_date <= max_exp):
            continue

        strike = opt["strike_price"]
        bid = opt["bid"]
        volume = opt["volume"]

        # Strike must be at or below target (we want OTM puts)
        if strike > target_strike * 1.02:  # small tolerance
            continue

        if bid <= 0:
            continue

        # Prefer volume > 10, but keep lower-volume as fallback
        distance = abs(strike - target_strike)
        candidates.append({**opt, "distance": distance, "exp_date": exp_date})

    if not candidates:
        logger.info("No suitable put found for %s (target strike ~%.2f)", ticker, target_strike)
        return None

    # Prefer contracts with volume > 10, then sort by premium (bid) descending
    liquid = [c for c in candidates if c["volume"] > 10]
    pool = liquid if liquid else candidates

    # Sort: closest to target strike first, then highest premium
    pool.sort(key=lambda c: (c["distance"], -c["bid"]))
    best = pool[0]

    mid = round((best["bid"] + best["ask"]) / 2, 2) if best["ask"] > 0 else best["bid"]
    cash_required = round(best["strike_price"] * 100, 2)
    discount_pct = round((1 - best["strike_price"] / current_price) * 100, 2)

    return {
        "contract_symbol": best["symbol"],
        "strike": best["strike_price"],
        "expiration": best["expiration_date"],
        "bid": best["bid"],
        "mid": mid,
        "premium_per_share": best["bid"],
        "cash_required": cash_required,
        "discount_pct": discount_pct,
    }


# ---------------------------------------------------------------------------
# 3. sell_weekly_put
# ---------------------------------------------------------------------------

def sell_weekly_put(ticker: str, contract: dict) -> dict | None:
    """Sell a cash-secured put via Alpaca.

    Args:
        ticker: Underlying ticker
        contract: Dict from find_weekly_put()

    Returns:
        Order dict on success, None on failure.
    """
    account = _get_account()
    if not account:
        logger.error("Cannot sell put — account unavailable")
        return None

    cash = float(account.get("cash", 0))
    cash_required = contract["cash_required"]

    if cash < cash_required:
        logger.warning("Insufficient cash for %s put: need $%.2f, have $%.2f",
                        ticker, cash_required, cash)
        return None

    # Submit limit sell order for put contract
    order_payload = {
        "symbol": contract["contract_symbol"],
        "qty": "1",
        "side": "sell",
        "type": "limit",
        "limit_price": str(contract["bid"]),
        "time_in_force": "day",
    }

    try:
        resp = requests.post(
            f"{ALPACA_URL}/v2/orders",
            json=order_payload,
            headers=get_headers(),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            logger.warning("Put sell order failed for %s: HTTP %d — %s",
                           ticker, resp.status_code, resp.text[:300])
            return None
        order = resp.json()
    except Exception as e:
        logger.error("Put sell order error for %s: %s", ticker, e)
        return None

    logger.info("PUT SOLD: %s %s strike $%.2f exp %s — order %s",
                ticker, contract["contract_symbol"],
                contract["strike"], contract["expiration"],
                order.get("id", "unknown"))

    # Log to weekly_puts.json
    put_record = {
        "ticker": ticker,
        "contract_symbol": contract["contract_symbol"],
        "strike": contract["strike"],
        "expiration": contract["expiration"],
        "premium_per_share": contract["premium_per_share"],
        "premium_total": round(contract["premium_per_share"] * 100, 2),
        "cash_required": cash_required,
        "discount_pct": contract["discount_pct"],
        "order_id": order.get("id", ""),
        "sold_at": datetime.now(timezone.utc).isoformat(),
        "status": "OPEN",
    }

    puts = _load_json(PUTS_FILE)
    puts.append(put_record)
    _save_json(PUTS_FILE, puts)

    # Queue telegram alert
    msg = (
        f"💰 Weekly Put SOLD\n"
        f"{ticker} ${contract['strike']} put exp {contract['expiration']}\n"
        f"Premium: ${put_record['premium_total']:.2f} | "
        f"Assigned at: ${contract['strike']} ({contract['discount_pct']:.1f}% below)\n"
        f"Cash required: ${cash_required:,.2f}"
    )
    _queue_telegram(msg)

    return order


# ---------------------------------------------------------------------------
# 4. run_weekly_puts_scan
# ---------------------------------------------------------------------------

def run_weekly_puts_scan() -> list[dict]:
    """Main entry point — scan all targets and sell optimal puts.

    Called Sunday evening or Monday morning.

    Returns:
        List of put records that were sold.
    """
    logger.info("=== Weekly Puts Scan Starting ===")

    account = _get_account()
    if not account:
        logger.error("Cannot run scan — account unavailable")
        return []

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    max_puts_cash = equity * MAX_PUTS_EXPOSURE_PCT

    current_exposure = _total_puts_exposure()
    remaining_budget = max_puts_cash - current_exposure

    logger.info("Account equity: $%.2f | Cash: $%.2f | Max puts exposure: $%.2f | "
                "Current exposure: $%.2f | Budget remaining: $%.2f",
                equity, cash, max_puts_cash, current_exposure, remaining_budget)

    # Load open puts and swing positions for skip checks
    open_puts = _get_open_puts()
    open_put_tickers = {p["ticker"] for p in open_puts}

    swing_positions = _load_json(SWING_FILE)
    swing_tickers = {
        p["ticker"] for p in swing_positions
        if p.get("status", "").upper() == "OPEN"
    }

    sold = []

    for ticker, config in PUT_TARGETS.items():
        logger.info("--- Evaluating %s ---", ticker)

        # Skip if already have an open put
        if ticker in open_put_tickers:
            logger.info("SKIP %s: already have open put", ticker)
            continue

        # Skip if holding in swing positions
        if ticker in swing_tickers:
            logger.info("SKIP %s: held in swing positions", ticker)
            continue

        # Get current price
        price = get_price(ticker)
        if not price:
            logger.warning("SKIP %s: price unavailable", ticker)
            continue

        # Find optimal put
        contract = find_weekly_put(ticker, price, config["entry_discount"])
        if not contract:
            logger.info("SKIP %s: no suitable put found at %.2f price", ticker, price)
            continue

        # Check premium threshold
        premium_pct = (contract["premium_per_share"] * 100) / contract["cash_required"]
        if premium_pct < MIN_PREMIUM_PCT:
            logger.info("SKIP %s: premium %.4f%% below minimum %.2f%%",
                        ticker, premium_pct * 100, MIN_PREMIUM_PCT * 100)
            continue

        # Check exposure budget
        if contract["cash_required"] > remaining_budget:
            logger.info("SKIP %s: cash required $%.2f exceeds remaining budget $%.2f",
                        ticker, contract["cash_required"], remaining_budget)
            continue

        # Check actual available cash
        if contract["cash_required"] > cash:
            logger.info("SKIP %s: cash required $%.2f exceeds available cash $%.2f",
                        ticker, contract["cash_required"], cash)
            continue

        # Sell the put
        order = sell_weekly_put(ticker, contract)
        if order:
            sold.append({
                "ticker": ticker,
                "strike": contract["strike"],
                "expiration": contract["expiration"],
                "premium": contract["premium_per_share"],
                "cash_required": contract["cash_required"],
            })
            remaining_budget -= contract["cash_required"]
            cash -= contract["cash_required"]
            logger.info("SOLD %s $%.2f put exp %s — premium $%.2f/share",
                        ticker, contract["strike"], contract["expiration"],
                        contract["premium_per_share"])

    # Print summary
    logger.info("=== Weekly Puts Scan Complete ===")
    if sold:
        total_premium = sum(s["premium"] * 100 for s in sold)
        total_cash = sum(s["cash_required"] for s in sold)
        logger.info("Sold %d puts | Total premium: $%.2f | Total cash secured: $%.2f",
                    len(sold), total_premium, total_cash)
        for s in sold:
            print(f"  ✓ {s['ticker']} ${s['strike']} put exp {s['expiration']} "
                  f"— premium ${s['premium'] * 100:.2f}")
    else:
        logger.info("No puts sold this scan")
        print("  No puts sold this scan")

    return sold


# ---------------------------------------------------------------------------
# 5. monitor_weekly_puts
# ---------------------------------------------------------------------------

def monitor_weekly_puts() -> None:
    """Daily check on open puts — detect expiry, assignment risk, or assignment.

    Called daily to update put statuses and queue alerts.
    """
    puts = _load_json(PUTS_FILE)
    if not puts:
        logger.info("No puts to monitor")
        return

    today = datetime.now(timezone.utc).date()
    modified = False

    for put in puts:
        if put.get("status") != "OPEN":
            continue

        ticker = put["ticker"]
        strike = float(put["strike"])
        exp_str = put.get("expiration", "")

        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("Invalid expiration date for %s: %s", ticker, exp_str)
            continue

        # Expired — mark as profit (expired worthless)
        if today > exp_date:
            put["status"] = "EXPIRED_PROFIT"
            put["closed_at"] = datetime.now(timezone.utc).isoformat()
            modified = True
            logger.info("PUT EXPIRED WORTHLESS: %s $%.2f — premium kept", ticker, strike)
            _queue_telegram(
                f"✅ Put expired worthless!\n"
                f"{ticker} ${strike} put — premium of ${put.get('premium_total', 0):.2f} kept as profit"
            )
            continue

        # Check current price for assignment risk
        price = get_price(ticker)
        if not price:
            continue

        days_to_exp = (exp_date - today).days

        # Approaching expiration and in-the-money
        if days_to_exp <= 2 and price < strike:
            logger.warning("ASSIGNMENT RISK: %s at $%.2f vs strike $%.2f (exp in %d days)",
                           ticker, price, strike, days_to_exp)
            _queue_telegram(
                f"⚠️ Put assignment likely for {ticker} at ${strike}\n"
                f"Current price: ${price:.2f} | Expires in {days_to_exp} day(s)\n"
                f"Cash needed for 100 shares: ${strike * 100:,.2f}"
            )

        # Already assigned (past expiration, ITM) — check for position
        if today >= exp_date and price < strike:
            put["status"] = "ASSIGNED"
            put["assigned_at"] = datetime.now(timezone.utc).isoformat()
            put["assigned_price"] = strike
            modified = True
            logger.info("PUT ASSIGNED: %s — 100 shares at $%.2f", ticker, strike)
            _queue_telegram(
                f"📋 Put ASSIGNED — shares acquired\n"
                f"{ticker}: 100 shares at ${strike}\n"
                f"Net cost basis: ${strike - float(put.get('premium_per_share', 0)):.2f}/share "
                f"(after premium)"
            )

    if modified:
        _save_json(PUTS_FILE, puts)


# ---------------------------------------------------------------------------
# 6. get_puts_income
# ---------------------------------------------------------------------------

def get_puts_income() -> dict:
    """Return summary of puts income and current exposure.

    Returns:
        Dict with total_premium, puts_open, puts_closed, assignment_risk, etc.
    """
    puts = _load_json(PUTS_FILE)

    total_premium = 0.0
    puts_open = 0
    puts_expired_profit = 0
    puts_assigned = 0
    open_exposure = 0.0
    at_risk = []

    today = datetime.now(timezone.utc).date()

    for put in puts:
        premium = float(put.get("premium_total", 0))
        total_premium += premium
        status = put.get("status", "")

        if status == "OPEN":
            puts_open += 1
            open_exposure += float(put.get("cash_required", 0))

            # Check if currently at risk
            price = get_price(put["ticker"])
            if price and price < float(put["strike"]):
                at_risk.append({
                    "ticker": put["ticker"],
                    "strike": put["strike"],
                    "current_price": price,
                    "expiration": put.get("expiration", ""),
                })
        elif status == "EXPIRED_PROFIT":
            puts_expired_profit += 1
        elif status == "ASSIGNED":
            puts_assigned += 1

    return {
        "total_premium_collected": round(total_premium, 2),
        "puts_open": puts_open,
        "puts_expired_profit": puts_expired_profit,
        "puts_assigned": puts_assigned,
        "open_exposure": round(open_exposure, 2),
        "assignment_risk": at_risk,
    }


# ---------------------------------------------------------------------------
# Init: ensure data files exist
# ---------------------------------------------------------------------------

def _ensure_data_files():
    """Create data files if they don't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    for path in [PUTS_FILE, CACHE_FILE]:
        if not os.path.exists(path):
            _save_json(path, [] if path == PUTS_FILE else {})

_ensure_data_files()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        run_weekly_puts_scan()
    elif cmd == "monitor":
        monitor_weekly_puts()
    elif cmd == "income":
        income = get_puts_income()
        print(json.dumps(income, indent=2))
    else:
        print(f"Usage: python {sys.argv[0]} [scan|monitor|income]")
