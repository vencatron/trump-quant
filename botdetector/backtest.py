"""
Backtest the bot detector using historical minute-bar data.

Approach:
1. Load historical posts with timestamps from data/posts_categorized.json
2. For each post, fetch 1-min bar data around the post time (+/-30 min)
3. Simulate the bot signature detection using bar-level approximation
4. If signature detected, simulate the trade with actual price data
5. Output: hit rate, average P&L, Sharpe ratio, drawdown
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from .config import BotDetectorConfig
from .models import SignalDirection

logger = logging.getLogger("botdetector.backtest")


@dataclass
class BacktestResult:
    """Results from a single simulated trade."""
    post_id: str
    post_text: str
    post_timestamp: str
    category: str
    ticker: str

    # Detection
    signature_detected: bool
    detection_bar_idx: int
    volume_spike_ratio: float
    price_velocity_pct: float
    spread_proxy_widening_pct: float

    # Trade simulation
    direction: str
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_bars: int
    pnl_pct: float
    pnl_dollars: float


@dataclass
class BacktestSummary:
    """Aggregate backtest stats."""
    total_posts: int
    signatures_detected: int
    detection_rate_pct: float
    trades_simulated: int
    win_rate_pct: float
    avg_pnl_pct: float
    total_pnl_dollars: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_hold_bars: float
    avg_detection_bar: float
    best_trade_pnl_pct: float
    worst_trade_pnl_pct: float


class Backtester:

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()
        self.results: list[BacktestResult] = []

    def run(self, posts_file: str = None, output_dir: str = None) -> BacktestSummary:
        """Run full backtest."""
        posts_file = posts_file or os.path.join(
            os.path.dirname(__file__), "..", "data", "posts_categorized.json"
        )
        output_dir = output_dir or os.path.join(
            os.path.dirname(__file__), "..", self.config.backtest_dir
        )
        os.makedirs(output_dir, exist_ok=True)

        # Load posts
        with open(posts_file) as f:
            posts = json.load(f)

        logger.info(f"Loaded {len(posts)} posts for backtesting")

        # Load minute-bar data (fetch via Alpaca if not cached)
        from .alpaca_client import AlpacaRESTClient
        rest = AlpacaRESTClient(self.config)

        for post in posts:
            categories = post.get("categories", [])
            signal_cats = [c for c in categories if c in self.config.category_tickers]
            if not signal_cats:
                continue

            ticker = self.config.category_tickers.get(signal_cats[0], "SPY")
            post_time = datetime.fromisoformat(
                post["date"].replace("Z", "+00:00")
            )

            # Fetch minute bars: 30 min before to 90 min after
            # Ensure UTC timezone and Alpaca-compatible ISO format
            if post_time.tzinfo is None:
                from datetime import timezone
                post_time = post_time.replace(tzinfo=timezone.utc)
            start = (post_time - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = (post_time + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

            try:
                bars = rest.get_bars(ticker, "1Min", start=start, end=end, limit=120)
            except Exception as e:
                logger.warning(f"Failed to get bars for {ticker}: {e}")
                continue

            if len(bars) < 20:
                continue

            result = self._simulate_detection_and_trade(
                post, ticker, signal_cats[0], bars, post_time
            )
            if result:
                self.results.append(result)

        summary = self._compute_summary()

        # Save results
        self._save_results(output_dir, summary)

        return summary

    def _simulate_detection_and_trade(
        self, post: dict, ticker: str, category: str,
        bars: list[dict], post_time: datetime
    ) -> Optional[BacktestResult]:
        """Simulate bot detection on minute-bar data."""
        # Find the bar closest to post time
        post_bar_idx = 0
        for i, bar in enumerate(bars):
            bar_time = datetime.fromisoformat(bar["timestamp"].replace("Z", "+00:00"))
            if bar_time >= post_time:
                post_bar_idx = i
                break

        if post_bar_idx < 15:
            return None  # Not enough trailing data

        price_at_post = bars[post_bar_idx]["close"]

        # Scan bars after post for signature
        detection_window_bars = self.config.detection_window_sec // 60

        for scan_idx in range(post_bar_idx + 1,
                               min(post_bar_idx + detection_window_bars + 1, len(bars))):
            bar = bars[scan_idx]

            # Trailing 15-bar volume avg
            trailing_vols = [
                bars[j]["volume"]
                for j in range(max(0, scan_idx - 15), scan_idx)
            ]
            avg_vol = np.mean(trailing_vols) if trailing_vols else 1
            vol_ratio = bar["volume"] / max(avg_vol, 1)

            # Price velocity
            velocity_pct = ((bar["close"] - price_at_post) / price_at_post) * 100

            # Spread proxy: normalized high-low range
            midpoint = (bar["high"] + bar["low"]) / 2
            bar_range_pct = ((bar["high"] - bar["low"]) / midpoint * 100) if midpoint > 0 else 0

            trailing_ranges = []
            for j in range(max(0, scan_idx - 15), scan_idx):
                b = bars[j]
                m = (b["high"] + b["low"]) / 2
                if m > 0:
                    trailing_ranges.append((b["high"] - b["low"]) / m * 100)
            avg_range = np.mean(trailing_ranges) if trailing_ranges else 0.01
            spread_widening = ((bar_range_pct - avg_range) / max(avg_range, 0.001)) * 100

            # Check criteria
            vol_ok = vol_ratio >= self.config.volume_spike_multiplier
            vel_ok = abs(velocity_pct) >= self.config.price_velocity_pct
            spread_ok = spread_widening >= self.config.spread_widening_pct

            if sum([vol_ok, vel_ok, spread_ok]) >= self.config.min_criteria_met:
                # Signature detected -- simulate trade
                direction = "LONG" if velocity_pct > 0 else "SHORT"
                entry_price = bar["close"]

                exit_price, exit_reason, hold_bars = self._simulate_trade(
                    bars, scan_idx, entry_price, direction
                )

                if direction == "LONG":
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                else:
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100

                return BacktestResult(
                    post_id=post["id"],
                    post_text=post["text"][:120],
                    post_timestamp=post["date"],
                    category=category,
                    ticker=ticker,
                    signature_detected=True,
                    detection_bar_idx=scan_idx - post_bar_idx,
                    volume_spike_ratio=vol_ratio,
                    price_velocity_pct=velocity_pct,
                    spread_proxy_widening_pct=spread_widening,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    hold_bars=hold_bars,
                    pnl_pct=pnl_pct,
                    pnl_dollars=pnl_pct / 100 * self.config.max_position_dollars,
                )

        # No signature detected
        return BacktestResult(
            post_id=post["id"],
            post_text=post["text"][:120],
            post_timestamp=post["date"],
            category=category,
            ticker=ticker,
            signature_detected=False,
            detection_bar_idx=0,
            volume_spike_ratio=0,
            price_velocity_pct=0,
            spread_proxy_widening_pct=0,
            direction="NONE",
            entry_price=0,
            exit_price=0,
            exit_reason="NO_SIGNAL",
            hold_bars=0,
            pnl_pct=0,
            pnl_dollars=0,
        )

    def _simulate_trade(self, bars: list[dict], entry_idx: int,
                         entry_price: float, direction: str
                         ) -> tuple[float, str, int]:
        """Simulate trade exit using minute bars."""
        stop_pct = self.config.stop_loss_pct / 100
        tp_pct = self.config.take_profit_pct / 100
        max_bars = self.config.max_hold_sec // 60

        for i in range(entry_idx + 1,
                       min(entry_idx + max_bars + 1, len(bars))):
            bar = bars[i]
            hold_bars = i - entry_idx

            if direction == "LONG":
                if bar["low"] <= entry_price * (1 - stop_pct):
                    return entry_price * (1 - stop_pct), "STOP_LOSS", hold_bars
                if bar["high"] >= entry_price * (1 + tp_pct):
                    return entry_price * (1 + tp_pct), "TAKE_PROFIT", hold_bars
            else:
                if bar["high"] >= entry_price * (1 + stop_pct):
                    return entry_price * (1 + stop_pct), "STOP_LOSS", hold_bars
                if bar["low"] <= entry_price * (1 - tp_pct):
                    return entry_price * (1 - tp_pct), "TAKE_PROFIT", hold_bars

        # Max hold time -- exit at last bar close
        last_idx = min(entry_idx + max_bars, len(bars) - 1)
        return bars[last_idx]["close"], "MAX_HOLD_TIME", last_idx - entry_idx

    def _compute_summary(self) -> BacktestSummary:
        """Compute aggregate stats from individual results."""
        total = len(self.results)
        detected = [r for r in self.results if r.signature_detected]
        traded = [r for r in detected if r.pnl_pct != 0]

        pnls = [r.pnl_pct for r in traded]
        wins = [p for p in pnls if p > 0]

        # Sharpe ratio (annualized, assuming ~250 trading days)
        if pnls and np.std(pnls) > 0:
            sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(250)
        else:
            sharpe = 0.0

        # Max drawdown
        cumulative = np.cumsum(pnls) if pnls else [0]
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

        return BacktestSummary(
            total_posts=total,
            signatures_detected=len(detected),
            detection_rate_pct=(len(detected) / max(total, 1)) * 100,
            trades_simulated=len(traded),
            win_rate_pct=(len(wins) / max(len(traded), 1)) * 100,
            avg_pnl_pct=float(np.mean(pnls)) if pnls else 0,
            total_pnl_dollars=sum(r.pnl_dollars for r in traded),
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            avg_hold_bars=float(np.mean([r.hold_bars for r in traded])) if traded else 0,
            avg_detection_bar=float(np.mean([r.detection_bar_idx for r in detected])) if detected else 0,
            best_trade_pnl_pct=max(pnls) if pnls else 0,
            worst_trade_pnl_pct=min(pnls) if pnls else 0,
        )

    def _save_results(self, output_dir: str, summary: BacktestSummary):
        """Save backtest results to files."""
        # Individual trades
        trades_file = os.path.join(output_dir, "backtest_trades.json")
        with open(trades_file, "w") as f:
            json.dump([r.__dict__ for r in self.results], f, indent=2, default=str)

        # Summary
        summary_file = os.path.join(output_dir, "backtest_summary.json")
        with open(summary_file, "w") as f:
            json.dump(summary.__dict__, f, indent=2)

        # Print summary
        print("\n" + "=" * 60)
        print("  BOT DETECTOR BACKTEST RESULTS")
        print("=" * 60)
        print(f"  Posts analyzed:      {summary.total_posts}")
        print(f"  Signatures found:    {summary.signatures_detected} "
              f"({summary.detection_rate_pct:.1f}%)")
        print(f"  Trades simulated:    {summary.trades_simulated}")
        print(f"  Win rate:            {summary.win_rate_pct:.1f}%")
        print(f"  Avg P&L per trade:   {summary.avg_pnl_pct:+.3f}%")
        print(f"  Total P&L:           ${summary.total_pnl_dollars:+.2f}")
        print(f"  Sharpe ratio:        {summary.sharpe_ratio:.2f}")
        print(f"  Max drawdown:        {summary.max_drawdown_pct:.2f}%")
        print(f"  Avg hold time:       {summary.avg_hold_bars:.1f} min")
        print(f"  Avg detection time:  {summary.avg_detection_bar:.1f} min after post")
        print(f"  Best trade:          {summary.best_trade_pnl_pct:+.3f}%")
        print(f"  Worst trade:         {summary.worst_trade_pnl_pct:+.3f}%")
        print("=" * 60)
        print(f"\n  Results saved to: {output_dir}/")


def main():
    bt = Backtester()
    bt.run()


if __name__ == "__main__":
    main()
