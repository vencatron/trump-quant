"""
MarketState — maintains rolling window statistics per ticker.

Fed by WebSocket trade and quote messages. Provides real-time metrics
for bot signature detection.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .models import MarketSnapshot


@dataclass
class TickerState:
    """Rolling state for a single ticker."""

    ticker: str

    # Price tracking
    last_price: float = 0.0
    last_price_time: Optional[datetime] = None
    price_at_arm: Optional[float] = None  # Set when detection window opens
    arm_time: Optional[datetime] = None

    # Volume: deque of (timestamp, volume, price) tuples
    trade_buffer: deque = field(default_factory=lambda: deque(maxlen=50000))

    # Quote tracking
    last_bid: float = 0.0
    last_ask: float = 0.0
    spread_buffer: deque = field(default_factory=lambda: deque(maxlen=10000))
    # spread_buffer entries: (timestamp, spread_pct)

    def update_trade(self, price: float, size: int, timestamp: datetime):
        """Process an incoming trade message."""
        self.last_price = price
        self.last_price_time = timestamp
        self.trade_buffer.append((timestamp, size, price))

    def update_quote(self, bid: float, ask: float, timestamp: datetime):
        """Process an incoming quote message."""
        self.last_bid = bid
        self.last_ask = ask
        midpoint = (bid + ask) / 2.0
        if midpoint > 0:
            spread_pct = (ask - bid) / midpoint * 100.0
            self.spread_buffer.append((timestamp, spread_pct))

    def arm(self, timestamp: datetime):
        """Mark the start of a detection window."""
        self.price_at_arm = self.last_price
        self.arm_time = timestamp

    def disarm(self):
        """Clear detection window."""
        self.price_at_arm = None
        self.arm_time = None

    def get_volume_in_window(self, now: datetime, window_sec: int) -> int:
        """Sum trade volume in the last `window_sec` seconds."""
        cutoff = now.timestamp() - window_sec
        total = 0
        for ts, size, _ in reversed(self.trade_buffer):
            if ts.timestamp() < cutoff:
                break
            total += size
        return total

    def get_rolling_volume_per_sec(self, now: datetime, window_sec: int) -> float:
        """Average volume per second over rolling window."""
        total = self.get_volume_in_window(now, window_sec)
        return total / max(window_sec, 1)

    def get_recent_volume_per_sec(self, now: datetime, recent_sec: int = 5) -> float:
        """Volume per second in the most recent `recent_sec` seconds."""
        total = self.get_volume_in_window(now, recent_sec)
        return total / max(recent_sec, 1)

    def get_volume_spike_ratio(self, now: datetime,
                                rolling_window_sec: int = 900,
                                recent_sec: int = 5) -> float:
        """Ratio of recent volume rate to rolling average rate."""
        rolling = self.get_rolling_volume_per_sec(now, rolling_window_sec)
        recent = self.get_recent_volume_per_sec(now, recent_sec)
        if rolling <= 0:
            return 0.0
        return recent / rolling

    def get_price_velocity_pct(self) -> float:
        """% price change since arm time."""
        if self.price_at_arm is None or self.price_at_arm <= 0:
            return 0.0
        return ((self.last_price - self.price_at_arm) / self.price_at_arm) * 100.0

    def get_spread_baseline(self, now: datetime, window_sec: int = 900) -> float:
        """Average spread % over the rolling window."""
        cutoff = now.timestamp() - window_sec
        spreads = [s for ts, s in self.spread_buffer if ts.timestamp() >= cutoff]
        if not spreads:
            return 0.0
        return sum(spreads) / len(spreads)

    def get_spread_widening_pct(self, now: datetime,
                                 baseline_window_sec: int = 900) -> float:
        """How much current spread exceeds baseline, as a percentage."""
        if not self.last_bid or not self.last_ask:
            return 0.0
        midpoint = (self.last_bid + self.last_ask) / 2.0
        if midpoint <= 0:
            return 0.0
        current_spread_pct = (self.last_ask - self.last_bid) / midpoint * 100.0
        baseline = self.get_spread_baseline(now, baseline_window_sec)
        if baseline <= 0:
            return 0.0
        return ((current_spread_pct - baseline) / baseline) * 100.0

    def get_snapshot(self, now: datetime, rolling_window_sec: int = 900) -> MarketSnapshot:
        """Build a MarketSnapshot from current state."""
        midpoint = (self.last_bid + self.last_ask) / 2.0 if self.last_bid and self.last_ask else self.last_price
        spread = self.last_ask - self.last_bid if self.last_bid and self.last_ask else 0.0
        spread_pct = (spread / midpoint * 100.0) if midpoint > 0 else 0.0

        return MarketSnapshot(
            ticker=self.ticker,
            timestamp=now,
            last_price=self.last_price,
            bid=self.last_bid,
            ask=self.last_ask,
            spread=spread,
            spread_pct=spread_pct,
            volume_1s=self.get_volume_in_window(now, 1),
            volume_rolling=self.get_rolling_volume_per_sec(now, rolling_window_sec),
            volume_spike_ratio=self.get_volume_spike_ratio(now, rolling_window_sec),
            price_at_arm=self.price_at_arm,
            price_velocity_pct=self.get_price_velocity_pct(),
            spread_baseline=self.get_spread_baseline(now, rolling_window_sec),
            spread_widening_pct=self.get_spread_widening_pct(now, rolling_window_sec),
        )


class MarketState:
    """
    Manages TickerState for all watched tickers.

    Usage:
        state = MarketState(["SPY", "QQQ", "DJT"])
        state.on_trade("SPY", price=540.12, size=100, timestamp=now)
        state.on_quote("SPY", bid=540.10, ask=540.14, timestamp=now)
        snapshot = state.get_snapshot("SPY")
    """

    def __init__(self, tickers: list[str]):
        self._states: dict[str, TickerState] = {
            t: TickerState(ticker=t) for t in tickers
        }

    def on_trade(self, ticker: str, price: float, size: int, timestamp: datetime):
        if ticker in self._states:
            self._states[ticker].update_trade(price, size, timestamp)

    def on_quote(self, ticker: str, bid: float, ask: float, timestamp: datetime):
        if ticker in self._states:
            self._states[ticker].update_quote(bid, ask, timestamp)

    def arm(self, ticker: str, timestamp: datetime):
        if ticker in self._states:
            self._states[ticker].arm(timestamp)

    def arm_all(self, timestamp: datetime):
        for state in self._states.values():
            state.arm(timestamp)

    def disarm(self, ticker: str):
        if ticker in self._states:
            self._states[ticker].disarm()

    def disarm_all(self):
        for state in self._states.values():
            state.disarm()

    def get_snapshot(self, ticker: str, now: datetime = None) -> Optional[MarketSnapshot]:
        if ticker not in self._states:
            return None
        now = now or datetime.now(timezone.utc)
        return self._states[ticker].get_snapshot(now)

    def get_state(self, ticker: str) -> Optional[TickerState]:
        return self._states.get(ticker)
