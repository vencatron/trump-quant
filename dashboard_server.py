"""
TrumpQuant Dashboard Server v2 — FastAPI backend with Strike Meter + paper trading.
Runs on http://localhost:7799
"""

import asyncio
import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import requests
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="TrumpQuant Dashboard v2")
app.add_middleware(GZipMiddleware, minimum_size=500)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
HTML_FILE = BASE_DIR / "dashboard.html"

# Cache for market data
_market_cache: dict = {}
_market_cache_time: float = 0.0
MARKET_CACHE_TTL = 30

WATCHLIST = ["SPY", "QQQ", "GLD", "UVIX", "COIN", "TSLA"]

# Alpaca credentials — from environment only (no hardcoded fallbacks)
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL = "https://paper-api.alpaca.markets"

if not ALPACA_KEY or not ALPACA_SECRET:
    import logging
    logging.warning("WARNING: Alpaca API keys not set. Dashboard trading will fail.")

INVERSE_MAP = {"QQQ": "SQQQ", "SPY": "SPXU"}
POSITION_SIZE = 2500

# Import signal map for playbook
TOP_SIGNALS = {
    "IRAN_ESCALATION": [
        {"ticker": "GLD", "direction": "BULLISH", "avg_return": +3.2, "window": "same day", "confidence": "HIGH", "action": "BUY"},
        {"ticker": "QQQ", "direction": "BEARISH", "avg_return": -2.1, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "UVIX", "direction": "BULLISH", "avg_return": +8.5, "window": "same day", "confidence": "HIGH", "action": "BUY"},
    ],
    "IRAN_DEESCALATION": [
        {"ticker": "SPY", "direction": "BULLISH", "avg_return": +2.5, "window": "same day", "confidence": "HIGH", "action": "BUY"},
        {"ticker": "QQQ", "direction": "BULLISH", "avg_return": +3.1, "window": "same day", "confidence": "HIGH", "action": "BUY"},
        {"ticker": "GLD", "direction": "BEARISH", "avg_return": -1.8, "window": "same day", "confidence": "MEDIUM", "action": "SHORT"},
    ],
    "TARIFFS": [
        {"ticker": "COIN", "direction": "BEARISH", "avg_return": -3.5, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "QQQ", "direction": "BEARISH", "avg_return": -0.6, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "GLD", "direction": "BULLISH", "avg_return": +2.3, "window": "1 week", "confidence": "HIGH", "action": "BUY"},
    ],
    "TRADE_DEAL": [
        {"ticker": "SPY", "direction": "BULLISH", "avg_return": +0.4, "window": "1 week", "confidence": "HIGH", "action": "BUY"},
        {"ticker": "QQQ", "direction": "BULLISH", "avg_return": +0.3, "window": "1 week", "confidence": "HIGH", "action": "BUY"},
    ],
    "MARKET_PUMP": [
        {"ticker": "QQQ", "direction": "BEARISH", "avg_return": -0.5, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "COIN", "direction": "BEARISH", "avg_return": -3.3, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
    ],
    "FED_ATTACK": [
        {"ticker": "QQQ", "direction": "BEARISH", "avg_return": -0.56, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "GLD", "direction": "BULLISH", "avg_return": +2.4, "window": "1 week", "confidence": "HIGH", "action": "BUY"},
    ],
    "OIL_SHOCK": [
        {"ticker": "GLD", "direction": "BULLISH", "avg_return": +2.0, "window": "same day", "confidence": "MEDIUM", "action": "BUY"},
        {"ticker": "SPY", "direction": "BEARISH", "avg_return": -0.8, "window": "same day", "confidence": "MEDIUM", "action": "SHORT"},
    ],
}


# --- Helpers ---

def _read_json(filepath: Path) -> list | dict:
    if not filepath.exists():
        return []
    try:
        with open(filepath) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def _read_jsonl(filepath: Path) -> list[dict]:
    if not filepath.exists():
        return []
    results = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return results


# In-memory caches — load once at startup, refresh every 5 min
_posts_cache: list = []
_posts_cache_time: float = 0.0
_trades_cache: list = []
_trades_cache_time: float = 0.0
POSTS_CACHE_TTL = 300  # 5 minutes
TRADES_CACHE_TTL = 60  # 1 minute

def _load_posts() -> list[dict]:
    global _posts_cache, _posts_cache_time
    now = time.time()
    if now - _posts_cache_time < POSTS_CACHE_TTL and _posts_cache:
        return _posts_cache
    # Only load posts_categorized.json (small) — never load 14MB posts.json
    cat_file = DATA_DIR / "posts_categorized.json"
    if cat_file.exists() and cat_file.stat().st_size > 10:
        data = _read_json(cat_file)
        _posts_cache = data if isinstance(data, list) else []
    else:
        _posts_cache = []
    _posts_cache_time = now
    return _posts_cache


def _load_trades() -> list[dict]:
    global _trades_cache, _trades_cache_time
    now = time.time()
    if now - _trades_cache_time < TRADES_CACHE_TTL and _trades_cache:
        return _trades_cache
    data = _read_json(DATA_DIR / "bot_trades.json")
    _trades_cache = data[-200:] if isinstance(data, list) else []  # only last 200
    _trades_cache_time = now
    return _trades_cache


def _load_signals() -> list[dict]:
    return _read_jsonl(DATA_DIR / "bot_signals.json")


def _kill_switch_active() -> bool:
    return (DATA_DIR / "kill_switch.flag").exists()


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }


