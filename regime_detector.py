"""
Regime Detector — measures how sensitive the market currently is to Trump posts.
HIGH_SENSITIVITY = bots react hard (last 3 weeks, Iran war period)
LOW_SENSITIVITY = market ignoring Trump (mid-2025 quiet period)
"""

import json
import os
import time
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
REGIME_FILE = os.path.join(DATA_DIR, "market_regime.json")
LEARNING_LOG = os.path.join(DATA_DIR, "learning_log.jsonl")
CACHE_TTL = 14400  # 4 hours


def detect_regime() -> dict:
    """Detect current market regime based on VIX, volatility, and post correlation."""
    os.makedirs(DATA_DIR, exist_ok=True)

    vix = None
    avg_daily_range_pct = 0
    spy_data_ok = False

    try:
        import yfinance as yf

        # VIX
        try:
            vix_ticker = yf.Ticker("^VIX")
            vix_hist = vix_ticker.history(period="1d")
            if not vix_hist.empty:
                vix = round(float(vix_hist["Close"].iloc[-1]), 2)
        except Exception:
            pass

        # SPY daily range over last 10 trading days
        try:
            spy = yf.Ticker("SPY")
            spy_hist = spy.history(period="15d")
            if not spy_hist.empty and len(spy_hist) >= 5:
                ranges = []
                for _, row in spy_hist.iterrows():
                    if row["Open"] > 0:
                        daily_range = (row["High"] - row["Low"]) / row["Open"] * 100
                        ranges.append(daily_range)
                if ranges:
                    avg_daily_range_pct = round(sum(ranges[-10:]) / len(ranges[-10:]), 3)
                    spy_data_ok = True
        except Exception:
            pass

    except ImportError:
        pass

    # Trump post correlation from learning_log
    post_correlation = _compute_post_correlation()

    # Determine regime
    if vix is not None:
        if vix > 25:
            vix_regime = "HIGH_SENSITIVITY"
        elif vix >= 18:
            vix_regime = "MEDIUM_SENSITIVITY"
        else:
            vix_regime = "LOW_SENSITIVITY"
    else:
        vix_regime = "MEDIUM_SENSITIVITY"

    range_regime = "HIGH_SENSITIVITY" if avg_daily_range_pct > 1.5 else (
        "MEDIUM_SENSITIVITY" if avg_daily_range_pct > 0.8 else "LOW_SENSITIVITY"
    )

    corr_regime = "HIGH_SENSITIVITY" if post_correlation > 60 else (
        "MEDIUM_SENSITIVITY" if post_correlation > 30 else "LOW_SENSITIVITY"
    )

    # Composite: take the highest sensitivity from all signals
    regime_scores = {"HIGH_SENSITIVITY": 3, "MEDIUM_SENSITIVITY": 2, "LOW_SENSITIVITY": 1}
    combined_score = max(
        regime_scores[vix_regime],
        regime_scores[range_regime],
        regime_scores[corr_regime],
    )
    regime = {3: "HIGH_SENSITIVITY", 2: "MEDIUM_SENSITIVITY", 1: "LOW_SENSITIVITY"}[combined_score]

    # Position multiplier
    multipliers = {"HIGH_SENSITIVITY": 1.5, "MEDIUM_SENSITIVITY": 1.0, "LOW_SENSITIVITY": 0.5}

    # Notes
    notes_parts = []
    if vix and vix > 25:
        notes_parts.append("VIX elevated")
    if post_correlation > 60:
        notes_parts.append("high post-market correlation")
    if avg_daily_range_pct > 1.5:
        notes_parts.append("wide daily ranges")
    iran_active = _check_iran_active()
    if iran_active:
        notes_parts.append("Iran situation active")
    notes = ", ".join(notes_parts) + " — maximum signal sensitivity" if notes_parts else "Normal conditions"

    result = {
        "regime": regime,
        "vix": vix,
        "avg_daily_range_pct": avg_daily_range_pct,
        "post_market_correlation_pct": post_correlation,
        "recommended_position_multiplier": multipliers[regime],
        "notes": notes,
        "updated": datetime.now(timezone.utc).isoformat(),
        "components": {
            "vix_regime": vix_regime,
            "range_regime": range_regime,
            "correlation_regime": corr_regime,
        },
    }

    with open(REGIME_FILE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def _compute_post_correlation() -> int:
    """From learning_log, what % of trades in last 5 days had >0.3% actual move?"""
    if not os.path.exists(LEARNING_LOG):
        return 50  # default assumption

    cutoff = time.time() - 5 * 86400
    total = 0
    moved = 0

    try:
        with open(LEARNING_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("closed_at_ts", 0) < cutoff:
                    continue
                total += 1
                if abs(entry.get("actual_move_pct", 0)) > 0.3:
                    moved += 1
    except Exception:
        return 50

    if total == 0:
        return 50  # not enough data, assume medium
    return round((moved / total) * 100)


def _check_iran_active() -> bool:
    """Check if any IRAN trade in the last 4 hours."""
    if not os.path.exists(LEARNING_LOG):
        return False
    cutoff = time.time() - 4 * 3600
    try:
        with open(LEARNING_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if "IRAN" in entry.get("signal_category", "") and entry.get("closed_at_ts", 0) > cutoff:
                    return True
    except Exception:
        pass
    return False


def get_regime_multiplier() -> float:
    """Quick function signal_check.py can call to get current size multiplier."""
    if os.path.exists(REGIME_FILE):
        try:
            mtime = os.path.getmtime(REGIME_FILE)
            if (time.time() - mtime) < CACHE_TTL:
                with open(REGIME_FILE) as f:
                    data = json.load(f)
                return data.get("recommended_position_multiplier", 1.0)
        except Exception:
            pass

    # File missing or stale — refresh
    try:
        result = detect_regime()
        return result.get("recommended_position_multiplier", 1.0)
    except Exception:
        return 1.0


if __name__ == "__main__":
    result = detect_regime()
    print(json.dumps(result, indent=2))
    print(f"\nRegime: {result['regime']}")
    print(f"Position multiplier: {result['recommended_position_multiplier']}x")
