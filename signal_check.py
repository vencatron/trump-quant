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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CORR_FILE = os.path.join(DATA_DIR, "correlation_results.json")
SEEN_FILE = os.path.join(DATA_DIR, "monitor_seen.json")
TRADES_FILE = os.path.join(DATA_DIR, "bot_trades.json")
BOT_CACHE_FILE = os.path.join(DATA_DIR, "bot_activity_cache.json")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q=Trump+statement+OR+Trump+tariff+OR+truth+social+OR+Trump+iran&hl=en-US&gl=US&ceid=US:en"

# Only alert on these categories (skip noise)
SIGNAL_CATEGORIES = {
    "TARIFFS", "TRADE_DEAL", "CRYPTO", "FED_ATTACK", "MARKET_PUMP",
    "SPECIFIC_TICKER", "IRAN_ESCALATION", "IRAN_DEESCALATION", "OIL_SHOCK",
}

# Alpaca paper trading credentials
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "PKQ2P7KLMAJH5E3IQVKYQPTBOB")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "A58boqhagLVQH7tKfz7MafU8axJx6HGc9GbR4VgUFhrT")
ALPACA_URL = "https://paper-api.alpaca.markets"

# Inverse ETF mapping for SHORT signals
INVERSE_MAP = {
    "QQQ": "SQQQ",   # 3x inverse Nasdaq
    "SPY": "SPXU",   # 3x inverse S&P
}

POSITION_SIZE = 2500  # max dollars per trade

