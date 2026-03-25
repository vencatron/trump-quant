"""
TrumpQuant Learning Engine
Tracks trade outcomes, reweights signal confidence, detects patterns.
Runs after each trade closes and weekly for full recalibration.
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LEARNING_LOG = os.path.join(DATA_DIR, "learning_log.jsonl")
WEIGHTS_FILE = os.path.join(DATA_DIR, "signal_weights.json")
REGIME_FILE = os.path.join(DATA_DIR, "market_regime.json")


def _et_now():
    """Current time in US Eastern."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _iran_active():
    """Check if any IRAN-category trade was logged in the last 4 hours."""
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
                if "IRAN" in entry.get("signal_category", ""):
                    ts = entry.get("closed_at_ts", 0)
                    if ts > cutoff:
                        return True
    except Exception:
        pass
    return False


def _post_timing(timestamp_iso):
    """Classify post timing as premarket, market_hours, or afterhours."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(timestamp_iso).astimezone(ZoneInfo("America/New_York"))
        h, m = dt.hour, dt.minute
        market_open = (h == 9 and m >= 30) or (10 <= h < 16)
        if market_open:
            return "market_hours"
        elif h < 9 or (h == 9 and m < 30):
            return "premarket"
        else:
            return "afterhours"
    except Exception:
        return "market_hours"


def record_outcome(trade_result: dict):
    """After each trade closes, append outcome to learning_log.jsonl."""
    os.makedirs(DATA_DIR, exist_ok=True)

    entry_price = trade_result.get("entry_price", 0)
    exit_price = trade_result.get("exit_price", 0)
    if entry_price and entry_price > 0 and exit_price and exit_price > 0:
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        actual_move_pct = pnl_pct
    elif not exit_price and trade_result.get('pnl') is not None and entry_price and entry_price > 0:
        # If pnl is passed directly (dollar P&L), compute pct from it
        pnl_pct = (trade_result['pnl'] / (entry_price * trade_result.get('shares', 1))) * 100
        actual_move_pct = pnl_pct
    else:
        pnl_pct = 0
        actual_move_pct = 0

    # Flip sign for SHORT trades — price going down is a win
    direction = trade_result.get("direction", "LONG")
    if "SHORT" in direction:
        pnl_pct = -pnl_pct
        actual_move_pct = -actual_move_pct

    predicted_move_pct = trade_result.get("avg_return", 0)
    target_pct = trade_result.get("target_pct", abs(predicted_move_pct))
    stop_loss_pct = trade_result.get("stop_loss_pct", -0.5)

    hit_target = actual_move_pct >= target_pct if target_pct else False
    hit_stop = pnl_pct <= stop_loss_pct if stop_loss_pct else False
    eod_close = trade_result.get("exit_reason", "") == "EOD"

    entry_ts = trade_result.get("timestamp", "")
    exit_ts = trade_result.get("closed_at", datetime.now(timezone.utc).isoformat())
    try:
        t1 = datetime.fromisoformat(entry_ts)
        t2 = datetime.fromisoformat(exit_ts)
        seconds_to_exit = (t2 - t1).total_seconds()
    except Exception:
        seconds_to_exit = 0

    et_now = _et_now()

    # Get regime
    regime = "UNKNOWN"
    if os.path.exists(REGIME_FILE):
        try:
            with open(REGIME_FILE) as f:
                regime = json.load(f).get("regime", "UNKNOWN")
        except Exception:
            pass

    record = {
        "signal_category": trade_result.get("signal_category", ""),
        "ticker": trade_result.get("signal_ticker", trade_result.get("actual_ticker", "")),
        "direction": direction,
        "predicted_move_pct": round(predicted_move_pct, 3),
        "actual_move_pct": round(actual_move_pct, 3),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 3),
        "time_of_day": et_now.strftime("%H:%M"),
        "day_of_week": et_now.strftime("%A"),
        "hit_target": hit_target,
        "hit_stop": hit_stop,
        "eod_close": eod_close,
        "seconds_to_exit": round(seconds_to_exit),
        "iran_active": _iran_active(),
        "post_timing": _post_timing(entry_ts),
        "market_regime": regime,
        "trade_id": trade_result.get("trade_id", ""),
        "closed_at": exit_ts,
        "closed_at_ts": time.time(),
    }

    with open(LEARNING_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

    # Update weights after every trade close
    try:
        calculate_signal_weights()
    except Exception:
        pass

    return record


def _load_log():
    """Load all entries from learning_log.jsonl."""
    entries = []
    if not os.path.exists(LEARNING_LOG):
        return entries
    with open(LEARNING_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def calculate_signal_weights() -> dict:
    """Compute per-signal stats and save weights to signal_weights.json."""
    entries = _load_log()

    if not entries:
        weights = {"_meta": {"updated": datetime.now(timezone.utc).isoformat(), "total_trades": 0}}
        with open(WEIGHTS_FILE, "w") as f:
            json.dump(weights, f, indent=2)
        return weights

    # Group by signal_category
    by_cat = defaultdict(list)
    for e in entries:
        cat = e.get("signal_category", "UNKNOWN")
        by_cat[cat].append(e)

    weights = {}
    for cat, trades in by_cat.items():
        # Rolling last 20 trades
        recent = trades[-20:]
        wins = [t for t in recent if t.get("pnl_pct", 0) > 0]
        win_rate = len(wins) / len(recent) if recent else 0

        # Avg actual vs predicted
        diffs = []
        for t in recent:
            pred = abs(t.get("predicted_move_pct", 0))
            actual = t.get("actual_move_pct", 0)
            if pred > 0:
                diffs.append(actual / pred)
        avg_actual_vs_predicted = sum(diffs) / len(diffs) if diffs else 1.0

        # Best time of day
        time_pnl = defaultdict(list)
        for t in trades:
            tod = t.get("time_of_day", "00:00")
            hour = tod.split(":")[0]
            time_pnl[hour].append(t.get("pnl_pct", 0))
        best_hour = max(time_pnl, key=lambda h: sum(time_pnl[h]) / len(time_pnl[h])) if time_pnl else "10"

        # Worst conditions
        worst_conditions = []
        regime_pnl = defaultdict(list)
        for t in trades:
            regime_pnl[t.get("market_regime", "UNKNOWN")].append(t.get("pnl_pct", 0))
        for regime, pnls in regime_pnl.items():
            avg = sum(pnls) / len(pnls)
            if avg < 0:
                worst_conditions.append(f"{regime} (avg {avg:.2f}%)")

        # Size multiplier based on win rate
        n = len(recent)
        if n < 5:
            multiplier = 1.0  # not enough data
        elif win_rate > 0.70:
            multiplier = 1.5
        elif win_rate >= 0.55:
            multiplier = 1.0
        elif win_rate >= 0.40:
            multiplier = 0.75
        else:
            multiplier = 0.5

        weights[cat] = {
            "rolling_win_rate": round(win_rate, 3),
            "total_trades": len(trades),
            "recent_trades": n,
            "avg_actual_vs_predicted": round(avg_actual_vs_predicted, 3),
            "best_time_of_day": f"{best_hour}:00 ET",
            "worst_conditions": worst_conditions,
            "recommended_size_multiplier": multiplier,
            "avg_pnl_pct": round(sum(t.get("pnl_pct", 0) for t in recent) / n, 3) if n else 0,
        }

    weights["_meta"] = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(entries),
    }

    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)

    return weights


def generate_weekly_report() -> str:
    """Generate a formatted Telegram message with weekly performance stats."""
    entries = _load_log()

    if not entries:
        return "📊 *TrumpQuant Weekly Report*\n\nNo trades recorded yet. Learning engine is ready."

    # Filter to last 7 days
    cutoff = time.time() - 7 * 86400
    week = [e for e in entries if e.get("closed_at_ts", 0) > cutoff]

    if not week:
        return "📊 *TrumpQuant Weekly Report*\n\nNo trades this week. Bot is monitoring."

    # Per-signal win rate
    by_cat = defaultdict(list)
    for e in week:
        by_cat[e.get("signal_category", "UNKNOWN")].append(e)

    lines = ["📊 *TrumpQuant Weekly Report*\n"]

    best_cat, best_wr = None, -1
    worst_cat, worst_wr = None, 2

    for cat, trades in sorted(by_cat.items()):
        wins = len([t for t in trades if t.get("pnl_pct", 0) > 0])
        wr = wins / len(trades) if trades else 0
        total_pnl = sum(t.get("pnl_pct", 0) for t in trades)
        lines.append(f"  `{cat}`: {wins}/{len(trades)} wins ({wr:.0%}) — {total_pnl:+.2f}%")
        if wr > best_wr:
            best_wr, best_cat = wr, cat
        if wr < worst_wr:
            worst_wr, worst_cat = wr, cat

    total_pnl = sum(t.get("pnl_pct", 0) for t in week)
    lines.append(f"\n✅ *Best signal*: `{best_cat}` ({best_wr:.0%} win rate)")
    lines.append(f"❌ *Worst signal*: `{worst_cat}` ({worst_wr:.0%} win rate)")
    lines.append(f"💰 *Total P&L*: {total_pnl:+.2f}% across {len(week)} trades")

    # Key learning: what condition caused most losses?
    losses = [t for t in week if t.get("pnl_pct", 0) < 0]
    if losses:
        regime_losses = defaultdict(float)
        for t in losses:
            regime_losses[t.get("market_regime", "UNKNOWN")] += abs(t.get("pnl_pct", 0))
        worst_regime = max(regime_losses, key=regime_losses.get)
        lines.append(f"\n🔍 *Key learning*: Most losses in `{worst_regime}` regime ({regime_losses[worst_regime]:.2f}% total loss)")

        timing_losses = defaultdict(int)
        for t in losses:
            timing_losses[t.get("post_timing", "unknown")] += 1
        worst_timing = max(timing_losses, key=timing_losses.get)
        lines.append(f"⏰ Worst entry timing: `{worst_timing}` ({timing_losses[worst_timing]} losing trades)")

    # Recommendations
    lines.append("\n📋 *Recommendations*:")
    weights = calculate_signal_weights()
    for cat, w in weights.items():
        if cat == "_meta":
            continue
        mult = w.get("recommended_size_multiplier", 1.0)
        if mult < 1.0:
            lines.append(f"  ⚠️ Reduce `{cat}` size to {mult}x (win rate: {w['rolling_win_rate']:.0%})")
        elif mult > 1.0:
            lines.append(f"  🚀 Increase `{cat}` size to {mult}x (win rate: {w['rolling_win_rate']:.0%})")

    # Unusual Whales upgrade check
    overall_wr = len([t for t in week if t.get("pnl_pct", 0) > 0]) / len(week) if week else 0
    if overall_wr > 0.55 and len(week) >= 10:
        lines.append("\n🐋 Win rate >55% sustained — consider Unusual Whales upgrade for options flow data")

    lines.append("\n_Auto-generated by TrumpQuant Learning Engine_")
    return "\n".join(lines)


def apply_weights_to_signal(signal: dict, weights: dict) -> dict:
    """Apply learned size multiplier to a signal from TOP_SIGNALS."""
    signal = dict(signal)  # don't mutate original
    cat = signal.get("signal_category", "")

    # Look up weight for this category
    cat_weight = weights.get(cat, {})
    multiplier = cat_weight.get("recommended_size_multiplier", 1.0)

    signal["learned_size_multiplier"] = multiplier
    signal["learned_win_rate"] = cat_weight.get("rolling_win_rate", None)
    return signal


if __name__ == "__main__":
    weights = calculate_signal_weights()
    print(json.dumps(weights, indent=2))
