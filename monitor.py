"""Lightweight live monitor for new Trump posts with signal alerts."""

import json
import os
import time
from datetime import datetime, timezone

import requests

from categorize import categorize_post

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CORR_FILE = os.path.join(DATA_DIR, "correlation_results.json")
SEEN_FILE = os.path.join(DATA_DIR, "monitor_seen.json")

CHECK_INTERVAL = 900  # 15 minutes

# Google News RSS for Trump-related news
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q=Trump+statement+OR+Trump+truth+social&hl=en-US&gl=US&ceid=US:en"


def load_correlation_results() -> dict:
    """Load pre-computed correlation results for signal lookup."""
    if not os.path.exists(CORR_FILE):
        print("WARNING: No correlation results found. Run correlate.py first.")
        return {}
    with open(CORR_FILE) as f:
        return json.load(f)


def load_seen() -> set:
    """Load previously seen post IDs."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def fetch_google_news_rss() -> list[dict]:
    """Fetch recent Trump-related headlines from Google News RSS."""
    try:
        resp = requests.get(GOOGLE_NEWS_RSS, timeout=15, headers={
            "User-Agent": "TrumpQuant Monitor/1.0"
        })
        resp.raise_for_status()

        # Simple XML parsing without lxml dependency
        import re
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        posts = []
        for item in items[:20]:
            title_match = re.search(r"<title>(.*?)</title>", item)
            date_match = re.search(r"<pubDate>(.*?)</pubDate>", item)
            link_match = re.search(r"<link>(.*?)</link>", item)

            if title_match:
                title = title_match.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                # Filter for Trump-specific content
                if any(kw in title.lower() for kw in ["trump", "tariff", "truth social"]):
                    posts.append({
                        "id": f"gn-{hash(title) % 100000:05d}",
                        "text": title,
                        "date": date_match.group(1) if date_match else datetime.now(timezone.utc).isoformat(),
                        "source": "google_news",
                        "link": link_match.group(1) if link_match else "",
                    })
        return posts
    except Exception as e:
        print(f"  [Google News] Error: {e}")
        return []


def lookup_signal(categories: list[str], corr_results: dict) -> list[dict]:
    """Look up historical signal for given categories."""
    signals = []
    cat_analysis = corr_results.get("category_analysis", {})

    for cat in categories:
        if cat in cat_analysis and "SPY" in cat_analysis[cat]:
            spy_stats = cat_analysis[cat]["SPY"]
            for window, s in spy_stats.items():
                signals.append({
                    "category": cat,
                    "window": window,
                    "mean_return_pct": s["mean_return_pct"],
                    "positive_rate": s.get("positive_rate_pct", 50),
                    "sample_size": s["sample_size"],
                    "p_value": s["p_value"],
                })
    return signals


def print_alert(post: dict, cat_result: dict, signals: list[dict]):
    """Print a formatted alert for a new post."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print()
    print("!" * 60)
    print(f"  NEW TRUMP POST DETECTED — {now}")
    print("!" * 60)
    print(f"  Source: {post['source']}")
    print(f"  Text: \"{post['text'][:200]}\"")
    print(f"  Categories: {', '.join(cat_result['categories'])}")
    print(f"  Sentiment: {cat_result['sentiment']}")
    if cat_result["mentioned_tickers"]:
        print(f"  Tickers mentioned: {', '.join(cat_result['mentioned_tickers'])}")

    if signals:
        print(f"\n  --- HISTORICAL SIGNAL ---")
        for s in signals:
            arrow = "▲" if s["mean_return_pct"] > 0 else "▼"
            print(f"  {arrow} {s['category']} → SPY {s['window']}: "
                  f"{s['mean_return_pct']:+.3f}% "
                  f"(win rate: {s['positive_rate']:.0f}%, n={s['sample_size']}, "
                  f"p={s['p_value']:.3f})")
    else:
        print("\n  No historical signal data available for these categories.")

    print("!" * 60)
    print()


def main():
    print("=" * 60)
    print("  TRUMPQUANT LIVE MONITOR")
    print(f"  Checking every {CHECK_INTERVAL // 60} minutes")
    print("  Press Ctrl+C to stop")
    print("=" * 60)

    corr_results = load_correlation_results()
    seen = load_seen()

    print(f"Loaded {len(seen)} previously seen posts")
    print(f"Correlation data: {'loaded' if corr_results else 'NOT AVAILABLE'}")
    print()

    try:
        while True:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print(f"[{now}] Checking for new posts...")

            new_posts = fetch_google_news_rss()
            new_count = 0

            for post in new_posts:
                if post["id"] in seen:
                    continue

                seen.add(post["id"])
                new_count += 1

                # Categorize
                cat_result = categorize_post(post["text"])

                # Look up signal
                signals = lookup_signal(cat_result["categories"], corr_results)

                # Alert
                print_alert(post, cat_result, signals)

            if new_count == 0:
                print(f"  No new posts found.")
            else:
                save_seen(seen)
                print(f"  Processed {new_count} new posts.")

            print(f"  Next check in {CHECK_INTERVAL // 60} minutes...\n")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
        save_seen(seen)


if __name__ == "__main__":
    main()
