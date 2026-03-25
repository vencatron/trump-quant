"""
TrumpQuant Signal Check v2 — runs once, fires Telegram alert + paper trades.
Designed to be called by cron every 15-30 minutes.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(__file__))
from categorize import categorize_post
from learning_engine import record_outcome
from swing_engine import process_signal_for_swing, monitor_swing_positions, get_swing_summary

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CORR_FILE = os.path.join(DATA_DIR, "correlation_results.json")
SEEN_FILE = os.path.join(DATA_DIR, "monitor_seen.json")
TRADES_FILE = os.path.join(DATA_DIR, "bot_trades.json")
BOT_CACHE_FILE = os.path.join(DATA_DIR, "bot_activity_cache.json")
TRADED_TODAY_FILE = os.path.join(DATA_DIR, "traded_today.json")
LEARNING_LOG_FILE = os.path.join(DATA_DIR, "learning_log.jsonl")
EOD_LOG_FILE = os.path.join(DATA_DIR, "eod_log.json")
VIX_CACHE_FILE = os.path.join(DATA_DIR, 'vix_cache.json')
VIX_REGIME_THRESHOLD = 25  # UVIX only useful when VIX < 25
SIGNAL_COOLDOWN_FILE = os.path.join(DATA_DIR, 'signal_cooldowns.json')
DAILY_TRADE_COUNT_FILE = os.path.join(DATA_DIR, 'daily_trade_count.json')
SIGNAL_COOLDOWN_HOURS = 4
MAX_TRADES_PER_DAY = 6
MAX_TRADES_PER_RUN = 2
DRY_RUN = '--dry-run' in sys.argv
MAX_DAILY_EXPOSURE = 10000  # $10k total portfolio cap
MAX_PER_TICKER_DAILY = 2500  # $2,500 hard cap per ticker per day
MAX_POSITIONS = 4  # max 4 concurrent positions

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q=Trump+statement+OR+Trump+tariff+OR+truth+social+OR+Trump+iran&hl=en-US&gl=US&ceid=US:en"

# Only alert on these categories (skip noise)
SIGNAL_CATEGORIES = {
    "TARIFFS", "TRADE_DEAL", "CRYPTO", "FED_ATTACK", "MARKET_PUMP",
    "SPECIFIC_TICKER", "IRAN_ESCALATION", "IRAN_DEESCALATION", "OIL_SHOCK",
    "WAR_ESCALATION", "MUSK_TRUMP",
}

# Alpaca paper trading credentials — from environment only
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL = "https://paper-api.alpaca.markets"

if not ALPACA_KEY or not ALPACA_SECRET:
    print("WARNING: Alpaca API keys not set in environment variables. Trading will fail.")

# Inverse ETF mapping for SHORT signals
INVERSE_MAP = {
    "QQQ": "SQQQ",   # 3x inverse Nasdaq
    "SPY": "SPXU",   # 3x inverse S&P
    "XLE": None,      # Energy — no inverse, skip SHORT
    "XLB": None,      # Materials — no inverse, skip SHORT
}

POSITION_SIZE = 2500  # max dollars per trade

# Per-ticker profit/stop targets for monitor_open_positions
SCALP_TARGETS = {
    'UVIX':  {'take_profit': 1.5, 'stop_loss': -0.5, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'SQQQ':  {'take_profit': 1.5, 'stop_loss': -0.5, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'SPXU':  {'take_profit': 1.5, 'stop_loss': -0.5, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'SPY':   {'take_profit': 0.8, 'stop_loss': -0.5, 'trail_activation_pct': 0.3, 'trail_distance_pct': 0.2},
    'QQQ':   {'take_profit': 0.8, 'stop_loss': -0.5, 'trail_activation_pct': 0.3, 'trail_distance_pct': 0.2},
    'XLE':   {'take_profit': 2.0, 'stop_loss': -0.8, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'USO':   {'take_profit': 2.5, 'stop_loss': -1.0, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'LMT':   {'take_profit': 2.0, 'stop_loss': -0.8, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'TSLA':  {'take_profit': 2.5, 'stop_loss': -1.5, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'NVDA':  {'take_profit': 2.0, 'stop_loss': -1.5, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'COIN':  {'take_profit': 3.0, 'stop_loss': -2.0, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'GLD':   {'take_profit': 2.0, 'stop_loss': -1.0, 'trail_activation_pct': 0.5, 'trail_distance_pct': 0.3},
    'DEFAULT': {'take_profit': 1.0, 'stop_loss': -0.5, 'trail_activation_pct': 0.3, 'trail_distance_pct': 0.2},
}

TRAILING_STOPS_FILE = os.path.join(DATA_DIR, 'trailing_stops.json')

# Watchlist for all tradeable tickers
WATCHLIST = ["UVIX", "SQQQ", "SPXU", "SPY", "QQQ", "GLD", "COIN", "XLE", "USO", "LMT", "TSLA", "XLB", "NVDA"]

# Fallback routing when primary ticker is already held
FALLBACK_MAP = {
    "UVIX": "GLD",
    "SQQQ": "SPXU",
    "SPXU": None,   # no further fallback
    "GLD": None,
    "XLE": "USO",
    "USO": None,
    "LMT": None,
    "TSLA": None,
}
MAX_CONCURRENT_POSITIONS = 2  # max 2 concurrent positions across all tickers

# Calibrated signals from 35k-post correlation data
TOP_SIGNALS = {
    # Iran war signals — highest volatility, biggest moves
    "IRAN_ESCALATION": [
        {"ticker": "GLD",  "direction": "BUY",  "action": "BUY",  "target_pct": 2.0, "stop_pct": 1.0, "confidence": "HIGH", "avg_return": 2.3,  "window": "1-3 days", "rationale": "Gold primary safe haven — 95% win rate on 35k datapoints", "regime_aware": False},
        {"ticker": "UVIX", "direction": "BUY",  "action": "BUY",  "target_pct": 2.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 8.5,  "window": "same day", "rationale": "VIX spikes on war", "regime_aware": True},
        {"ticker": "SQQQ", "direction": "BUY",  "action": "BUY",  "target_pct": 2.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 2.1,  "window": "same day", "rationale": "QQQ drops on escalation", "regime_aware": True},
        {"ticker": "XLE",  "direction": "BUY",  "action": "BUY",  "target_pct": 3.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 3.5,  "window": "same day", "rationale": "Energy pumps on Hormuz risk"},
        {"ticker": "LMT",  "direction": "BUY",  "action": "BUY",  "target_pct": 2.5, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 2.8,  "window": "same day", "rationale": "Defense contracts on war"},
    ],
    "IRAN_DEESCALATION": [
        {"ticker": "SPY",  "direction": "BUY",  "action": "BUY",  "target_pct": 2.5, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 2.5,  "window": "same day", "rationale": "Market rips on peace", "uvix_alert": True},
        {"ticker": "QQQ",  "direction": "BUY",  "action": "BUY",  "target_pct": 3.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 3.1,  "window": "same day", "rationale": "Tech leads recovery", "uvix_alert": True},
        {"ticker": "TSLA", "direction": "BUY",  "action": "BUY",  "target_pct": 4.0, "stop_pct": 1.0, "confidence": "HIGH", "avg_return": 4.2,  "window": "same day", "rationale": "Musk/Trump correlation — TSLA rips on good news"},
    ],
    # Tariff signals — validated from 1,740 posts, p=0.0000
    "TARIFFS": [
        {"ticker": "SQQQ", "direction": "BUY",  "action": "BUY",  "target_pct": 1.5, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 1.8,  "window": "same day", "rationale": "QQQ -0.6% on tariff posts"},
        {"ticker": "SPXU", "direction": "BUY",  "action": "BUY",  "target_pct": 1.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 1.4,  "window": "same day", "rationale": "SPY drops same day"},
        {"ticker": "XLB",  "direction": "SHORT","action": "SHORT", "target_pct": 1.2, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 1.2,  "window": "same day", "rationale": "Materials sector -1.2% on tariffs"},
    ],
    "TRADE_DEAL": [
        {"ticker": "QQQ",  "direction": "BUY",  "action": "BUY",  "target_pct": 1.5, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 1.5,  "window": "same day", "rationale": "Market pumps on deal"},
        {"ticker": "SPY",  "direction": "BUY",  "action": "BUY",  "target_pct": 1.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 1.0,  "window": "same day", "rationale": "Broad market long"},
        {"ticker": "TSLA", "direction": "BUY",  "action": "BUY",  "target_pct": 3.0, "stop_pct": 1.0, "confidence": "HIGH", "avg_return": 3.2,  "window": "same day", "rationale": "TSLA rips on Trump good news"},
        {"ticker": "XLE",  "direction": "SHORT","action": "SHORT", "target_pct": 1.5, "stop_pct": 0.5, "confidence": "MEDIUM","avg_return": 1.5, "window": "same day", "rationale": "Oil drops on Iran deal"},
    ],
    "MARKET_PUMP": [
        {"ticker": "QQQ",  "direction": "BEARISH", "avg_return": -0.5, "window": "same day",  "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "COIN", "direction": "BEARISH", "avg_return": -3.3, "window": "same day",  "confidence": "HIGH", "action": "SHORT"},
    ],
    "FED_ATTACK": [
        {"ticker": "QQQ",  "direction": "BEARISH", "avg_return": -0.56, "window": "same day", "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "GLD",  "direction": "BULLISH", "avg_return": +2.4, "window": "1 week",    "confidence": "HIGH", "action": "BUY"},
    ],
    "OIL_SHOCK": [
        {"ticker": "USO",  "direction": "BUY",  "action": "BUY",  "target_pct": 3.0, "stop_pct": 0.8, "confidence": "HIGH", "avg_return": 4.0,  "window": "same day", "rationale": "Oil ETF spikes on supply shock"},
        {"ticker": "XLE",  "direction": "BUY",  "action": "BUY",  "target_pct": 2.5, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 3.0,  "window": "same day", "rationale": "Energy sector pumps"},
    ],
    "WAR_ESCALATION": [
        {"ticker": "LMT",  "direction": "BUY",  "action": "BUY",  "target_pct": 2.5, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 2.8,  "window": "same day", "rationale": "Lockheed prints on conflict"},
        {"ticker": "SQQQ", "direction": "BUY",  "action": "BUY",  "target_pct": 2.0, "stop_pct": 0.5, "confidence": "HIGH", "avg_return": 2.0,  "window": "same day", "rationale": "Market drops on war news"},
    ],
    "MUSK_TRUMP": [
        {"ticker": "TSLA", "direction": "BUY",  "action": "BUY",  "target_pct": 3.0, "stop_pct": 1.0, "confidence": "MEDIUM","avg_return": 3.5,  "window": "same day", "rationale": "Musk/Trump correlation play"},
    ],
}


def load_trailing_stops():
    if os.path.exists(TRAILING_STOPS_FILE):
        try:
            with open(TRAILING_STOPS_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_trailing_stops(stops):
    with open(TRAILING_STOPS_FILE, "w") as f:
        json.dump(stops, f, indent=2)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def load_trades():
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def save_trade(trade):
    trades = load_trades()
    trades.append(trade)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


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


def load_signal_cooldowns():
    if os.path.exists(SIGNAL_COOLDOWN_FILE):
        try:
            with open(SIGNAL_COOLDOWN_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_signal_cooldowns(cooldowns):
    with open(SIGNAL_COOLDOWN_FILE, 'w') as f:
        json.dump(cooldowns, f, indent=2)


def is_on_cooldown(category, ticker):
    cooldowns = load_signal_cooldowns()
    key = f'{category}_{ticker}'
    if key in cooldowns:
        expires = datetime.fromisoformat(cooldowns[key])
        if datetime.now(timezone.utc) < expires:
            return True
        # Expired — clean up
        del cooldowns[key]
        save_signal_cooldowns(cooldowns)
    return False


def set_cooldown(category, ticker):
    cooldowns = load_signal_cooldowns()
    key = f'{category}_{ticker}'
    cooldowns[key] = (datetime.now(timezone.utc) + timedelta(hours=SIGNAL_COOLDOWN_HOURS)).isoformat()
    save_signal_cooldowns(cooldowns)


def get_daily_trade_count():
    if os.path.exists(DAILY_TRADE_COUNT_FILE):
        try:
            with open(DAILY_TRADE_COUNT_FILE) as f:
                data = json.load(f)
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if data.get('date') == today:
                return data.get('count', 0)
        except Exception:
            pass
    return 0


def increment_daily_trade_count():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    data = {'date': today, 'count': get_daily_trade_count() + 1}
    with open(DAILY_TRADE_COUNT_FILE, 'w') as f:
        json.dump(data, f, indent=2)


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


def is_market_open():
    """Check if US stock market is currently open (rough check)."""
    now = datetime.now(timezone.utc)
    # Market hours: 9:30 AM - 4:00 PM ET (14:30 - 21:00 UTC)
    # Rough — doesn't account for holidays
    hour_utc = now.hour
    weekday = now.weekday()
    if weekday >= 5:  # Saturday/Sunday
        return False
    if hour_utc < 14 or (hour_utc == 14 and now.minute < 30):
        return False
    if hour_utc >= 21:
        return False
    return True


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def get_current_price(ticker):
    """Get latest price from Alpaca market data."""
    try:
        url = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
        resp = requests.get(url, headers=alpaca_headers(), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            quote = data.get("quote", {})
            mid = (quote.get("ap", 0) + quote.get("bp", 0)) / 2
            if mid > 0:
                return round(mid, 2)
    except Exception as e:
        print(f"  Price fetch error for {ticker}: {e}")
    return None


def submit_alpaca_order(ticker, qty, side="buy"):
    """Submit a market order to Alpaca paper trading."""
    url = f"{ALPACA_URL}/v2/orders"
    payload = {
        "symbol": ticker,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    try:
        resp = requests.post(url, json=payload, headers=alpaca_headers(), timeout=15)
        if resp.status_code in (200, 201):
            order = resp.json()
            print(f"  ORDER SUBMITTED: {side.upper()} {qty} {ticker} — order_id={order.get('id', 'unknown')}")
            return order
        else:
            print(f"  ORDER FAILED ({resp.status_code}): {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"  ORDER ERROR: {e}")
        return None


def _normalize_headline(text):
    """Normalize headline for content-based dedup: lowercase, strip URLs, strip punctuation."""
    text = text.lower()
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_current_vix():
    '''Fetch VIX level, cached for 30 minutes.'''
    if os.path.exists(VIX_CACHE_FILE):
        try:
            with open(VIX_CACHE_FILE) as f:
                cache = json.load(f)
            if time.time() - cache.get('timestamp', 0) < 1800:  # 30 min TTL
                return cache.get('vix', None)
        except:
            pass
    try:
        import yfinance as yf
        vix = yf.Ticker('^VIX')
        hist = vix.history(period='1d')
        if not hist.empty:
            vix_level = float(hist['Close'].iloc[-1])
            with open(VIX_CACHE_FILE, 'w') as f:
                json.dump({'vix': vix_level, 'timestamp': time.time()}, f)
            return vix_level
    except Exception as e:
        print(f'  VIX fetch error: {e}')
    return None


def is_already_priced_in():
    '''Check if SPY already dropped >1% today — move is priced in.'''
    try:
        url = f'https://data.alpaca.markets/v2/stocks/SPY/bars'
        params = {'timeframe': '1Day', 'limit': 1, 'feed': 'iex'}
        r = requests.get(url, params=params, headers=alpaca_headers(), timeout=8)
        if r.status_code == 200:
            bars = r.json().get('bars', [])
            if bars:
                open_price = float(bars[-1]['o'])
                close_price = float(bars[-1]['c'])
                daily_change = ((close_price - open_price) / open_price) * 100
                if daily_change <= -1.0:
                    print(f'  Already priced in: SPY {daily_change:.2f}% today')
                    return True
    except Exception as e:
        print(f'  Priced-in check error: {e}')
    return False


def execute_paper_trade(signal, post, category):
    """Execute a paper trade with full safety guards."""
    # GUARD: Daily trade limit
    if get_daily_trade_count() >= MAX_TRADES_PER_DAY:
        print(f'  Skipping — hit daily trade limit ({MAX_TRADES_PER_DAY})')
        return None

    # GUARD: Signal cooldown
    if is_on_cooldown(category, signal["ticker"]):
        print(f'  Skipping — {category}_{signal["ticker"]} on cooldown')
        return None

    # HARD GATE: Never execute real trades outside market hours
    now_et = datetime.now(timezone(timedelta(hours=-4)))  # ET approximation
    hour = now_et.hour
    if not (9 <= hour < 16):
        print(f"  After-hours gate: {hour}:00 ET — skipping trade execution")
        return None

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

    # GUARD 2+4: Fetch all positions once — per-ticker dedup + concurrent position limit
    try:
        positions_resp = requests.get(f"{ALPACA_URL}/v2/positions", headers=alpaca_headers(), timeout=8)
        all_positions = positions_resp.json() if positions_resp.status_code == 200 else []
    except Exception:
        all_positions = []
    held_tickers = {p["symbol"] for p in all_positions} if isinstance(all_positions, list) else set()

    if actual_ticker in held_tickers:
        # Try fallback routing
        fallback = FALLBACK_MAP.get(actual_ticker)
        if fallback and fallback not in held_tickers:
            print(f"  Routing to fallback: {fallback} ({actual_ticker} already held)")
            actual_ticker = fallback
            side = "buy"
            trade_direction = f"LONG (fallback from {ticker})"
        else:
            print(f"  Skipping — already holding {actual_ticker}, no fallback available")
            return None

    if len(held_tickers) >= MAX_CONCURRENT_POSITIONS:
        print(f"  Skipping — max {MAX_CONCURRENT_POSITIONS} concurrent positions ({len(held_tickers)}/{MAX_CONCURRENT_POSITIONS} held)")
        return None

    # GUARD 3: Check total portfolio exposure
    total_exposure = sum(abs(float(p.get("market_value", 0))) for p in all_positions) if all_positions else 0
    if total_exposure >= MAX_DAILY_EXPOSURE:
        print(f"  Skipping — total exposure ${total_exposure:.0f} >= ${MAX_DAILY_EXPOSURE} cap")
        return None

    # HARD CAP position size: $2,500 regardless of multipliers
    adjusted_size = MAX_PER_TICKER_DAILY

    # GUARD: VIX regime check for UVIX
    if actual_ticker == 'UVIX':
        vix = get_current_vix()
        if vix is not None and vix >= VIX_REGIME_THRESHOLD:
            print(f'  VIX regime block: VIX={vix:.1f} >= {VIX_REGIME_THRESHOLD} — UVIX not effective')
            # Route to GLD or SQQQ instead
            for fallback_ticker in ['GLD', 'SQQQ']:
                if fallback_ticker not in held_tickers:
                    print(f'  VIX regime routing to {fallback_ticker}')
                    actual_ticker = fallback_ticker
                    side = 'buy'
                    trade_direction = f'LONG (VIX regime fallback from UVIX)'
                    price = get_current_price(actual_ticker)
                    if price and price > 0:
                        shares = max(1, int(adjusted_size / price))
                        break
            else:
                return None  # no fallback available

    # GUARD: Already priced in — don't short if market already tanked
    if action == 'SHORT' or actual_ticker in ('SQQQ', 'SPXU'):
        if is_already_priced_in():
            print(f'  Skipping — market drop already priced in')
            return None

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

    # Dry-run mode — skip actual order
    if DRY_RUN:
        print(f'  [DRY-RUN] Would {side} {shares} {actual_ticker} @ ${price:.2f} (category={category})')
        return {'dry_run': True, 'ticker': actual_ticker, 'shares': shares, 'price': price, 'side': side, 'category': category, 'direction': trade_direction, 'actual_ticker': actual_ticker, 'entry_price': price, 'position_value': shares * price, 'exit_strategy': 'DRY_RUN'}

    # Submit order
    order = submit_alpaca_order(actual_ticker, shares, side)

    # Record in traded_today
    if order:
        record_traded_today(post["id"], actual_ticker)
        set_cooldown(category, actual_ticker)
        increment_daily_trade_count()

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


def get_bot_activity_score(ticker, lookback_minutes=30):
    """
    Compute a 0-100 'heat score' for a ticker based on volume vs 15-day average.
    Uses Alpaca market data bars.
    """
    cache = {}
    if os.path.exists(BOT_CACHE_FILE):
        try:
            with open(BOT_CACHE_FILE) as f:
                cache = json.load(f)
            # Use cache if fresh (< 5 min old)
            cached = cache.get(ticker)
            if cached and (time.time() - cached.get("ts", 0)) < 300:
                return cached.get("score", 50)
        except (json.JSONDecodeError, ValueError):
            pass

    try:
        # Fetch recent 1-min bars
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=lookback_minutes)
        url = f"https://data.alpaca.markets/v2/stocks/{ticker}/bars"
        params = {
            "timeframe": "1Min",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": lookback_minutes,
        }
        resp = requests.get(url, params=params, headers=alpaca_headers(), timeout=10)
        if resp.status_code != 200:
            return 50  # default

        bars = resp.json().get("bars", [])
        if not bars:
            return 30

        recent_volume = sum(b.get("v", 0) for b in bars)

        # Fetch 15-day daily bars for average
        day_start = end - timedelta(days=15)
        params_daily = {
            "timeframe": "1Day",
            "start": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 15,
        }
        resp_daily = requests.get(url, params=params_daily, headers=alpaca_headers(), timeout=10)
        daily_bars = resp_daily.json().get("bars", []) if resp_daily.status_code == 200 else []

        if daily_bars:
            avg_daily_vol = sum(b.get("v", 0) for b in daily_bars) / len(daily_bars)
            # Scale: recent volume in lookback_minutes vs proportional daily avg
            minutes_in_day = 390  # 6.5 hours
            expected_vol = avg_daily_vol * (lookback_minutes / minutes_in_day)
            if expected_vol > 0:
                ratio = recent_volume / expected_vol
                score = min(100, int(ratio * 33))  # 3x = 100
            else:
                score = 50
        else:
            score = 50

        # Cache result
        cache[ticker] = {"score": score, "ts": time.time(), "volume": recent_volume}
        with open(BOT_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

        return score

    except Exception as e:
        print(f"  Bot activity score error for {ticker}: {e}")
        return 50


def fetch_posts():
    try:
        resp = requests.get(GOOGLE_NEWS_RSS, timeout=15, headers={"User-Agent": "TrumpQuant/2.0"})
        resp.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        posts = []
        for item in items[:25]:
            title_m = re.search(r"<title>(.*?)</title>", item)
            date_m  = re.search(r"<pubDate>(.*?)</pubDate>", item)
            link_m  = re.search(r"<link>(.*?)</link>", item)
            if title_m:
                title = title_m.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                if any(k in title.lower() for k in ["trump", "tariff", "truth social", "iran"]):
                    # Content-based dedup: normalize text then hash
                    normalized = _normalize_headline(title)
                    post_id = f"gn-{abs(hash(normalized)) % 999999:06d}"
                    posts.append({
                        "id":     post_id,
                        "text":   title,
                        "date":   date_m.group(1) if date_m else "",
                        "source": "google_news",
                        "link":   link_m.group(1) if link_m else "",
                    })
        return posts
    except Exception as e:
        print(f"Fetch error: {e}")
        return []


def build_alert(post, categories, signal, trade=None):
    direction_emoji = "🟢" if signal["direction"] == "BULLISH" else "🔴"
    conf_emoji = "⭐⭐" if signal["confidence"] == "HIGH" else "⭐"
    arrow = "▲" if signal["direction"] == "BULLISH" else "▼"

    # Iran posts get special treatment
    is_iran = any("IRAN" in c for c in categories)
    header = "☢️ *IRAN SIGNAL*" if is_iran else "🚨 *TrumpQuant Signal*"

    msg = (
        f"{header}\n\n"
        f"📰 _{post['text'][:180]}_\n\n"
        f"🏷 Category: `{', '.join(categories)}`\n"
        f"{direction_emoji} Signal: *{signal['direction']}* {signal['ticker']}\n"
        f"{arrow} Avg move: *{signal['avg_return']:+.2f}%* ({signal['window']})\n"
        f"{conf_emoji} Confidence: {signal['confidence']}\n"
    )

    if trade:
        msg += (
            f"\n💰 *TRADE EXECUTED*\n"
            f"  {trade['direction']} {trade['shares']} x {trade['actual_ticker']} @ ${trade['entry_price']}\n"
            f"  Position: ${trade['position_value']}\n"
            f"  Exit: {trade['exit_strategy']}\n"
        )

    msg += f"\n_Based on historical Trump post patterns — not financial advice_"
    return msg


def send_telegram(text):
    """Use openclaw to send a Telegram message to Ron."""
    try:
        result = subprocess.run(
            ["openclaw", "message", "send", "--to", "8387647137", "--channel", "telegram", "--message", text],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False


def _find_trade_meta(ticker, active_scalps):
    """Find trade metadata from active_scalps for a given ticker."""
    for t in (active_scalps or []):
        if t.get("actual_ticker") == ticker or t.get("signal_ticker") == ticker:
            return t
    return {}


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

        # Load trailing stops state
        trailing_stops = load_trailing_stops()
        trail_state = trailing_stops.get(ticker, {'high_pct': 0, 'trail_stop_pct': None, 'partial_taken': False})

        targets = SCALP_TARGETS.get(ticker, SCALP_TARGETS['DEFAULT'])
        take_profit = targets['take_profit']
        stop_loss = targets['stop_loss']
        trail_activation = targets.get('trail_activation_pct', 0.5)
        trail_distance = targets.get('trail_distance_pct', 0.3)

        close_reason = None
        close_qty = None  # None = close all, else partial

        # Update high water mark
        if pnl_pct > trail_state.get('high_pct', 0):
            trail_state['high_pct'] = pnl_pct

        high_pct = trail_state['high_pct']

        # Trailing stop tiers:
        # Tier 1: Up +0.5% → trail at breakeven (0%)
        # Tier 2: Up +1.0% → trail at 0.5% below high + partial exit 50%
        # Tier 3: Up +1.5% → trail at 1.0% below high
        if high_pct >= 1.5:
            trail_state['trail_stop_pct'] = high_pct - 1.0
        elif high_pct >= 1.0:
            trail_state['trail_stop_pct'] = high_pct - 0.5
            # Partial exit: close 50% at +1.0% if not already done
            if not trail_state.get('partial_taken', False):
                total_qty = int(pos.get('qty', 0))
                partial_qty = max(1, total_qty // 2)
                print(f'  PARTIAL EXIT: Closing {partial_qty}/{total_qty} of {ticker} at +{pnl_pct:.2f}%')
                try:
                    payload = {'symbol': ticker, 'qty': str(partial_qty), 'side': 'sell', 'type': 'market', 'time_in_force': 'day'}
                    resp = requests.post(f'{ALPACA_URL}/v2/orders', json=payload, headers=headers, timeout=10)
                    if resp.status_code in (200, 201):
                        trail_state['partial_taken'] = True
                        partial_pnl = (pnl_pct / 100) * (float(pos.get('market_value', 0)) / 2)
                        emoji = '💰'
                        msg = f'{emoji} *TrumpQuant Partial Exit*\n\nTicker: {ticker}\nClosed: {partial_qty}/{total_qty} shares\nP&L: +{pnl_pct:.2f}%\nRest trailing at {trail_state["trail_stop_pct"]:.1f}%'
                        send_telegram(msg)
                except Exception as e:
                    print(f'  Partial exit error: {e}')
        elif high_pct >= 0.5:
            trail_state['trail_stop_pct'] = 0  # breakeven

        # Save trailing stop state
        trailing_stops[ticker] = trail_state
        save_trailing_stops(trailing_stops)

        # Check close conditions
        # Rule 1: Take profit (still keep as hard ceiling)
        if pnl_pct >= take_profit:
            close_reason = f'PROFIT_TAKE (+{pnl_pct:.2f}%, target {take_profit}%)'

        # Rule 2: Trailing stop hit
        elif trail_state.get('trail_stop_pct') is not None and pnl_pct <= trail_state['trail_stop_pct']:
            close_reason = f'TRAILING_STOP ({pnl_pct:.2f}%, trail at {trail_state["trail_stop_pct"]:.1f}%)'

        # Rule 3: Hard stop loss (unchanged)
        elif pnl_pct <= stop_loss:
            close_reason = f'STOP_LOSS ({pnl_pct:.2f}%, limit {stop_loss}%)'

        # Rule 4: Time-based (unchanged)
        elif hours_held > 2.0 and unrealized_pl > 0:
            close_reason = f'SCALP_COMPLETE ({hours_held:.1f}h, +{pnl_pct:.2f}%)'

        # Rule 5: Stale
        elif hours_held > 6.5:
            close_reason = f'STALE_POSITION ({hours_held:.1f}h)'

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

                    # Clean up trailing stop state
                    if ticker in trailing_stops:
                        del trailing_stops[ticker]
                        save_trailing_stops(trailing_stops)

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

                    # Feed learning engine with full trade context
                    trade_result = {
                        'signal_category': trade_meta.get('signal_category', 'UNKNOWN'),
                        'signal_ticker': trade_meta.get('signal_ticker', ticker),
                        'actual_ticker': ticker,
                        'direction': trade_meta.get('direction', 'LONG'),
                        'entry_price': trade_meta.get('entry_price', 0),
                        'exit_price': float(pos.get('current_price', pos.get('avg_entry_price', 0))),
                        'pnl': unrealized_pl,
                        'exit_reason': close_reason,
                        'trade_id': trade_meta.get('trade_id', ''),
                        'avg_return': trade_meta.get('avg_return', 0),
                        'target_pct': trade_meta.get('target_pct', targets.get('take_profit', 1.0)),
                        'stop_loss_pct': trade_meta.get('stop_loss_pct', targets.get('stop_loss', -0.5)),
                        'timestamp': trade_meta.get('timestamp', ''),
                        'closed_at': now.isoformat(),
                    }
                    try:
                        record_outcome(trade_result)
                        print(f'  Learning engine: recorded outcome for {ticker}')
                    except Exception as e:
                        print(f'  Learning engine error: {e}')

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


def check_for_dips():
    """
    Aggressive intraday dip buyer.
    When the market has dropped significantly intraday, buy the dip.
    Trump manipulation pattern: markets overreact down, then recover.
    """
    # HARD GATE: Never execute real trades outside market hours
    now_et = datetime.now(timezone(timedelta(hours=-4)))  # ET approximation
    hour = now_et.hour
    if not (9 <= hour < 16):
        print(f"  After-hours gate: {hour}:00 ET — skipping dip check")
        return None

    if not is_market_open():
        return None

    headers = alpaca_headers()

    # Tickers to watch for dips
    DIP_TARGETS = {
        "SPY":  {"dip_threshold": -1.0, "target_pct": 1.2, "stop_pct": 0.5, "max_size": 2500},
        "QQQ":  {"dip_threshold": -1.5, "target_pct": 1.5, "stop_pct": 0.5, "max_size": 2500},
        "TSLA": {"dip_threshold": -3.0, "target_pct": 3.0, "stop_pct": 1.5, "max_size": 2500},
        "NVDA": {"dip_threshold": -3.0, "target_pct": 2.5, "stop_pct": 1.5, "max_size": 2500},
        "COIN": {"dip_threshold": -5.0, "target_pct": 4.0, "stop_pct": 2.0, "max_size": 2500},
    }

    # Get current positions to avoid doubling
    try:
        r = requests.get(f"{ALPACA_URL}/v2/positions", headers=headers, timeout=8)
        held = {p["symbol"] for p in (r.json() if r.status_code == 200 else [])}
        if len(held) >= 2:
            return None  # already at max positions
    except Exception:
        return None

    bought = []
    for ticker, cfg in DIP_TARGETS.items():
        if ticker in held:
            continue

        try:
            # Get today open and current price via Alpaca bars
            url = f"https://data.alpaca.markets/v2/stocks/{ticker}/bars"
            params = {"timeframe": "1Day", "limit": 2, "feed": "iex"}
            r = requests.get(url, params=params, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            bars = r.json().get("bars", [])
            if not bars:
                continue

            today_open = float(bars[-1]["o"])
            current = float(bars[-1]["c"])
            intraday_chg_pct = ((current - today_open) / today_open) * 100

            if intraday_chg_pct <= cfg["dip_threshold"]:
                # IT IS A DIP — BUY AGGRESSIVELY
                shares = max(1, int(cfg["max_size"] / current))
                print(f"  DIP DETECTED: {ticker} down {intraday_chg_pct:.1f}% today — BUYING {shares} shares @ ${current:.2f}")

                order = submit_alpaca_order(ticker, shares, "buy")
                if order:
                    bought.append({
                        "ticker": ticker,
                        "shares": shares,
                        "entry_price": current,
                        "dip_pct": intraday_chg_pct,
                        "target_pct": cfg["target_pct"],
                        "stop_pct": cfg["stop_pct"],
                        "reason": f"DIP_BUY ({intraday_chg_pct:.1f}% down)",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "actual_ticker": ticker,
                        "signal_category": "DIP_BUY",
                        "direction": "LONG",
                        "status": "OPEN",
                        "trade_id": f"dip-{int(time.time())}-{ticker}",
                        "position_value": current * shares,
                    })
                    held.add(ticker)
                    if len(held) >= 2:
                        break
        except Exception as e:
            print(f"  Dip check error {ticker}: {e}")

    if bought:
        # Save to active_scalps
        scalps_file = os.path.join(DATA_DIR, "active_scalps.json")
        existing = []
        if os.path.exists(scalps_file):
            try:
                with open(scalps_file) as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        existing.extend(bought)
        with open(scalps_file, "w") as f:
            json.dump(existing, f, indent=2)

        # Send Telegram alert
        msg = "*TrumpQuant DIP BUYER*\n\n"
        for b in bought:
            msg += f"*{b['ticker']}* DOWN {b['dip_pct']:.1f}% — BOUGHT {b['shares']} shares @ ${b['entry_price']:.2f}\n"
            msg += f"   Target: +{b['target_pct']}% | Stop: -{b['stop_pct']}%\n\n"
        msg += "_Aggressive dip buy — Trump manipulation recovery pattern_"
        send_telegram(msg)

    return bought


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

    # Check for dips aggressively
    if is_market_open():
        check_for_dips()

    # Monitor swing positions (check targets/stops/time exits)
    swing_closed = monitor_swing_positions()
    if swing_closed:
        total_swing_pnl = sum(c["pnl_dollars"] for c in swing_closed)
        msg = f"📈 *TrumpQuant Swing Close*\n\n"
        for c in swing_closed:
            e = "✅" if c["pnl_dollars"] >= 0 else "❌"
            msg += f"{e} *{c['ticker']}* {c['close_reason']}\n"
            msg += f"   P&L: ${c['pnl_dollars']:+.2f} ({c['pnl_pct']:+.1f}%)\n"
            msg += f"   Thesis: _{c['thesis']}_\n\n"
        msg += f"*Swing P&L today: ${total_swing_pnl:+.2f}*"
        send_telegram(msg)

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
            # Regime-aware filtering
            if best_signal.get('regime_aware'):
                # These signals need VIX check — handled in execute_paper_trade
                pass  # The VIX gate in execute_paper_trade handles UVIX routing

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
                if not DRY_RUN:
                    send_telegram(alert)
                fired += 1

            # Also open swing position on high-conviction signals
            if best_category in ("IRAN_ESCALATION", "TARIFFS", "FED_ATTACK", "WAR_ESCALATION", "TRADE_DEAL", "IRAN_DEESCALATION"):
                swing_positions = process_signal_for_swing(best_category, post["text"])
                if swing_positions:
                    swing_msg = f"📊 *TrumpQuant Swing Position Opened*\n\n"
                    for sp in swing_positions:
                        swing_msg += f"🎯 *{sp['direction']} {sp['ticker']}* @ ${sp['entry_price']:.2f}\n"
                        swing_msg += f"   Size: ${sp['position_value']:,.0f} | Target: +{sp['target_pct']}% in {sp['hold_days']}d\n"
                        swing_msg += f"   Stop: -{sp['stop_pct']}% | Conviction: {sp['conviction']}\n"
                        swing_msg += f"   📖 _{sp['thesis']}_\n\n"
                    send_telegram(swing_msg)

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
    print("ADD MONITOR CRON: */5 9-16 * * 1-5 America/Los_Angeles — python3 signal_check.py monitor")


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


if __name__ == "__main__":
    if DRY_RUN:
        print("[DRY-RUN MODE] No real orders or Telegram messages will be sent.")
    args = [a for a in sys.argv[1:] if a != '--dry-run']
    if args and args[0] == "eod":
        close_eod_positions()
    elif args and args[0] == "monitor":
        monitor_open_positions()
    elif args and args[0] == "weekend":
        from weekend_war import run_friday_assessment
        run_friday_assessment()
    elif args and args[0] == "monday":
        from weekend_war import monday_gap_detector
        monday_gap_detector()
    else:
        main()
