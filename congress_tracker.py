"""
TrumpQuant Congressional Trading Tracker
Monitors STOCK Act disclosures for defense/energy/war-related trades.
Congress members trade BEFORE announcements — this is our early warning system.
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
CONGRESS_TRADES_FILE = os.path.join(DATA_DIR, "congress_trades.json")
CONGRESS_SIGNAL_FILE = os.path.join(DATA_DIR, "congress_signal.json")
CONGRESS_LAST_REPORT_FILE = os.path.join(DATA_DIR, "congress_last_report.json")

WAR_TICKERS = [
    "LMT", "RTX", "NOC", "BA", "GD", "HII",
    "LHX", "XLE", "USO", "DVN", "OXY", "CVX",
]

# Members with highest signal value — classified briefing access
ARMED_SERVICES_MEMBERS = [
    "Roger Wicker", "Jack Reed", "Jim Inhofe", "Deb Fischer",
    "Tom Cotton", "Mike Rounds", "Joni Ernst", "Dan Sullivan",
    "Kevin Cramer", "Rick Scott", "Tuberville", "Tommy Tuberville",
    "Mark Kelly", "Tim Kaine", "Jeanne Shaheen", "Kirsten Gillibrand",
    "Richard Blumenthal", "Mazie Hirono", "Elizabeth Warren",
    "Adam Smith", "Mike Rogers", "Jim Cooper",
]

INTELLIGENCE_COMMITTEE_MEMBERS = [
    "Mark Warner", "Marco Rubio", "Dianne Feinstein", "Richard Burr",
    "Ben Sasse", "Susan Collins", "John Cornyn", "Tom Cotton",
    "Mike Turner", "Jim Himes", "Adam Schiff",
]

HIGH_VALUE_MEMBERS = set(ARMED_SERVICES_MEMBERS + INTELLIGENCE_COMMITTEE_MEMBERS)

CACHE_TTL = 4 * 3600  # 4 hours


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


def fetch_congress_trades() -> list[dict]:
    """
    Fetch recent congressional trading disclosures.
    Tries Quiver Quantitative API, then SEC EDGAR, then scraping as fallback.
    Results cached for 4 hours.
    """
    # Check cache
    cached = _load_json(CONGRESS_TRADES_FILE, default={})
    if isinstance(cached, dict) and cached.get("trades"):
        cache_time = cached.get("fetched_at", "")
        if cache_time:
            try:
                dt = datetime.fromisoformat(cache_time)
                if (datetime.now(timezone.utc) - dt).total_seconds() < CACHE_TTL:
                    print(f"  Using cached congress trades ({len(cached['trades'])} records)")
                    return cached["trades"]
            except (ValueError, TypeError):
                pass

    trades = []

    # --- Attempt 1: Quiver Quantitative API ---
    try:
        print("  Fetching from Quiver Quantitative API...")
        resp = requests.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            headers={
                "Accept": "application/json",
                "User-Agent": "TrumpQuant/1.0",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                for item in data[:200]:  # Cap at 200 most recent
                    trades.append({
                        "politician": item.get("Representative", item.get("politician", "Unknown")),
                        "ticker": item.get("Ticker", item.get("ticker", "")),
                        "transaction": item.get("Transaction", item.get("transaction", "Unknown")),
                        "amount": item.get("Amount", item.get("amount", "Unknown")),
                        "date": item.get("TransactionDate", item.get("date", "")),
                        "party": item.get("Party", item.get("party", "Unknown")),
                        "source": "quiver",
                    })
                print(f"  Got {len(trades)} trades from Quiver API")
        else:
            print(f"  Quiver API returned {resp.status_code}")
    except Exception as e:
        print(f"  Quiver API failed: {e}")

    # --- Attempt 2: SEC EDGAR full-text search ---
    if not trades:
        try:
            print("  Trying SEC EDGAR...")
            today = datetime.now(timezone.utc)
            start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            url = f"https://efts.sec.gov/LATEST/search-index?q=%22congressional%22&dateRange=custom&startdt={start}"
            resp = requests.get(
                url,
                headers={"User-Agent": "TrumpQuant/1.0 research@example.com"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                hits = data.get("hits", {}).get("hits", [])
                for hit in hits[:50]:
                    source = hit.get("_source", {})
                    trades.append({
                        "politician": source.get("display_names", ["Unknown"])[0] if source.get("display_names") else "Unknown",
                        "ticker": "",
                        "transaction": "Filing",
                        "amount": "See filing",
                        "date": source.get("file_date", ""),
                        "party": "Unknown",
                        "source": "sec_edgar",
                    })
                print(f"  Got {len(trades)} filings from SEC EDGAR")
        except Exception as e:
            print(f"  SEC EDGAR failed: {e}")

    # --- Attempt 3: Scrape Quiver website ---
    if not trades:
        try:
            print("  Trying Quiver website scrape...")
            resp = requests.get(
                "https://www.quiverquant.com/congresstrading/",
                headers={"User-Agent": "Mozilla/5.0 TrumpQuant/1.0"},
                timeout=15,
            )
            if resp.status_code == 200:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "html.parser")
                    # Look for table rows with trade data
                    rows = soup.select("table tr")
                    for row in rows[1:51]:  # Skip header, cap at 50
                        cells = row.find_all("td")
                        if len(cells) >= 5:
                            trades.append({
                                "politician": cells[0].get_text(strip=True),
                                "ticker": cells[1].get_text(strip=True),
                                "transaction": cells[2].get_text(strip=True),
                                "amount": cells[3].get_text(strip=True),
                                "date": cells[4].get_text(strip=True),
                                "party": cells[5].get_text(strip=True) if len(cells) > 5 else "Unknown",
                                "source": "scrape",
                            })
                    print(f"  Scraped {len(trades)} trades from Quiver website")
                except ImportError:
                    print("  BeautifulSoup not available for scraping")
        except Exception as e:
            print(f"  Quiver scrape failed: {e}")

    if not trades:
        print("  WARNING: All congress trade sources failed")
        trades = []

    # Cache results
    cache_data = {
        "trades": trades,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(trades),
    }
    _save_json(CONGRESS_TRADES_FILE, cache_data)

    return trades


def analyze_war_signals(trades: list) -> list[dict]:
    """
    Filter trades for war/defense/energy signals.
    Returns flagged trades with signal strength.
    """
    if not trades:
        return []

    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    three_days_ago = now - timedelta(days=3)

    flagged = []
    ticker_net_buys = {}

    for trade in trades:
        ticker = trade.get("ticker", "").upper().strip()
        if ticker not in WAR_TICKERS:
            continue

        # Parse date
        trade_date = None
        try:
            date_str = trade.get("date", "")
            if date_str:
                # Try multiple formats
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        trade_date = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if not trade_date:
                    trade_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            trade_date = now  # If can't parse, assume recent

        if trade_date < seven_days_ago:
            continue

        # Track net buys
        transaction = trade.get("transaction", "").lower()
        if ticker not in ticker_net_buys:
            ticker_net_buys[ticker] = 0

        if "purchase" in transaction or "buy" in transaction:
            ticker_net_buys[ticker] += 1
        elif "sale" in transaction or "sell" in transaction:
            ticker_net_buys[ticker] -= 1

        # Check if politician is high-value (Armed Services / Intelligence)
        politician = trade.get("politician", "")
        is_high_value = any(name.lower() in politician.lower() for name in HIGH_VALUE_MEMBERS)
        is_recent = trade_date >= three_days_ago if trade_date else False

        # Determine signal strength
        if is_high_value and is_recent and ("purchase" in transaction or "buy" in transaction):
            signal_strength = "HIGH"
        elif is_high_value:
            signal_strength = "MEDIUM"
        elif "purchase" in transaction or "buy" in transaction:
            signal_strength = "LOW"
        else:
            signal_strength = "INFO"

        flagged.append({
            **trade,
            "is_war_ticker": True,
            "is_high_value_member": is_high_value,
            "signal_strength": signal_strength,
            "is_recent": is_recent,
        })

    # Add net buy analysis
    for item in flagged:
        ticker = item.get("ticker", "").upper()
        item["net_buys_7d"] = ticker_net_buys.get(ticker, 0)
        if ticker_net_buys.get(ticker, 0) > 0:
            # Defense tickers with net buys = BULLISH
            defense_tickers = ["LMT", "RTX", "NOC", "BA", "GD", "HII", "LHX"]
            if ticker in defense_tickers:
                item["defense_signal"] = "BULLISH"
            else:
                item["defense_signal"] = "NEUTRAL"
        else:
            item["defense_signal"] = "BEARISH" if ticker_net_buys.get(ticker, 0) < 0 else "NEUTRAL"

    # Sort by signal strength
    strength_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    flagged.sort(key=lambda x: strength_order.get(x.get("signal_strength", "INFO"), 99))

    return flagged


def generate_congress_signal() -> dict:
    """
    Main signal function — aggregates congress trade data into a signal.
    """
    trades = fetch_congress_trades()
    war_signals = analyze_war_signals(trades)

    if not war_signals:
        result = {
            "signal": "NEUTRAL",
            "tickers": [],
            "evidence": "No defense/energy trades found in recent disclosures",
            "confidence": "LOW",
            "war_trades": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_json(CONGRESS_SIGNAL_FILE, result)
        return result

    # Determine overall signal
    high_signals = [s for s in war_signals if s.get("signal_strength") == "HIGH"]
    medium_signals = [s for s in war_signals if s.get("signal_strength") == "MEDIUM"]
    buy_signals = [s for s in war_signals if "purchase" in s.get("transaction", "").lower() or "buy" in s.get("transaction", "").lower()]
    sell_signals = [s for s in war_signals if "sale" in s.get("transaction", "").lower() or "sell" in s.get("transaction", "").lower()]

    tickers_involved = list(set(s.get("ticker", "") for s in war_signals if s.get("ticker")))

    if high_signals:
        # Armed Services/Intel Committee member bought defense in last 3 days
        signal = "BULLISH"
        confidence = "HIGH"
        evidence = (
            f"{len(high_signals)} HIGH-value trade(s): "
            + ", ".join(f"{s['politician']} → {s['ticker']}" for s in high_signals[:3])
        )
    elif len(buy_signals) >= 2 and len(set(s.get("ticker") for s in buy_signals)) >= 2:
        # Multiple congress members buying 2+ defense tickers
        signal = "BULLISH"
        confidence = "MEDIUM"
        evidence = (
            f"{len(buy_signals)} defense/energy purchases across "
            f"{len(set(s.get('ticker') for s in buy_signals))} tickers this week"
        )
    elif len(sell_signals) > len(buy_signals):
        # Net selling of defense stocks
        signal = "BEARISH"
        confidence = "MEDIUM"
        evidence = f"Net selling: {len(sell_signals)} sales vs {len(buy_signals)} purchases in defense/energy"
    else:
        signal = "NEUTRAL"
        confidence = "LOW"
        evidence = f"Mixed signals: {len(buy_signals)} buys, {len(sell_signals)} sells"

    result = {
        "signal": signal,
        "tickers": tickers_involved,
        "evidence": evidence,
        "confidence": confidence,
        "war_trades": war_signals[:20],  # Top 20 flagged trades
        "total_war_trades": len(war_signals),
        "high_value_count": len(high_signals),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    _save_json(CONGRESS_SIGNAL_FILE, result)
    return result


def daily_congress_report():
    """
    Called by cron at 8am daily on weekdays.
    Fetches fresh data, sends alert if new trades found.
    """
    print("=== DAILY CONGRESS REPORT ===")

    # Fetch fresh data (ignore cache for daily report)
    # Clear cache to force fresh fetch
    if os.path.exists(CONGRESS_TRADES_FILE):
        cached = _load_json(CONGRESS_TRADES_FILE)
        if isinstance(cached, dict):
            cached["fetched_at"] = ""  # Force refresh
            _save_json(CONGRESS_TRADES_FILE, cached)

    signal = generate_congress_signal()

    # Load last report to check for new trades
    last_report = _load_json(CONGRESS_LAST_REPORT_FILE, default={"last_trades": []})
    last_trade_ids = set()
    for t in last_report.get("last_trades", []):
        # Create a unique key for each trade
        key = f"{t.get('politician', '')}_{t.get('ticker', '')}_{t.get('date', '')}"
        last_trade_ids.add(key)

    # Find new trades
    new_trades = []
    war_trades = signal.get("war_trades", [])
    for t in war_trades:
        key = f"{t.get('politician', '')}_{t.get('ticker', '')}_{t.get('date', '')}"
        if key not in last_trade_ids:
            new_trades.append(t)

    if new_trades:
        msg = f"🏛️ *Congress Alert*\n\n"
        for t in new_trades[:5]:  # Cap at 5 per alert
            tx_type = t.get("transaction", "Unknown")
            tx_emoji = "🟢" if "purchase" in tx_type.lower() or "buy" in tx_type.lower() else "🔴"
            high_value = " ⭐" if t.get("is_high_value_member") else ""
            msg += (
                f"{tx_emoji} *{t.get('politician', 'Unknown')}*{high_value}\n"
                f"  {tx_type} {t.get('ticker', '?')} ({t.get('amount', '?')})\n"
                f"  Date: {t.get('date', '?')} | Party: {t.get('party', '?')}\n\n"
            )

        msg += (
            f"💡 Signal: *{signal['signal']}* ({signal['confidence']} confidence)\n"
            f"📊 Evidence: _{signal['evidence']}_\n\n"
        )

        if signal["signal"] == "BULLISH" and signal["confidence"] == "HIGH":
            msg += "⚠️ *HIGH CONFIDENCE — Armed Services/Intel Committee buying defense*"
        elif signal["signal"] == "BEARISH":
            msg += "📉 Congress selling defense — potential de-escalation signal"

        _send_telegram(msg)
        print(f"  Sent alert for {len(new_trades)} new trades")
    else:
        print("  No new defense/energy trades since last report")
        # Still send a brief daily summary if we have data
        if war_trades:
            msg = (
                f"🏛️ *Congress Daily Summary*\n\n"
                f"Signal: {signal['signal']} ({signal['confidence']})\n"
                f"Active war trades: {signal.get('total_war_trades', 0)}\n"
                f"No new trades since last report"
            )
            _send_telegram(msg)

    # Update last report
    _save_json(CONGRESS_LAST_REPORT_FILE, {
        "last_trades": war_trades,
        "last_report_time": datetime.now(timezone.utc).isoformat(),
    })

    print("=== CONGRESS REPORT COMPLETE ===")


def integrate_with_signals():
    """
    Integrates congress signal with the regime detector.
    Boosts or dampens WAR_ESCALATION signals based on congress trading.
    """
    signal = _load_json(CONGRESS_SIGNAL_FILE, default={})
    if not signal:
        return

    congress_signal = signal.get("signal", "NEUTRAL")
    confidence = signal.get("confidence", "LOW")

    # Load current regime
    regime_file = os.path.join(DATA_DIR, "market_regime.json")
    regime = _load_json(regime_file, default={})

    if not regime:
        return

    # Adjust war escalation confidence
    war_conf = regime.get("war_escalation_confidence", 50)

    if congress_signal == "BULLISH" and confidence in ("HIGH", "MEDIUM"):
        # Congress buying defense = boost war signal
        boost = 20 if confidence == "HIGH" else 10
        war_conf = min(100, war_conf + boost)
        regime["war_escalation_confidence"] = war_conf
        regime["congress_boost"] = boost
        regime["congress_signal"] = "BULLISH"
        print(f"  Congress BOOST: war confidence {war_conf - boost} → {war_conf}")

    elif congress_signal == "BEARISH" and confidence in ("HIGH", "MEDIUM"):
        # Congress selling defense = dampen war signal
        dampen = 15 if confidence == "HIGH" else 8
        war_conf = max(0, war_conf - dampen)
        regime["war_escalation_confidence"] = war_conf
        regime["congress_dampen"] = dampen
        regime["congress_signal"] = "BEARISH"
        print(f"  Congress DAMPEN: war confidence {war_conf + dampen} → {war_conf}")

    else:
        regime["congress_signal"] = "NEUTRAL"

    _save_json(regime_file, regime)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        daily_congress_report()
    elif len(sys.argv) > 1 and sys.argv[1] == "integrate":
        integrate_with_signals()
    else:
        signal = generate_congress_signal()
        print(f"\nCongress Signal: {signal['signal']} ({signal['confidence']})")
        print(f"Evidence: {signal['evidence']}")
        print(f"Tickers: {signal['tickers']}")
        print(f"War trades: {signal.get('total_war_trades', 0)}")