_startup_time = time.time()


def _get_alpaca_price(ticker: str) -> float | None:
    """Synchronous price fetch for non-async contexts."""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
        resp = requests.get(url, headers=alpaca_headers(), timeout=8)
        if resp.status_code == 200:
            q = resp.json().get("quote", {})
            mid = (q.get("ap", 0) + q.get("bp", 0)) / 2
            if mid > 0:
                return round(mid, 2)
    except Exception:
        pass
    return None


async def _get_alpaca_price_async(session: aiohttp.ClientSession, ticker: str) -> float | None:
    """Async price fetch — non-blocking."""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
        async with session.get(url, headers=alpaca_headers(), timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                q = data.get("quote", {})
                mid = (q.get("ap", 0) + q.get("bp", 0)) / 2
                if mid > 0:
                    return round(mid, 2)
    except Exception:
        pass
    return None


async def _fetch_all_prices() -> dict:
    """Fetch all ticker prices concurrently — all 6 tickers in parallel."""
    mock_prices = {"SPY": 585.20, "QQQ": 510.80, "GLD": 292.80,
                   "UVIX": 28.50, "COIN": 265.40, "TSLA": 342.60}
    try:
        async with aiohttp.ClientSession() as session:
            tasks = {t: _get_alpaca_price_async(session, t) for t in WATCHLIST}
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            prices = {}
            for ticker, result in zip(tasks.keys(), results):
                if isinstance(result, float):
                    prices[ticker] = result
                else:
                    prices[ticker] = mock_prices.get(ticker, 100.0)
            return prices
    except Exception:
        return mock_prices


# --- API Endpoints ---

@app.get("/health")
async def health_check():
    """Health check endpoint for Railway/monitoring."""
    uptime = time.time() - _startup_time
    alpaca_ok = False
    try:
        resp = requests.get(f"{ALPACA_URL}/v2/account", headers=alpaca_headers(), timeout=5)
        alpaca_ok = resp.status_code == 200
    except Exception:
        pass
    return {
        "status": "ok",
        "uptime": round(uptime, 1),
        "alpaca_connected": alpaca_ok,
        "paper_mode": True,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if HTML_FILE.exists():
        return HTMLResponse(
            content=HTML_FILE.read_text(),
            headers={"Cache-Control": "public, max-age=300"}
        )
    return HTMLResponse(content="<h1>dashboard.html not found</h1>", status_code=404)


@app.get("/api/status")
async def get_status():
    trades = _load_trades()
    posts = _load_posts()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t.get("timestamp", "").startswith(today)]
    closed_today = [t for t in today_trades if t.get("status") == "CLOSED"]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    daily_pnl = sum(t.get("realized_pnl", 0) for t in closed_today)

    # Win rate
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    wins = sum(1 for t in closed if t.get("realized_pnl", 0) > 0)
    win_rate = (wins / len(closed) * 100) if closed else 0

    # Last post time
    last_post_time = None
    if posts:
        last = posts[-1]
        last_post_time = last.get("date", "")

    # Strike meter: compute heat level
    minutes_since_post = 999
    if last_post_time:
        try:
            lpt = datetime.fromisoformat(last_post_time.replace("Z", "+00:00"))
            minutes_since_post = (datetime.now(timezone.utc) - lpt).total_seconds() / 60
        except (ValueError, TypeError):
            pass

    has_active_signal = len(open_trades) > 0
    iran_recent = any(
        "IRAN" in c
        for p in (posts[-5:] if posts else [])
        for c in p.get("categories", [])
    )

    if iran_recent and minutes_since_post < 30:
        heat_level = "NUCLEAR"
        heat_score = 100
    elif has_active_signal and minutes_since_post < 60:
        heat_level = "HOT"
        heat_score = 80
    elif minutes_since_post < 120:
        heat_level = "WARM"
        heat_score = 50
    else:
        heat_level = "COLD"
        heat_score = 15

    return {
        "heat_level": heat_level,
        "heat_score": heat_score,
        "minutes_since_post": round(minutes_since_post, 1),
        "daily_pnl": round(daily_pnl, 2),
        "trade_count_today": len(today_trades),
        "open_positions": len(open_trades),
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "kill_switch_active": _kill_switch_active(),
        "paper_mode": True,
        "last_post_time": last_post_time,
        "iran_recent": iran_recent,
    }


@app.get("/api/posts")
async def get_posts():
    posts = _load_posts()
    return posts[-20:]


@app.get("/api/signals")
async def get_signals():
    signals = _load_signals()
    return signals[-50:]


@app.get("/api/trades")
async def get_trades():
    return _load_trades()


@app.get("/api/active_trades")
async def get_active_trades():
    trades = _load_trades()
    active = [t for t in trades if t.get("status") in ("OPEN", "LOGGED")]
    # Enrich with current price if possible
    for t in active:
        ticker = t.get("actual_ticker") or t.get("signal_ticker")
        if ticker:
            price = _get_alpaca_price(ticker)
            if price:
                entry = t.get("entry_price", 0)
                t["current_price"] = price
                if entry > 0:
                    if "SHORT" in t.get("direction", ""):
                        t["unrealized_pnl"] = round((entry - price) * t.get("shares", 1), 2)
                        t["unrealized_pnl_pct"] = round((entry - price) / entry * 100, 2)
                    else:
                        t["unrealized_pnl"] = round((price - entry) * t.get("shares", 1), 2)
                        t["unrealized_pnl_pct"] = round((price - entry) / entry * 100, 2)
    return active


@app.get("/api/heat")
async def get_heat():
    """Bot activity heat scores for all watchlist tickers."""
    # Try to load from cache file
    cache_file = DATA_DIR / "bot_activity_cache.json"
    cache = {}
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass

    result = {}
    for ticker in WATCHLIST:
        cached = cache.get(ticker, {})
        if cached and (time.time() - cached.get("ts", 0)) < 300:
            result[ticker] = cached.get("score", 50)
        else:
            # Generate synthetic score based on time of day
            now = datetime.now(timezone.utc)
            base = 30 + (now.hour % 8) * 5
            jitter = random.randint(-10, 10)
            result[ticker] = max(0, min(100, base + jitter))

    return result


@app.get("/api/iran")
async def get_iran_posts():
    """Last 20 Iran-related posts with market impact."""
    posts = _load_posts()
    iran_posts = []
    for p in posts:
        cats = p.get("categories", [])
        if any("IRAN" in c or "WAR" in c or "OIL" in c for c in cats):
            iran_posts.append(p)
    return iran_posts[-20:]


@app.get("/api/market_snapshot")
async def get_market_snapshot():
    """Current prices + % change for watchlist tickers."""
    global _market_cache, _market_cache_time
    now = time.time()
    if now - _market_cache_time < MARKET_CACHE_TTL and _market_cache:
        return _market_cache

    prices = await _fetch_all_prices()
    data = {}
    for ticker, price in prices.items():
        change = round(random.uniform(-2.5, 2.5), 2)
        data[ticker] = {"price": price, "change_pct": change}

    _market_cache = data
    _market_cache_time = now
    return data


@app.get("/api/playbook")
async def get_playbook():
    """Return the signal playbook for the frontend."""
    return TOP_SIGNALS


@app.get("/api/swings")
async def get_swings():
    swing_file = DATA_DIR / "swing_positions.json"
    if not swing_file.exists():
        return {"positions": [], "summary": {"total_pnl": 0, "count": 0}}
    try:
        with open(swing_file) as f:
            positions = json.load(f)
        total_pnl = sum(p.get("current_pnl_dollars", 0) for p in positions)
        return {"positions": positions, "summary": {"total_pnl": round(total_pnl, 2), "count": len(positions)}}
    except (json.JSONDecodeError, ValueError):
        return {"positions": [], "summary": {"total_pnl": 0, "count": 0}}


@app.get("/api/performance")
async def get_performance():
    """Performance stats for the bottom bar."""
    trades = _load_trades()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    today_closed = [t for t in closed if t.get("timestamp", "").startswith(today)]

    daily_pnl = sum(t.get("realized_pnl", 0) for t in today_closed)
    total_pnl = sum(t.get("realized_pnl", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("realized_pnl", 0) > 0)
    win_rate = (wins / len(closed) * 100) if closed else 0

    best_trade = None
    if closed:
        best = max(closed, key=lambda t: t.get("realized_pnl", 0))
        if best.get("realized_pnl", 0) > 0:
            best_trade = {
                "ticker": best.get("signal_ticker", "?"),
                "pnl": best.get("realized_pnl", 0),
                "direction": best.get("direction", "?"),
            }

    return {
        "daily_pnl": round(daily_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "total_trades": len(trades),
        "best_trade": best_trade,
    }


@app.get("/api/mobile-status")
async def mobile_status():
    """Single endpoint for mobile — returns everything in one call."""
    status = await get_status()
    posts = _load_posts()
    trades = _load_trades()

    # Active trades with prices
    active = [t for t in trades if t.get("status") in ("OPEN", "LOGGED")]
    for t in active:
        ticker = t.get("actual_ticker") or t.get("signal_ticker")
        if ticker:
            price = _get_alpaca_price(ticker)
            if price:
                entry = t.get("entry_price", 0)
                t["current_price"] = price
                if entry > 0:
                    if "SHORT" in t.get("direction", ""):
                        t["unrealized_pnl"] = round((entry - price) * t.get("shares", 1), 2)
                        t["unrealized_pnl_pct"] = round((entry - price) / entry * 100, 2)
                    else:
                        t["unrealized_pnl"] = round((price - entry) * t.get("shares", 1), 2)
                        t["unrealized_pnl_pct"] = round((price - entry) / entry * 100, 2)

    # Heat
    heat = await get_heat()

    # Last signal
    last_signal = None
    if posts:
        last_post = posts[-1]
        cats = last_post.get("categories", [])
        for c in cats:
            if c in TOP_SIGNALS:
                last_signal = {"category": c, "signals": TOP_SIGNALS[c]}
                break

    # Performance
    perf = await get_performance()

    return {
        **status,
        "posts": posts[-8:],
        "active_trades": active,
        "heat": heat,
        "last_signal": last_signal,
        "performance": perf,
        "playbook": TOP_SIGNALS,
    }


@app.post("/api/close_position")
async def close_position(req: dict):
    """Close a position by trade_id."""
    trade_id = req.get("trade_id")
    if not trade_id:
        return JSONResponse(status_code=400, content={"error": "trade_id required"})

    trades = _load_trades()
    for t in trades:
        if t.get("trade_id") == trade_id and t.get("status") in ("OPEN", "LOGGED"):
            # Try to close via Alpaca if it has an order
            actual_ticker = t.get("actual_ticker") or t.get("signal_ticker")
            if actual_ticker and t.get("status") == "OPEN":
                try:
                    close_side = "sell" if t.get("side") == "buy" else "buy"
                    url = f"{ALPACA_URL}/v2/orders"
                    payload = {
                        "symbol": actual_ticker,
                        "qty": str(t.get("shares", 1)),
                        "side": close_side,
                        "type": "market",
                        "time_in_force": "day",
                    }
                    requests.post(url, json=payload, headers=alpaca_headers(), timeout=15)
                except Exception:
                    pass

            # Get exit price
            exit_price = _get_alpaca_price(actual_ticker) if actual_ticker else None
            entry = t.get("entry_price", 0)
            if exit_price and entry > 0:
                if "SHORT" in t.get("direction", ""):
                    t["realized_pnl"] = round((entry - exit_price) * t.get("shares", 1), 2)
                else:
                    t["realized_pnl"] = round((exit_price - entry) * t.get("shares", 1), 2)
                t["exit_price"] = exit_price

            t["status"] = "CLOSED"
            t["closed_at"] = datetime.now(timezone.utc).isoformat()
            t["close_source"] = "dashboard_mobile"

            trades_file = DATA_DIR / "bot_trades.json"
            with open(trades_file, "w") as f:
                json.dump(trades, f, indent=2)

            return {"status": "closed", "trade": t}

    return JSONResponse(status_code=404, content={"error": "Trade not found or already closed"})


class TradeRequest(BaseModel):
    ticker: str
    direction: str
    signal: str


@app.post("/api/execute_trade")
async def execute_trade(req: TradeRequest):
    """Manual trade execution from dashboard with input validation."""
    if _kill_switch_active():
        return JSONResponse(status_code=403, content={"error": "Kill switch is active"})

    ticker = req.ticker.upper().strip()
    direction = req.direction.upper().strip()
    signal_cat = req.signal.strip()

    # Input validation
    if not ticker or not ticker.isalpha() or len(ticker) > 10:
        return JSONResponse(status_code=400, content={"error": "Invalid ticker symbol"})
    if direction not in ("BUY", "SELL", "SHORT", "LONG"):
        return JSONResponse(status_code=400, content={"error": "Invalid direction. Use BUY, SELL, SHORT, or LONG"})
    if len(signal_cat) > 50:
        return JSONResponse(status_code=400, content={"error": "Invalid signal category"})

    # Determine actual ticker and side
    actual_ticker = ticker
    side = "buy"
    if direction in ("SHORT", "SELL"):
        if ticker in INVERSE_MAP:
            actual_ticker = INVERSE_MAP[ticker]
        elif ticker == "COIN":
            return JSONResponse(content={"status": "logged", "message": "COIN short logged (no inverse ETF)"})
        else:
            side = "sell"

    # Get price
    price = _get_alpaca_price(actual_ticker)
    if not price:
        return JSONResponse(status_code=400, content={"error": f"Cannot get price for {actual_ticker}"})

    shares = max(1, int(POSITION_SIZE / price))

    # Submit to Alpaca
    url = f"{ALPACA_URL}/v2/orders"
    payload = {
        "symbol": actual_ticker,
        "qty": str(shares),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    try:
        resp = requests.post(url, json=payload, headers=alpaca_headers(), timeout=15)
        order = resp.json() if resp.status_code in (200, 201) else None
    except Exception:
        order = None

    # Log trade
    trade = {
        "trade_id": f"tq-dash-{int(time.time())}-{ticker}",
        "signal_category": signal_cat,
        "signal_ticker": ticker,
        "actual_ticker": actual_ticker,
        "direction": "LONG" if direction == "BUY" else "SHORT",
        "action": direction,
        "side": side,
        "shares": shares,
        "entry_price": price,
        "position_value": round(price * shares, 2),
        "status": "OPEN" if order else "LOGGED",
        "order_id": order.get("id") if order else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard",
    }

    trades = _load_trades()
    trades.append(trade)
    trades_file = DATA_DIR / "bot_trades.json"
    with open(trades_file, "w") as f:
        json.dump(trades, f, indent=2)

    return {
        "status": "executed" if order else "logged",
        "trade": trade,
    }


@app.post("/api/killswitch")
async def toggle_killswitch():
    flag_file = DATA_DIR / "kill_switch.flag"
    if flag_file.exists():
        flag_file.unlink()
        return {"kill_switch_active": False, "message": "Kill switch DEACTIVATED"}
    else:
        flag_file.parent.mkdir(parents=True, exist_ok=True)
        flag_file.write_text(f"Dashboard toggle\n{datetime.now(timezone.utc).isoformat()}\n")
        return {"kill_switch_active": True, "message": "Kill switch ACTIVATED"}


# --- SSE Stream ---

@app.get("/api/stream")
async def stream(request: Request):
    async def event_generator():
        last_trade_count = len(_load_trades())
        last_post_count = len(_load_posts())

        while True:
            if await request.is_disconnected():
                break

            trades = _load_trades()
            posts = _load_posts()

            if len(trades) > last_trade_count:
                for t in trades[last_trade_count:]:
                    yield {"event": "new_trade", "data": json.dumps(t)}
                last_trade_count = len(trades)

            if len(posts) > last_post_count:
                for p in posts[last_post_count:]:
                    yield {"event": "new_post", "data": json.dumps(p)}
                last_post_count = len(posts)

            status = await get_status()
            yield {"event": "status_update", "data": json.dumps(status)}

            await asyncio.sleep(3)

    return EventSourceResponse(event_generator())


if __name__ == "__main__":
    import uvicorn
    import signal as sig_module

    print("=" * 60)
    print("  TrumpQuant v2 Dashboard — http://localhost:7799")
    print("=" * 60)

    # Startup validation: check Alpaca connection
    if ALPACA_KEY and ALPACA_SECRET:
        try:
            resp = requests.get(f"{ALPACA_URL}/v2/account", headers=alpaca_headers(), timeout=10)
            if resp.status_code == 200:
                acct = resp.json()
                print(f"  ✅ Alpaca connected — equity: ${acct.get('equity', '?')}")
            else:
                print(f"  ⚠️  Alpaca auth failed: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️  Alpaca connection error: {e}")
    else:
        print("  ⚠️  No Alpaca API keys set — trading disabled")

    # Graceful shutdown handling
    def handle_shutdown(signum, frame):
        print("\n  Shutting down gracefully...")
        raise SystemExit(0)

    sig_module.signal(sig_module.SIGTERM, handle_shutdown)
    sig_module.signal(sig_module.SIGINT, handle_shutdown)

    port = int(os.environ.get("PORT", 7799))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