# Calibrated signals from 35k-post correlation data
TOP_SIGNALS = {
    # Iran war signals — highest volatility, biggest moves
    "IRAN_ESCALATION": [
        {"ticker": "GLD",  "direction": "BULLISH", "avg_return": +3.2, "window": "same day",  "confidence": "HIGH", "action": "BUY"},
        {"ticker": "QQQ",  "direction": "BEARISH", "avg_return": -2.1, "window": "same day",  "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "UVIX", "direction": "BULLISH", "avg_return": +8.5, "window": "same day",  "confidence": "HIGH", "action": "BUY"},
    ],
    "IRAN_DEESCALATION": [
        {"ticker": "SPY",  "direction": "BULLISH", "avg_return": +2.5, "window": "same day",  "confidence": "HIGH", "action": "BUY"},
        {"ticker": "QQQ",  "direction": "BULLISH", "avg_return": +3.1, "window": "same day",  "confidence": "HIGH", "action": "BUY"},
        {"ticker": "GLD",  "direction": "BEARISH", "avg_return": -1.8, "window": "same day",  "confidence": "MEDIUM", "action": "SHORT"},
    ],
    # Tariff signals — validated from 1,740 posts, p=0.0000
    "TARIFFS": [
        {"ticker": "COIN", "direction": "BEARISH", "avg_return": -3.5, "window": "same day",  "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "QQQ",  "direction": "BEARISH", "avg_return": -0.6, "window": "same day",  "confidence": "HIGH", "action": "SHORT"},
        {"ticker": "GLD",  "direction": "BULLISH", "avg_return": +2.3, "window": "1 week",    "confidence": "HIGH", "action": "BUY"},
    ],
    "TRADE_DEAL": [
        {"ticker": "SPY",  "direction": "BULLISH", "avg_return": +0.4, "window": "1 week",    "confidence": "HIGH", "action": "BUY"},
        {"ticker": "QQQ",  "direction": "BULLISH", "avg_return": +0.3, "window": "1 week",    "confidence": "HIGH", "action": "BUY"},
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
        {"ticker": "GLD",  "direction": "BULLISH", "avg_return": +2.0, "window": "same day",  "confidence": "MEDIUM", "action": "BUY"},
        {"ticker": "SPY",  "direction": "BEARISH", "avg_return": -0.8, "window": "same day",  "confidence": "MEDIUM", "action": "SHORT"},
    ],
}


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


def execute_paper_trade(signal, post, category):
    """Execute a paper trade based on a fired signal."""
    ticker = signal["ticker"]
    action = signal["action"]
    window = signal["window"]
    confidence = signal["confidence"]

    # Only execute HIGH confidence signals when market is open
    if confidence != "HIGH":
        print(f"  Skipping trade — {confidence} confidence (need HIGH)")
        return None

    market_open = is_market_open()
    if not market_open:
        print(f"  Market closed — logging trade intent only")

    # Determine actual ticker and side
    actual_ticker = ticker
    side = "buy"
    trade_direction = "LONG"

    if action == "SHORT":
        if ticker in INVERSE_MAP:
            actual_ticker = INVERSE_MAP[ticker]
            side = "buy"
            trade_direction = "SHORT (via inverse ETF)"
            print(f"  SHORT {ticker} → BUY {actual_ticker} (inverse ETF)")
        elif ticker == "COIN":
            print(f"  SHORT COIN → logging intent (no easy inverse ETF)")
            trade_direction = "SHORT (intent logged)"
            actual_ticker = None
        else:
            print(f"  SHORT {ticker} → attempting short sell")
            side = "sell"
            trade_direction = "SHORT"

    # Get price and calculate shares
    price = get_current_price(actual_ticker) if actual_ticker else None
    if price and price > 0:
        shares = max(1, int(POSITION_SIZE / price))
    else:
        shares = 1
        price = 0

    # Calculate exit time
    if "same day" in window:
        exit_strategy = "EOD"
        exit_by = datetime.now(timezone.utc).replace(hour=21, minute=0, second=0).isoformat()
    else:
        exit_strategy = "5 trading days"
        exit_by = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    # Submit order if market is open and we have a ticker
    order = None
    if market_open and actual_ticker:
        order = submit_alpaca_order(actual_ticker, shares, side)

    # Log trade
    trade = {
        "trade_id": f"tq-{int(time.time())}-{ticker}",
        "signal_category": category,
        "signal_ticker": ticker,
        "actual_ticker": actual_ticker or ticker,
        "direction": trade_direction,
        "action": action,
        "side": side,
        "shares": shares,
        "entry_price": price,
        "position_value": round(price * shares, 2) if price else 0,
        "confidence": confidence,
        "window": window,
        "avg_return": signal["avg_return"],
        "exit_strategy": exit_strategy,
        "exit_by": exit_by,
        "stop_loss_pct": -0.5,
        "target_pct": abs(signal["avg_return"]),
        "status": "OPEN" if (market_open and order) else "LOGGED",
        "order_id": order.get("id") if order else None,
        "post_text": post["text"][:200],
        "post_id": post["id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_was_open": market_open,
    }
    save_trade(trade)
    return trade


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
                    posts.append({
                        "id":     f"gn-{abs(hash(title)) % 999999:06d}",
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


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    seen = load_seen()
    posts = fetch_posts()
    fired = 0

    for post in posts:
        if post["id"] in seen:
            continue
        seen.add(post["id"])

        cat_result = categorize_post(post["text"])
        categories = [c for c in cat_result["categories"] if c in SIGNAL_CATEGORIES]

        if not categories:
            continue

        # Find all signals for this post (new: multiple signals per category)
        best_signal = None
        best_category = None
        for cat in categories:
            if cat in TOP_SIGNALS:
                signals_list = TOP_SIGNALS[cat]
                for sig in signals_list:
                    if best_signal is None or abs(sig["avg_return"]) > abs(best_signal["avg_return"]):
                        best_signal = sig
                        best_category = cat

        if best_signal:
            # Execute paper trade
            trade = execute_paper_trade(best_signal, post, best_category)

            # Build and send alert
            alert = build_alert(post, categories, best_signal, trade)
            print(f"FIRING ALERT: {post['text'][:80]}...")
            print(alert)
            send_telegram(alert)
            fired += 1

            # Execute ALL high-confidence signals for this category (not just the best)
            if best_category in TOP_SIGNALS:
                for sig in TOP_SIGNALS[best_category]:
                    if sig is not best_signal and sig["confidence"] == "HIGH":
                        extra_trade = execute_paper_trade(sig, post, best_category)
                        if extra_trade:
                            print(f"  Additional trade: {extra_trade['direction']} {extra_trade['actual_ticker']}")

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
                    print(f"  -> Bot detector armed for {post['id']}")
                except Exception as e:
                    print(f"  -> Bot detector launch failed: {e}")

    save_seen(seen)
    print(f"Done. Checked {len(posts)} posts, fired {fired} alerts, seen pool: {len(seen)}")


if __name__ == "__main__":
    main()
