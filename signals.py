"""Generate trading signal playbook from correlation analysis."""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CORR_FILE = os.path.join(DATA_DIR, "correlation_results.json")


def load_results() -> dict:
    with open(CORR_FILE) as f:
        return json.load(f)


def generate_signals(results: dict) -> list[dict]:
    """Build signal framework from correlation data."""
    signals = []
    cat_analysis = results.get("category_analysis", {})

    for cat, tickers in cat_analysis.items():
        for ticker, windows in tickers.items():
            for window, s in windows.items():
                mean_ret = s["mean_return_pct"]
                n = s["sample_size"]
                p = s["p_value"]

                # Skip tiny sample sizes
                if n < 3:
                    continue

                # Determine confidence
                if p < 0.01 and n >= 10:
                    confidence = "HIGH"
                elif p < 0.05 and n >= 5:
                    confidence = "MEDIUM"
                elif p < 0.10 and n >= 5:
                    confidence = "LOW"
                else:
                    confidence = "SPECULATIVE"

                # Direction
                direction = "BULLISH" if mean_ret > 0 else "BEARISH"

                # Suggested instrument
                instruments = _suggest_instruments(ticker, direction)

                # Suggested hold duration
                hold = _suggest_hold(window)

                signals.append({
                    "category": cat,
                    "ticker": ticker,
                    "window": window,
                    "direction": direction,
                    "mean_return_pct": mean_ret,
                    "positive_rate": s.get("positive_rate_pct", 50),
                    "sample_size": n,
                    "p_value": p,
                    "confidence": confidence,
                    "instruments": instruments,
                    "hold_duration": hold,
                })

    # Sort by confidence then absolute return
    confidence_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "SPECULATIVE": 3}
    signals.sort(key=lambda x: (confidence_order[x["confidence"]], -abs(x["mean_return_pct"])))

    return signals


def _suggest_instruments(ticker: str, direction: str) -> list[str]:
    """Suggest trading instruments based on ticker and direction."""
    instruments = []
    if direction == "BULLISH":
        instruments.append(f"{ticker} shares (long)")
        if ticker in ("SPY", "QQQ"):
            instruments.append(f"{ticker} call options (0-7 DTE)")
            instruments.append(f"TQQQ" if ticker == "QQQ" else "UPRO")
    else:
        instruments.append(f"{ticker} puts")
        if ticker in ("SPY", "QQQ"):
            instruments.append(f"SQQQ" if ticker == "QQQ" else "SPXU")
            instruments.append(f"{ticker} put options (0-7 DTE)")
    return instruments


def _suggest_hold(window: str) -> str:
    if window == "same_day":
        return "Intraday — close by EOD"
    elif window == "next_day":
        return "Overnight — enter at close, exit next day"
    elif window == "one_week":
        return "Swing — hold 3-5 trading days"
    return "Unknown"


def print_playbook(signals: list[dict]):
    """Print human-readable signal playbook."""
    print()
    print("=" * 72)
    print("  TRUMPQUANT SIGNAL PLAYBOOK")
    print("  Based on historical correlation of Trump posts → market moves")
    print("=" * 72)
    print()
    print("DISCLAIMER: This is for educational/research purposes only.")
    print("Past correlations do not guarantee future results.")
    print("Do your own due diligence before trading.")
    print()

    if not signals:
        print("No statistically meaningful signals found.")
        print("This may indicate insufficient data. Run fetch_posts.py and")
        print("fetch_market.py to gather more data.")
        return

    # Group by confidence
    for conf in ["HIGH", "MEDIUM", "LOW", "SPECULATIVE"]:
        group = [s for s in signals if s["confidence"] == conf]
        if not group:
            continue

        print(f"{'─' * 72}")
        print(f"  {conf} CONFIDENCE SIGNALS")
        print(f"{'─' * 72}")
        print()

        for s in group:
            arrow = "▲" if s["direction"] == "BULLISH" else "▼"
            print(f"  {arrow} {s['category']} → {s['ticker']} ({s['window']})")
            print(f"    Direction:     {s['direction']}")
            print(f"    Avg return:    {s['mean_return_pct']:+.3f}%")
            print(f"    Win rate:      {s['positive_rate']:.0f}%")
            print(f"    Sample size:   {s['sample_size']}")
            print(f"    P-value:       {s['p_value']:.4f}")
            print(f"    Hold:          {s['hold_duration']}")
            print(f"    Instruments:   {', '.join(s['instruments'])}")
            print()

    # Print quick reference
    print(f"{'─' * 72}")
    print("  QUICK REFERENCE: When Trump posts about...")
    print(f"{'─' * 72}")
    print()

    seen_cats = set()
    for s in signals:
        if s["category"] in seen_cats:
            continue
        if s["confidence"] in ("HIGH", "MEDIUM"):
            seen_cats.add(s["category"])
            arrow = "▲" if s["direction"] == "BULLISH" else "▼"
            print(f"  • {s['category']:20s} → {arrow} {s['ticker']:8s} "
                  f"{s['mean_return_pct']:+.2f}% ({s['window']}, "
                  f"conf: {s['confidence']})")

    if not seen_cats:
        for s in signals[:5]:
            if s["category"] not in seen_cats:
                seen_cats.add(s["category"])
                arrow = "▲" if s["direction"] == "BULLISH" else "▼"
                print(f"  • {s['category']:20s} → {arrow} {s['ticker']:8s} "
                      f"{s['mean_return_pct']:+.2f}% ({s['window']}, "
                      f"conf: {s['confidence']})")

    print()
    print(f"{'=' * 72}")
    print(f"  Total signals generated: {len(signals)}")
    print(f"  High confidence: {sum(1 for s in signals if s['confidence'] == 'HIGH')}")
    print(f"  Medium confidence: {sum(1 for s in signals if s['confidence'] == 'MEDIUM')}")
    print(f"{'=' * 72}")
    print()


def main():
    results = load_results()
    signals = generate_signals(results)
    print_playbook(signals)


if __name__ == "__main__":
    main()
