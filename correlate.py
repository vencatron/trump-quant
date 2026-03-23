"""Correlate Trump posts with market movements."""

import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from scipy import stats

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MARKET_DIR = os.path.join(DATA_DIR, "market_data")
POSTS_FILE = os.path.join(DATA_DIR, "posts_categorized.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "correlation_results.json")

ANALYSIS_TICKERS = ["SPY", "QQQ", "DJI", "NVDA", "GME", "DJT", "TSLA", "META", "COIN", "GLD", "BTC-USD"]


def load_market_data(ticker: str) -> pd.DataFrame | None:
    """Load daily market data for a ticker."""
    path = os.path.join(MARKET_DIR, f"{ticker}_daily.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def find_trading_day(market_df: pd.DataFrame, target_date: datetime) -> int | None:
    """Find the index of the closest trading day on or after target_date."""
    target = pd.Timestamp(target_date).tz_localize("UTC") if target_date.tzinfo is None else pd.Timestamp(target_date).tz_convert("UTC")
    # Strip time component for date comparison
    target_date_only = target.normalize()

    # Find trading days on or after the target date
    future_days = market_df.index[market_df.index.normalize() >= target_date_only]
    if len(future_days) == 0:
        return None
    return market_df.index.get_loc(future_days[0])


def calculate_returns(market_df: pd.DataFrame, post_date: datetime) -> dict:
    """Calculate returns at various windows relative to a post date."""
    idx = find_trading_day(market_df, post_date)
    if idx is None:
        return {}

    returns = {}
    n = len(market_df)

    # Same-day return (open to close of the closest trading day)
    if idx < n:
        row = market_df.iloc[idx]
        if row["Open"] > 0:
            returns["same_day"] = (row["Close"] - row["Open"]) / row["Open"]

    # Next-day return (open to close of T+1)
    if idx + 1 < n:
        row = market_df.iloc[idx + 1]
        if row["Open"] > 0:
            returns["next_day"] = (row["Close"] - row["Open"]) / row["Open"]

    # 1-week forward return (close T to close T+5)
    if idx < n and idx + 5 < n:
        close_t = market_df.iloc[idx]["Close"]
        close_t5 = market_df.iloc[idx + 5]["Close"]
        if close_t > 0:
            returns["one_week"] = (close_t5 - close_t) / close_t

    return returns


def analyze_correlations(posts: list[dict], market_data: dict[str, pd.DataFrame]) -> dict:
    """Main correlation analysis."""
    results = {
        "category_analysis": {},
        "big_movers": [],
        "ticker_specific": {},
        "summary_stats": {},
    }

    # Collect returns by category and ticker
    cat_returns = {}  # {category: {ticker: {window: [returns]}}}

    for post in posts:
        post_date = datetime.fromisoformat(post["date"].replace("Z", "+00:00"))
        categories = post.get("categories", [])
        mentioned_tickers = post.get("mentioned_tickers", [])

        for ticker, mdf in market_data.items():
            returns = calculate_returns(mdf, post_date)
            if not returns:
                continue

            # Track by category
            for cat in categories:
                if cat not in cat_returns:
                    cat_returns[cat] = {}
                if ticker not in cat_returns[cat]:
                    cat_returns[cat][ticker] = {"same_day": [], "next_day": [], "one_week": []}
                for window, ret in returns.items():
                    cat_returns[cat][ticker][window].append(ret)

            # Check for big movers (>1% SPY move within same/next day)
            if ticker == "SPY":
                for window in ["same_day", "next_day"]:
                    if window in returns and abs(returns[window]) > 0.01:
                        results["big_movers"].append({
                            "post_id": post["id"],
                            "text": post["text"][:120],
                            "date": post["date"],
                            "categories": categories,
                            "spy_return": round(returns[window] * 100, 3),
                            "window": window,
                        })

            # Track ticker-specific mentions
            if ticker in mentioned_tickers:
                if ticker not in results["ticker_specific"]:
                    results["ticker_specific"][ticker] = []
                results["ticker_specific"][ticker].append({
                    "post_id": post["id"],
                    "date": post["date"],
                    "text": post["text"][:100],
                    "returns": {k: round(v * 100, 3) for k, v in returns.items()},
                })

    # Compute stats per category
    for cat, tickers in cat_returns.items():
        results["category_analysis"][cat] = {}
        for ticker, windows in tickers.items():
            ticker_stats = {}
            for window, rets in windows.items():
                if len(rets) < 2:
                    continue
                arr = np.array(rets)
                t_stat, p_value = stats.ttest_1samp(arr, 0)
                ticker_stats[window] = {
                    "mean_return_pct": round(float(np.mean(arr)) * 100, 4),
                    "median_return_pct": round(float(np.median(arr)) * 100, 4),
                    "std_pct": round(float(np.std(arr)) * 100, 4),
                    "sample_size": len(rets),
                    "t_statistic": round(float(t_stat), 4),
                    "p_value": round(float(p_value), 4),
                    "significant_at_05": bool(p_value < 0.05),
                    "positive_rate_pct": round(float(np.mean(arr > 0)) * 100, 1),
                }
            if ticker_stats:
                results["category_analysis"][cat][ticker] = ticker_stats

    # Summary stats
    total_posts = len(posts)
    results["summary_stats"] = {
        "total_posts_analyzed": total_posts,
        "total_big_movers": len(results["big_movers"]),
        "categories_analyzed": list(cat_returns.keys()),
        "tickers_analyzed": list(market_data.keys()),
    }

    return results


def main():
    # Load posts
    with open(POSTS_FILE) as f:
        posts = json.load(f)

    # Load market data
    market_data = {}
    for ticker in ANALYSIS_TICKERS:
        df = load_market_data(ticker)
        if df is not None and not df.empty:
            market_data[ticker] = df
            print(f"Loaded {len(df)} days of data for {ticker}")
        else:
            print(f"WARNING: No data for {ticker}")

    if not market_data:
        print("ERROR: No market data found. Run fetch_market.py first.")
        return

    print(f"\nAnalyzing {len(posts)} posts against {len(market_data)} tickers...")
    results = analyze_correlations(posts, market_data)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    # Print highlights
    print(f"\n{'='*60}")
    print("CORRELATION RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"Posts analyzed: {results['summary_stats']['total_posts_analyzed']}")
    print(f"Big movers (>1% SPY): {results['summary_stats']['total_big_movers']}")

    print(f"\n--- Category → SPY Returns ---")
    for cat, tickers in results["category_analysis"].items():
        if "SPY" in tickers:
            spy = tickers["SPY"]
            for window, s in spy.items():
                sig = "*" if s["significant_at_05"] else ""
                print(f"  {cat} → {window}: {s['mean_return_pct']:+.3f}% "
                      f"(n={s['sample_size']}, p={s['p_value']:.3f}{sig})")

    if results["big_movers"]:
        print(f"\n--- Posts that moved SPY >1% ---")
        for m in results["big_movers"][:10]:
            print(f"  [{m['date'][:10]}] {m['spy_return']:+.2f}% ({m['window']})")
            print(f"    \"{m['text']}\"")

    print(f"\nFull results saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
