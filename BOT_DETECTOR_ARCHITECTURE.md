# Bot Detector — Technical Architecture

> **System:** TrumpQuant Bot Detector  
> **Author:** Hamilton (Quant Systems Architect)  
> **Date:** 2026-03-23  
> **Status:** Implementation-Ready Blueprint  
> **Target:** Claude Code coding agent

---

## 1. Executive Summary

The Bot Detector extends TrumpQuant from a correlation-based alert system into a **microstructure-aware trading system**. Instead of predicting market direction from post sentiment, we **observe the algorithmic reaction** to Trump posts in real-time and trade the confirmed momentum.

**Core thesis:** When Trump posts, algorithmic trading bots react within 30-120 seconds with characteristic volume/price/spread signatures. By detecting this signature, we confirm the move is real (not just noise) and enter with momentum.

**We are not predicting. We are observing and following.**

---

## 2. File Structure

```
trumpquant/
├── signal_check.py              # EXISTING — modify to call bot_detector on signal
├── monitor.py                   # EXISTING — no changes needed
├── categorize.py                # EXISTING — no changes needed
├── correlate.py                 # EXISTING — no changes needed
├── signals.py                   # EXISTING — no changes needed
├── fetch_market.py              # EXISTING — no changes needed
├── requirements.txt             # MODIFY — add alpaca-py, websockets
│
├── botdetector/                 # NEW — all new code lives here
│   ├── __init__.py              # Package init, version string
│   ├── config.py                # All thresholds, env vars, feature flags
│   ├── bot_detector.py          # Core: WebSocket listener + signature detection
│   ├── trade_executor.py        # Position entry/exit/sizing via Alpaca REST
│   ├── risk_manager.py          # Daily loss limits, position caps, kill switch
│   ├── market_state.py          # Rolling window state: volume, price, spread
│   ├── models.py                # Dataclasses: BotSignal, Trade, MarketSnapshot
│   ├── alpaca_client.py         # Thin wrapper around Alpaca REST + WebSocket
│   ├── notifier.py              # Telegram alerts for trades (reuses openclaw)
│   ├── backtest.py              # Replay historical posts against minute-bar data
│   ├── logger.py                # Structured JSON logging
│   └── cli.py                   # Entry point: `python -m botdetector`
│
├── data/
│   ├── bot_trades.json          # NEW — trade log
│   ├── bot_signals.json         # NEW — detected signal log
│   ├── kill_switch.flag         # NEW — touch this file = halt all trading
│   └── backtest_results/        # NEW — backtest output dir
│
└── tests/
    ├── test_bot_detector.py     # Unit tests for signature detection
    ├── test_trade_executor.py   # Unit tests for position sizing/exit logic
    ├── test_risk_manager.py     # Unit tests for risk controls
    ├── test_market_state.py     # Unit tests for rolling window math
    └── test_integration.py      # End-to-end with mock WebSocket
```

---

## 3. Data Flow Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                        TRIGGER LAYER                                 │
│                                                                      │
│  signal_check.py (cron every 15 min)                                │
│       │                                                              │
│       ▼                                                              │
│  New Trump post detected + categorized                              │
│       │                                                              │
│       ▼                                                              │
│  Category in SIGNAL_CATEGORIES? ──No──► Log & skip                  │
│       │                                                              │
│      Yes                                                             │
│       │                                                              │
│       ▼                                                              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  BotDetector.arm(post, categories, tickers_to_watch)       │    │
│  │  Starts 120-second detection window                        │    │
│  └──────────────────────┬──────────────────────────────────────┘    │
│                         │                                            │
└─────────────────────────┼────────────────────────────────────────────┘
                          │
┌─────────────────────────┼────────────────────────────────────────────┐
│                    DETECTION LAYER                                    │
│                         │                                            │
│                         ▼                                            │
│  Alpaca WebSocket (already connected, always streaming)             │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Subscribed to: trades + quotes for watchlist tickers        │   │
│  │  (SPY, QQQ, DJT, COIN, TSLA, NVDA, GME, META, GLD)        │   │
│  └────────────┬─────────────────────────────────┬──────────────┘   │
│               │                                 │                    │
│          Trade msgs                        Quote msgs                │
│               │                                 │                    │
│               ▼                                 ▼                    │
│     MarketState.update_trade()       MarketState.update_quote()     │
│     - accumulate volume              - track bid/ask spread         │
│     - compute price velocity         - compute spread baseline      │
│               │                                 │                    │
│               └────────────┬────────────────────┘                    │
│                            │                                         │
│                            ▼                                         │
│              BotDetector._check_signature()                         │
│              ┌──────────────────────────────────┐                   │
│              │ ✓ Volume ≥ 3x 15-min avg?        │                   │
│              │ ✓ Price velocity ≥ 0.3% / 60s?   │                   │
│              │ ✓ Spread ≥ 1.5x baseline?         │                   │
│              │                                    │                   │
│              │ All 3 confirmed within 120s?       │                   │
│              └──────────────┬─────────────────────┘                   │
│                             │                                        │
│                     No ─────┤───── Yes                               │
│                     │       │       │                                 │
│                     ▼       │       ▼                                 │
│              Log "no sig"   │  BotSignal created                     │
│              Disarm         │       │                                 │
│                             │       │                                 │
└─────────────────────────────┼───────┼────────────────────────────────┘
                              │       │
┌─────────────────────────────┼───────┼────────────────────────────────┐
│                    EXECUTION LAYER   │                                │
│                                     │                                │
│                                     ▼                                │
│                  RiskManager.check_can_trade()                       │
│                  ┌──────────────────────────────┐                    │
│                  │ Kill switch file exists? HALT │                    │
│                  │ Daily loss limit hit? HALT    │                    │
│                  │ Max positions open? HALT      │                    │
│                  │ Market hours? CHECK           │                    │
│                  └──────────────┬────────────────┘                    │
│                                │                                     │
│                          Pass? │                                     │
│                                ▼                                     │
│                  TradeExecutor.execute(signal)                       │
│                  ┌──────────────────────────────┐                    │
│                  │ 1. Determine direction        │                    │
│                  │ 2. Calculate position size    │                    │
│                  │ 3. Set stop loss              │                    │
│                  │ 4. Submit order (paper/live)  │                    │
│                  │ 5. Start exit timer           │                    │
│                  └──────────────┬────────────────┘                    │
│                                │                                     │
│                                ▼                                     │
│                  Notifier.send_trade_alert()                         │
│                  (Telegram to Ron)                                   │
│                                │                                     │
│                                ▼                                     │
│                  TradeExecutor.monitor_exit()                        │
│                  ┌──────────────────────────────┐                    │
│                  │ Check every 5s:               │                    │
│                  │ - Stop loss hit? → close      │                    │
│                  │ - Take profit hit? → close    │                    │
│                  │ - Max hold time? → close      │                    │
│                  │ - Trailing stop? → adjust     │                    │
│                  └──────────────────────────────┘                    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 4. Module Specifications

### 4.1 `config.py` — Configuration & Thresholds

```python
"""Central configuration. All tunable parameters in one place."""

import os
from dataclasses import dataclass, field

@dataclass(frozen=True)
class BotDetectorConfig:
    """Immutable config loaded at startup."""

    # === Alpaca API ===
    alpaca_api_key: str = os.environ.get("ALPACA_API_KEY", "")
    alpaca_secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")
    alpaca_base_url: str = os.environ.get(
        "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
    )
    alpaca_data_ws: str = "wss://stream.data.alpaca.markets/v2/iex"
    paper_mode: bool = True  # ALWAYS start paper. Explicit toggle to go live.

    # === Watchlist ===
    # Tickers to stream and monitor for bot signatures
    watchlist: tuple[str, ...] = (
        "SPY", "QQQ", "DJT", "COIN", "TSLA", "NVDA", "GME", "META", "GLD",
    )
    # Category → primary ticker mapping (which ticker to trade per signal)
    category_tickers: dict[str, str] = field(default_factory=lambda: {
        "TARIFFS": "SPY",
        "TRADE_DEAL": "SPY",
        "CRYPTO": "COIN",
        "FED_ATTACK": "SPY",
        "MARKET_PUMP": "SPY",
        "SPECIFIC_TICKER": "DJT",
    })

    # === Bot Signature Detection Thresholds ===
    detection_window_sec: int = 120        # Seconds after post to watch
    volume_spike_multiplier: float = 3.0   # Volume must be ≥ 3x rolling avg
    volume_rolling_window_sec: int = 900   # 15-minute rolling window for avg
    price_velocity_pct: float = 0.3        # ≥ 0.3% move required
    price_velocity_window_sec: int = 60    # Within 60 seconds
    spread_widening_pct: float = 50.0      # Spread ≥ 50% above baseline
    spread_baseline_window_sec: int = 900  # 15-min baseline for spread
    min_criteria_met: int = 3              # Must meet 3 of 3 criteria

    # === Trade Execution ===
    max_position_pct: float = 0.05         # Max 5% of portfolio per trade
    max_position_dollars: float = 2500.0   # Hard cap $2,500 per trade
    stop_loss_pct: float = 0.5             # 0.5% stop loss
    take_profit_pct: float = 1.5           # 1.5% take profit
    trailing_stop_pct: float = 0.3         # 0.3% trailing stop (activates after 0.5% gain)
    trailing_stop_activation_pct: float = 0.5
    min_hold_sec: int = 60                 # Don't exit before 60s (avoid whipsaw)
    max_hold_sec: int = 3600               # Force close after 60 min
    default_hold_sec: int = 1800           # Default target hold: 30 min
    exit_check_interval_sec: int = 5       # Check exit conditions every 5s

    # === Risk Controls ===
    max_daily_loss_dollars: float = 500.0  # Stop trading after $500 daily loss
    max_daily_trades: int = 5              # Max 5 trades per day
    max_concurrent_positions: int = 2      # Max 2 open positions at once
    cooldown_after_loss_sec: int = 1800    # 30-min cooldown after a losing trade
    kill_switch_file: str = "data/kill_switch.flag"

    # === Paths ===
    trade_log_file: str = "data/bot_trades.json"
    signal_log_file: str = "data/bot_signals.json"
    backtest_dir: str = "data/backtest_results"

    # === Telegram ===
    telegram_user_id: str = "8387647137"
```

---

### 4.2 `models.py` — Data Structures

```python
"""Dataclasses for the bot detector system."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class ExitReason(Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    MAX_HOLD_TIME = "MAX_HOLD_TIME"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"
    MARKET_CLOSE = "MARKET_CLOSE"


@dataclass
class MarketSnapshot:
    """Point-in-time market state for a single ticker."""
    ticker: str
    timestamp: datetime
    last_price: float
    bid: float
    ask: float
    spread: float                      # ask - bid
    spread_pct: float                  # spread / midpoint * 100
    volume_1s: int                     # Volume in last 1 second
    volume_rolling: float              # Rolling avg volume per second (15-min)
    volume_spike_ratio: float          # volume_1s / volume_rolling
    price_at_arm: Optional[float]      # Price when detection window opened
    price_velocity_pct: float          # % move from price_at_arm
    spread_baseline: float             # Baseline spread (15-min avg)
    spread_widening_pct: float         # Current spread vs baseline, %


@dataclass
class BotSignal:
    """A confirmed bot activity signature."""
    signal_id: str                     # UUID
    ticker: str
    direction: SignalDirection
    post_id: str                       # Link to TrumpQuant post ID
    post_text: str
    post_categories: list[str]
    post_timestamp: datetime
    detection_timestamp: datetime
    seconds_after_post: float

    # Signature metrics at detection time
    volume_spike_ratio: float          # e.g., 4.2x
    price_velocity_pct: float          # e.g., -0.45%
    spread_widening_pct: float         # e.g., 78%

    # Market context
    entry_price: float                 # Price at signal confirmation
    snapshot: MarketSnapshot

    # Metadata
    confidence: str = "DETECTED"       # DETECTED | STRONG (if >> thresholds)


@dataclass
class Trade:
    """A single trade from entry to exit."""
    trade_id: str                      # UUID
    signal_id: str                     # Links to BotSignal
    ticker: str
    direction: SignalDirection
    status: TradeStatus = TradeStatus.PENDING

    # Entry
    entry_price: float = 0.0
    entry_timestamp: Optional[datetime] = None
    entry_order_id: str = ""
    shares: int = 0
    position_value: float = 0.0

    # Exit
    exit_price: float = 0.0
    exit_timestamp: Optional[datetime] = None
    exit_order_id: str = ""
    exit_reason: Optional[ExitReason] = None

    # Risk levels (set at entry)
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    trailing_stop_price: float = 0.0
    max_exit_time: Optional[datetime] = None

    # P&L
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    commissions: float = 0.0

    # Flags
    paper_mode: bool = True


@dataclass
class DailyRiskState:
    """Tracks daily risk metrics. Reset at market open."""
    date: str                          # YYYY-MM-DD
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: list[str] = field(default_factory=list)  # trade_ids
    last_loss_timestamp: Optional[datetime] = None
    halted: bool = False
    halt_reason: str = ""
```

---

### 4.3 `market_state.py` — Rolling Window State Tracker

```python
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

    # Volume: deque of (timestamp, volume) tuples
    # Each entry is a single trade message
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
```

---

### 4.4 `alpaca_client.py` — Alpaca API Wrapper

```python
"""
Thin wrapper around Alpaca REST + WebSocket.
Isolates all Alpaca-specific API calls.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from .config import BotDetectorConfig

logger = logging.getLogger("botdetector.alpaca")


class AlpacaRESTClient:
    """REST API for account info, orders, positions."""

    def __init__(self, config: BotDetectorConfig):
        self.config = config
        self.client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper_mode,
        )
        self.data_client = StockHistoricalDataClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
        )

    def get_account(self) -> dict:
        """Get account info (buying power, equity, etc.)."""
        acct = self.client.get_account()
        return {
            "equity": float(acct.equity),
            "buying_power": float(acct.buying_power),
            "cash": float(acct.cash),
            "portfolio_value": float(acct.portfolio_value),
            "pattern_day_trader": acct.pattern_day_trader,
        }

    def submit_market_order(self, ticker: str, qty: int,
                             side: str, time_in_force: str = "day") -> dict:
        """Submit a market order. Returns order dict."""
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC
        req = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=order_side,
            time_in_force=tif,
        )
        order = self.client.submit_order(req)
        return {
            "order_id": str(order.id),
            "status": str(order.status),
            "filled_qty": float(order.filled_qty or 0),
            "filled_avg_price": float(order.filled_avg_price or 0),
            "submitted_at": str(order.submitted_at),
        }

    def get_position(self, ticker: str) -> Optional[dict]:
        """Get current position for a ticker."""
        try:
            pos = self.client.get_open_position(ticker)
            return {
                "ticker": pos.symbol,
                "qty": int(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "unrealized_pl": float(pos.unrealized_pl),
                "market_value": float(pos.market_value),
            }
        except Exception:
            return None

    def close_position(self, ticker: str) -> dict:
        """Close entire position for a ticker."""
        order = self.client.close_position(ticker)
        return {"order_id": str(order.id), "status": str(order.status)}

    def get_bars(self, ticker: str, timeframe: str = "1Min",
                  start: str = None, end: str = None, limit: int = 1000) -> list[dict]:
        """Fetch historical bars for backtesting."""
        tf = TimeFrame.Minute if timeframe == "1Min" else TimeFrame.Day
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
        )
        bars = self.data_client.get_stock_bars(req)
        return [
            {
                "timestamp": str(bar.timestamp),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            }
            for bar in bars[ticker]
        ]


class AlpacaWSClient:
    """
    WebSocket client for real-time trades and quotes.

    Connects to Alpaca IEX data stream. Calls registered
    handlers on each trade/quote message.
    """

    def __init__(self, config: BotDetectorConfig):
        self.config = config
        self._ws = None
        self._on_trade: Optional[Callable] = None
        self._on_quote: Optional[Callable] = None
        self._running = False

    def set_handlers(self, on_trade: Callable, on_quote: Callable):
        """
        Register callbacks.
        on_trade(ticker: str, price: float, size: int, timestamp: datetime)
        on_quote(ticker: str, bid: float, ask: float, timestamp: datetime)
        """
        self._on_trade = on_trade
        self._on_quote = on_quote

    async def connect_and_stream(self, tickers: list[str]):
        """
        Connect to Alpaca WebSocket, authenticate, subscribe, and stream.
        Reconnects on disconnect with exponential backoff.
        """
        self._running = True
        backoff = 1

        while self._running:
            try:
                async with websockets.connect(self.config.alpaca_data_ws) as ws:
                    self._ws = ws
                    # Read welcome
                    await ws.recv()

                    # Authenticate
                    auth_msg = {
                        "action": "auth",
                        "key": self.config.alpaca_api_key,
                        "secret": self.config.alpaca_secret_key,
                    }
                    await ws.send(json.dumps(auth_msg))
                    auth_resp = await ws.recv()
                    logger.info(f"Auth response: {auth_resp}")

                    # Subscribe to trades and quotes
                    sub_msg = {
                        "action": "subscribe",
                        "trades": tickers,
                        "quotes": tickers,
                    }
                    await ws.send(json.dumps(sub_msg))
                    sub_resp = await ws.recv()
                    logger.info(f"Subscribe response: {sub_resp}")

                    backoff = 1  # Reset backoff on successful connect

                    # Stream loop
                    async for raw_msg in ws:
                        msgs = json.loads(raw_msg)
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        for msg in msgs:
                            self._dispatch(msg)

            except (websockets.ConnectionClosed, ConnectionError) as e:
                logger.warning(f"WebSocket disconnected: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                logger.error(f"WebSocket error: {e}", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _dispatch(self, msg: dict):
        """Route a message to the appropriate handler."""
        msg_type = msg.get("T")
        if msg_type == "t" and self._on_trade:
            # Trade message
            ts = datetime.fromisoformat(msg["t"].replace("Z", "+00:00"))
            self._on_trade(
                ticker=msg["S"],
                price=float(msg["p"]),
                size=int(msg["s"]),
                timestamp=ts,
            )
        elif msg_type == "q" and self._on_quote:
            # Quote message
            ts = datetime.fromisoformat(msg["t"].replace("Z", "+00:00"))
            self._on_quote(
                ticker=msg["S"],
                bid=float(msg["bp"]),
                ask=float(msg["ap"]),
                timestamp=ts,
            )

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
```

---

### 4.5 `bot_detector.py` — Core Detection Engine

```python
"""
BotDetector — the core engine.

Lifecycle:
1. Starts up, connects WebSocket, begins streaming market data
2. Waits for arm() call from signal_check.py (via IPC or direct call)
3. On arm(): records price_at_arm, starts 120-second detection window
4. Every incoming trade/quote: updates MarketState, checks signature
5. If 3/3 criteria met: creates BotSignal, calls TradeExecutor
6. After 120s or signal confirmed: disarms

Can run as:
- Long-running daemon (python -m botdetector daemon)
- One-shot triggered by signal_check.py (python -m botdetector arm --post-id X)
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from .config import BotDetectorConfig
from .models import BotSignal, SignalDirection
from .market_state import MarketState
from .alpaca_client import AlpacaWSClient
from .trade_executor import TradeExecutor
from .risk_manager import RiskManager
from .notifier import Notifier
from .logger import log_signal

logger = logging.getLogger("botdetector.core")


class BotDetector:
    """
    Main detection engine.

    Architecture: single async event loop.
    - WebSocket stream feeds MarketState
    - arm() sets a detection window
    - _check_signature() evaluates on every tick during armed window
    - On confirmation, delegates to TradeExecutor
    """

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()
        self.market_state = MarketState(list(self.config.watchlist))
        self.ws_client = AlpacaWSClient(self.config)
        self.executor = TradeExecutor(self.config)
        self.risk_manager = RiskManager(self.config)
        self.notifier = Notifier(self.config)

        # Detection state
        self._armed = False
        self._arm_time: Optional[datetime] = None
        self._arm_post: Optional[dict] = None  # {id, text, categories, tickers}
        self._arm_tickers: list[str] = []       # Which tickers to watch
        self._signal_fired = False               # Prevent duplicate signals per arm

        # Criteria tracking (per arm window)
        self._volume_confirmed = False
        self._velocity_confirmed = False
        self._spread_confirmed = False
        self._volume_confirm_time: Optional[datetime] = None
        self._velocity_confirm_time: Optional[datetime] = None
        self._spread_confirm_time: Optional[datetime] = None

        # Wire up WebSocket handlers
        self.ws_client.set_handlers(
            on_trade=self._handle_trade,
            on_quote=self._handle_quote,
        )

    # ─── Public API ───

    def arm(self, post_id: str, post_text: str, categories: list[str],
            tickers: list[str] = None, timestamp: datetime = None):
        """
        Start a detection window.

        Called by signal_check.py when a new Trump post matches
        a signal category.

        Args:
            post_id: TrumpQuant post ID
            post_text: Post headline text
            categories: List of categories (TARIFFS, CRYPTO, etc.)
            tickers: Override which tickers to watch (default: use category mapping)
            timestamp: Post timestamp (default: now)
        """
        if self._armed:
            logger.warning("Already armed — ignoring new arm request")
            return

        now = timestamp or datetime.now(timezone.utc)

        # Determine tickers to watch
        if tickers:
            self._arm_tickers = tickers
        else:
            self._arm_tickers = list(set(
                self.config.category_tickers.get(cat, "SPY")
                for cat in categories
            ))

        self._arm_post = {
            "id": post_id,
            "text": post_text,
            "categories": categories,
        }
        self._arm_time = now
        self._armed = True
        self._signal_fired = False

        # Reset criteria
        self._volume_confirmed = False
        self._velocity_confirmed = False
        self._spread_confirmed = False
        self._volume_confirm_time = None
        self._velocity_confirm_time = None
        self._spread_confirm_time = None

        # Arm market state for target tickers
        for ticker in self._arm_tickers:
            self.market_state.arm(ticker, now)

        logger.info(
            f"ARMED: post={post_id}, tickers={self._arm_tickers}, "
            f"categories={categories}, window={self.config.detection_window_sec}s"
        )

    def disarm(self):
        """End the detection window."""
        if not self._armed:
            return
        self._armed = False
        self.market_state.disarm_all()
        elapsed = 0
        if self._arm_time:
            elapsed = (datetime.now(timezone.utc) - self._arm_time).total_seconds()
        logger.info(
            f"DISARMED after {elapsed:.1f}s. "
            f"Signal fired: {self._signal_fired}"
        )

    async def run_daemon(self):
        """
        Long-running mode. Connects WebSocket and streams indefinitely.
        arm()/disarm() called externally (via file watch, HTTP, or direct import).
        """
        logger.info("Bot Detector daemon starting...")

        # Start WebSocket in background
        ws_task = asyncio.create_task(
            self.ws_client.connect_and_stream(list(self.config.watchlist))
        )

        # Start timeout checker
        timeout_task = asyncio.create_task(self._timeout_loop())

        try:
            await asyncio.gather(ws_task, timeout_task)
        except asyncio.CancelledError:
            logger.info("Daemon shutting down...")
            await self.ws_client.stop()

    async def run_oneshot(self, post_id: str, post_text: str,
                           categories: list[str], tickers: list[str] = None):
        """
        One-shot mode: arm, wait for detection window, then exit.
        Used when called from signal_check.py.
        """
        self.arm(post_id, post_text, categories, tickers)

        # Connect and stream for detection_window_sec + buffer
        timeout = self.config.detection_window_sec + 10

        ws_task = asyncio.create_task(
            self.ws_client.connect_and_stream(list(self.config.watchlist))
        )

        try:
            await asyncio.wait_for(
                self._wait_for_signal_or_timeout(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.info("Detection window expired — no bot signature confirmed")
        finally:
            self.disarm()
            await self.ws_client.stop()
            ws_task.cancel()

    # ─── Internal Handlers ───

    def _handle_trade(self, ticker: str, price: float, size: int,
                       timestamp: datetime):
        """Called by WebSocket on every trade message."""
        self.market_state.on_trade(ticker, price, size, timestamp)

        if self._armed and ticker in self._arm_tickers and not self._signal_fired:
            self._check_signature(ticker, timestamp)

    def _handle_quote(self, ticker: str, bid: float, ask: float,
                       timestamp: datetime):
        """Called by WebSocket on every quote message."""
        self.market_state.on_quote(ticker, bid, ask, timestamp)

        if self._armed and ticker in self._arm_tickers and not self._signal_fired:
            self._check_signature(ticker, timestamp)

    def _check_signature(self, ticker: str, now: datetime):
        """
        Core detection algorithm. Called on every tick during armed window.

        Checks 3 criteria independently. When all 3 are confirmed
        (not necessarily simultaneously, but all within the detection window),
        fires a signal.
        """
        state = self.market_state.get_state(ticker)
        if state is None:
            return

        cfg = self.config

        # Check timeout
        if self._arm_time:
            elapsed = (now - self._arm_time).total_seconds()
            if elapsed > cfg.detection_window_sec:
                return  # Window expired, timeout_loop will disarm

        # ── Criterion 1: Volume Spike ──
        if not self._volume_confirmed:
            ratio = state.get_volume_spike_ratio(
                now, cfg.volume_rolling_window_sec, recent_sec=5
            )
            if ratio >= cfg.volume_spike_multiplier:
                self._volume_confirmed = True
                self._volume_confirm_time = now
                logger.info(
                    f"[{ticker}] ✓ Volume spike confirmed: {ratio:.1f}x "
                    f"(threshold: {cfg.volume_spike_multiplier}x)"
                )

        # ── Criterion 2: Price Velocity ──
        if not self._velocity_confirmed:
            velocity = abs(state.get_price_velocity_pct())
            if velocity >= cfg.price_velocity_pct:
                # Also check it happened within velocity window
                if self._arm_time:
                    secs_since_arm = (now - self._arm_time).total_seconds()
                    if secs_since_arm <= cfg.price_velocity_window_sec:
                        self._velocity_confirmed = True
                        self._velocity_confirm_time = now
                        logger.info(
                            f"[{ticker}] ✓ Price velocity confirmed: "
                            f"{state.get_price_velocity_pct():+.3f}% "
                            f"in {secs_since_arm:.0f}s "
                            f"(threshold: ±{cfg.price_velocity_pct}%)"
                        )

        # ── Criterion 3: Spread Widening ──
        if not self._spread_confirmed:
            widening = state.get_spread_widening_pct(
                now, cfg.spread_baseline_window_sec
            )
            if widening >= cfg.spread_widening_pct:
                self._spread_confirmed = True
                self._spread_confirm_time = now
                logger.info(
                    f"[{ticker}] ✓ Spread widening confirmed: {widening:.1f}% "
                    f"(threshold: {cfg.spread_widening_pct}%)"
                )

        # ── Check if all 3 confirmed ──
        criteria_met = sum([
            self._volume_confirmed,
            self._velocity_confirmed,
            self._spread_confirmed,
        ])

        if criteria_met >= cfg.min_criteria_met:
            self._fire_signal(ticker, now)

    def _fire_signal(self, ticker: str, now: datetime):
        """All criteria met — create signal and hand off to executor."""
        self._signal_fired = True
        state = self.market_state.get_state(ticker)
        snapshot = self.market_state.get_snapshot(ticker, now)

        # Determine direction from price velocity
        velocity = state.get_price_velocity_pct()
        direction = SignalDirection.LONG if velocity > 0 else SignalDirection.SHORT

        signal = BotSignal(
            signal_id=str(uuid.uuid4()),
            ticker=ticker,
            direction=direction,
            post_id=self._arm_post["id"],
            post_text=self._arm_post["text"],
            post_categories=self._arm_post["categories"],
            post_timestamp=self._arm_time,
            detection_timestamp=now,
            seconds_after_post=(now - self._arm_time).total_seconds(),
            volume_spike_ratio=state.get_volume_spike_ratio(now),
            price_velocity_pct=velocity,
            spread_widening_pct=state.get_spread_widening_pct(now),
            entry_price=state.last_price,
            snapshot=snapshot,
            confidence=self._assess_confidence(state, now),
        )

        logger.info(
            f"🚨 BOT SIGNATURE CONFIRMED: {ticker} {direction.value} "
            f"vol={signal.volume_spike_ratio:.1f}x "
            f"vel={signal.price_velocity_pct:+.3f}% "
            f"spread={signal.spread_widening_pct:.1f}% "
            f"after {signal.seconds_after_post:.0f}s"
        )

        # Log signal
        log_signal(signal)

        # Check risk before executing
        can_trade, reason = self.risk_manager.check_can_trade(signal)
        if not can_trade:
            logger.warning(f"Risk check BLOCKED trade: {reason}")
            self.notifier.send_blocked_alert(signal, reason)
            return

        # Execute trade
        trade = self.executor.execute(signal)
        if trade:
            self.notifier.send_trade_alert(signal, trade)
            # Start monitoring exit in background
            asyncio.create_task(self.executor.monitor_exit(trade))

    def _assess_confidence(self, state, now: datetime) -> str:
        """
        Rate confidence based on how far above thresholds we are.
        STRONG if all 3 criteria are ≥ 2x the threshold.
        """
        cfg = self.config
        vol_ratio = state.get_volume_spike_ratio(now) / cfg.volume_spike_multiplier
        vel_ratio = abs(state.get_price_velocity_pct()) / cfg.price_velocity_pct
        spd_ratio = state.get_spread_widening_pct(now) / max(cfg.spread_widening_pct, 1)

        if vol_ratio >= 2.0 and vel_ratio >= 2.0 and spd_ratio >= 2.0:
            return "STRONG"
        return "DETECTED"

    async def _wait_for_signal_or_timeout(self):
        """Wait until signal fires or detection window expires."""
        while self._armed and not self._signal_fired:
            if self._arm_time:
                elapsed = (datetime.now(timezone.utc) - self._arm_time).total_seconds()
                if elapsed > self.config.detection_window_sec:
                    return
            await asyncio.sleep(0.1)

    async def _timeout_loop(self):
        """Background task: disarms after detection window expires."""
        while True:
            if self._armed and self._arm_time:
                elapsed = (datetime.now(timezone.utc) - self._arm_time).total_seconds()
                if elapsed > self.config.detection_window_sec:
                    if not self._signal_fired:
                        logger.info("Detection window expired — no signature")
                    self.disarm()
            await asyncio.sleep(1)
```

---

### 4.6 `trade_executor.py` — Trade Execution

```python
"""
TradeExecutor — position sizing, entry, exit management.

Executes trades via Alpaca REST API based on confirmed BotSignals.
Manages exits via stop loss, take profit, trailing stop, and time-based close.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import BotDetectorConfig
from .models import (
    BotSignal, Trade, TradeStatus, ExitReason, SignalDirection
)
from .alpaca_client import AlpacaRESTClient
from .notifier import Notifier

logger = logging.getLogger("botdetector.executor")


class TradeExecutor:

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()
        self.rest = AlpacaRESTClient(self.config)
        self.notifier = Notifier(self.config)
        self._active_trades: dict[str, Trade] = {}  # trade_id → Trade

    def execute(self, signal: BotSignal) -> Optional[Trade]:
        """
        Execute a trade based on a confirmed bot signal.

        Steps:
        1. Calculate position size
        2. Determine entry side (buy/sell-short)
        3. Calculate stop loss, take profit levels
        4. Submit market order
        5. Return Trade object for exit monitoring

        Returns None if order fails.
        """
        try:
            # Get account for position sizing
            account = self.rest.get_account()
            equity = account["equity"]

            # Position sizing
            shares, position_value = self._calculate_position_size(
                signal.entry_price, equity
            )

            if shares <= 0:
                logger.warning(f"Position size = 0 shares, skipping trade")
                return None

            # Determine side
            side = "buy" if signal.direction == SignalDirection.LONG else "sell"

            # Calculate exit levels
            stop_loss = self._calculate_stop_loss(
                signal.entry_price, signal.direction
            )
            take_profit = self._calculate_take_profit(
                signal.entry_price, signal.direction
            )

            # Create trade record
            trade = Trade(
                trade_id=str(uuid.uuid4()),
                signal_id=signal.signal_id,
                ticker=signal.ticker,
                direction=signal.direction,
                shares=shares,
                position_value=position_value,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                trailing_stop_price=stop_loss,  # Initially same as stop loss
                max_exit_time=datetime.now(timezone.utc) + timedelta(
                    seconds=self.config.max_hold_sec
                ),
                paper_mode=self.config.paper_mode,
            )

            # Submit order
            logger.info(
                f"Submitting {side} order: {shares} shares of {signal.ticker} "
                f"@ ~${signal.entry_price:.2f} "
                f"(SL: ${stop_loss:.2f}, TP: ${take_profit:.2f})"
            )

            order = self.rest.submit_market_order(
                ticker=signal.ticker,
                qty=shares,
                side=side,
            )

            trade.entry_order_id = order["order_id"]
            trade.entry_price = order.get("filled_avg_price", signal.entry_price)
            trade.entry_timestamp = datetime.now(timezone.utc)
            trade.status = TradeStatus.FILLED

            # Recalculate stops based on actual fill price
            if trade.entry_price > 0 and trade.entry_price != signal.entry_price:
                trade.stop_loss_price = self._calculate_stop_loss(
                    trade.entry_price, signal.direction
                )
                trade.take_profit_price = self._calculate_take_profit(
                    trade.entry_price, signal.direction
                )
                trade.trailing_stop_price = trade.stop_loss_price

            self._active_trades[trade.trade_id] = trade

            logger.info(
                f"✅ ORDER FILLED: {side} {shares}x {signal.ticker} "
                f"@ ${trade.entry_price:.2f} "
                f"(value: ${position_value:.2f})"
            )

            return trade

        except Exception as e:
            logger.error(f"Trade execution failed: {e}", exc_info=True)
            return None

    def _calculate_position_size(self, price: float, equity: float) -> tuple[int, float]:
        """
        Calculate position size.

        Rules:
        - Max config.max_position_pct of portfolio equity
        - Hard cap of config.max_position_dollars
        - Round down to whole shares
        - Minimum 1 share

        Returns: (shares, position_value_dollars)
        """
        max_by_pct = equity * self.config.max_position_pct
        max_dollars = min(max_by_pct, self.config.max_position_dollars)

        if price <= 0:
            return 0, 0.0

        shares = int(max_dollars / price)
        if shares <= 0:
            return 0, 0.0

        position_value = shares * price
        return shares, position_value

    def _calculate_stop_loss(self, entry_price: float,
                              direction: SignalDirection) -> float:
        """Calculate stop loss price."""
        pct = self.config.stop_loss_pct / 100.0
        if direction == SignalDirection.LONG:
            return entry_price * (1 - pct)
        else:
            return entry_price * (1 + pct)

    def _calculate_take_profit(self, entry_price: float,
                                direction: SignalDirection) -> float:
        """Calculate take profit price."""
        pct = self.config.take_profit_pct / 100.0
        if direction == SignalDirection.LONG:
            return entry_price * (1 + pct)
        else:
            return entry_price * (1 - pct)

    def _update_trailing_stop(self, trade: Trade, current_price: float):
        """
        Update trailing stop if price has moved favorably.

        Trailing stop activates after price moves trailing_stop_activation_pct
        in our favor, then trails at trailing_stop_pct.
        """
        entry = trade.entry_price
        activation_pct = self.config.trailing_stop_activation_pct / 100.0
        trail_pct = self.config.trailing_stop_pct / 100.0

        if trade.direction == SignalDirection.LONG:
            gain_pct = (current_price - entry) / entry
            if gain_pct >= activation_pct:
                new_stop = current_price * (1 - trail_pct)
                if new_stop > trade.trailing_stop_price:
                    trade.trailing_stop_price = new_stop
                    logger.debug(
                        f"Trailing stop updated: ${new_stop:.2f} "
                        f"(price: ${current_price:.2f})"
                    )
        else:  # SHORT
            gain_pct = (entry - current_price) / entry
            if gain_pct >= activation_pct:
                new_stop = current_price * (1 + trail_pct)
                if new_stop < trade.trailing_stop_price:
                    trade.trailing_stop_price = new_stop

    async def monitor_exit(self, trade: Trade):
        """
        Monitor an open trade for exit conditions.

        Checks every exit_check_interval_sec:
        1. Kill switch file
        2. Stop loss hit
        3. Trailing stop hit
        4. Take profit hit
        5. Max hold time exceeded
        6. Market close approaching (close 5 min before)
        """
        import os
        logger.info(f"Monitoring exit for trade {trade.trade_id[:8]}...")

        min_exit_time = trade.entry_timestamp + timedelta(
            seconds=self.config.min_hold_sec
        )

        while trade.status == TradeStatus.FILLED:
            await asyncio.sleep(self.config.exit_check_interval_sec)

            now = datetime.now(timezone.utc)

            # Get current position from Alpaca
            position = self.rest.get_position(trade.ticker)
            if position is None:
                # Position was closed externally
                logger.info("Position closed externally")
                trade.status = TradeStatus.CLOSED
                trade.exit_reason = ExitReason.MANUAL
                break

            current_price = position["current_price"]

            # Don't exit before min hold time (avoid whipsaw)
            if now < min_exit_time:
                self._update_trailing_stop(trade, current_price)
                continue

            # Check kill switch
            if os.path.exists(self.config.kill_switch_file):
                logger.warning("KILL SWITCH ACTIVE — closing position")
                self._close_trade(trade, ExitReason.KILL_SWITCH, current_price)
                break

            # Check stop loss
            if trade.direction == SignalDirection.LONG:
                if current_price <= trade.stop_loss_price:
                    self._close_trade(trade, ExitReason.STOP_LOSS, current_price)
                    break
                if current_price <= trade.trailing_stop_price:
                    self._close_trade(trade, ExitReason.TRAILING_STOP, current_price)
                    break
                if current_price >= trade.take_profit_price:
                    self._close_trade(trade, ExitReason.TAKE_PROFIT, current_price)
                    break
            else:  # SHORT
                if current_price >= trade.stop_loss_price:
                    self._close_trade(trade, ExitReason.STOP_LOSS, current_price)
                    break
                if current_price >= trade.trailing_stop_price:
                    self._close_trade(trade, ExitReason.TRAILING_STOP, current_price)
                    break
                if current_price <= trade.take_profit_price:
                    self._close_trade(trade, ExitReason.TAKE_PROFIT, current_price)
                    break

            # Check max hold time
            if trade.max_exit_time and now >= trade.max_exit_time:
                self._close_trade(trade, ExitReason.MAX_HOLD_TIME, current_price)
                break

            # Update trailing stop
            self._update_trailing_stop(trade, current_price)

        # Remove from active trades
        self._active_trades.pop(trade.trade_id, None)

    def _close_trade(self, trade: Trade, reason: ExitReason,
                      current_price: float):
        """Close a trade position."""
        logger.info(
            f"Closing trade {trade.trade_id[:8]}: {reason.value} "
            f"@ ${current_price:.2f}"
        )

        try:
            result = self.rest.close_position(trade.ticker)
            trade.exit_order_id = result["order_id"]
        except Exception as e:
            logger.error(f"Failed to close position: {e}")

        trade.exit_price = current_price
        trade.exit_timestamp = datetime.now(timezone.utc)
        trade.exit_reason = reason
        trade.status = TradeStatus.CLOSED

        # Calculate P&L
        if trade.direction == SignalDirection.LONG:
            trade.realized_pnl = (trade.exit_price - trade.entry_price) * trade.shares
        else:
            trade.realized_pnl = (trade.entry_price - trade.exit_price) * trade.shares

        trade.realized_pnl_pct = (
            trade.realized_pnl / (trade.entry_price * trade.shares) * 100
            if trade.entry_price > 0 else 0
        )

        logger.info(
            f"{'✅' if trade.realized_pnl >= 0 else '❌'} "
            f"Trade closed: {reason.value} | "
            f"P&L: ${trade.realized_pnl:+.2f} ({trade.realized_pnl_pct:+.2f}%)"
        )

        # Notify
        self.notifier.send_exit_alert(trade)

        # Update risk manager
        from .risk_manager import RiskManager
        risk = RiskManager(self.config)
        risk.record_trade_result(trade)

    def get_active_trades(self) -> list[Trade]:
        return list(self._active_trades.values())
```

---

### 4.7 `risk_manager.py` — Risk Controls

```python
"""
RiskManager — enforces all risk limits.

Controls:
- Kill switch (file-based, instant halt)
- Daily loss limit ($500 default)
- Max trades per day (5)
- Max concurrent positions (2)
- Cooldown after loss (30 min)
- Market hours check
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Tuple

from .config import BotDetectorConfig
from .models import BotSignal, Trade, DailyRiskState

logger = logging.getLogger("botdetector.risk")

# US market hours in UTC (ET + 4/5 depending on DST)
MARKET_OPEN_UTC_HOUR = 13   # 9:30 AM ET ≈ 13:30 UTC (approx)
MARKET_CLOSE_UTC_HOUR = 20  # 4:00 PM ET = 20:00 UTC


class RiskManager:

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()
        self._state = self._load_or_create_state()

    def _load_or_create_state(self) -> DailyRiskState:
        """Load today's risk state or create fresh."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state_file = os.path.join(
            os.path.dirname(self.config.trade_log_file),
            "daily_risk_state.json"
        )

        if os.path.exists(state_file):
            with open(state_file) as f:
                data = json.load(f)
                if data.get("date") == today:
                    state = DailyRiskState(**data)
                    return state

        return DailyRiskState(date=today)

    def _save_state(self):
        state_file = os.path.join(
            os.path.dirname(self.config.trade_log_file),
            "daily_risk_state.json"
        )
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(self._state.__dict__, f, indent=2, default=str)

    def check_can_trade(self, signal: BotSignal) -> Tuple[bool, str]:
        """
        Run all risk checks. Returns (can_trade, reason).

        Checks in order (fail-fast):
        1. Kill switch
        2. Market hours
        3. Daily loss limit
        4. Max daily trades
        5. Max concurrent positions
        6. Post-loss cooldown
        """
        # 1. Kill switch
        kill_path = os.path.join(
            os.path.dirname(__file__), "..", self.config.kill_switch_file
        )
        if os.path.exists(kill_path):
            return False, "KILL SWITCH ACTIVE"

        if self._state.halted:
            return False, f"HALTED: {self._state.halt_reason}"

        # 2. Market hours
        now = datetime.now(timezone.utc)
        if now.hour < MARKET_OPEN_UTC_HOUR or now.hour >= MARKET_CLOSE_UTC_HOUR:
            return False, f"Outside market hours (UTC hour: {now.hour})"

        # 3. Daily loss limit
        if self._state.realized_pnl_today <= -self.config.max_daily_loss_dollars:
            self._state.halted = True
            self._state.halt_reason = (
                f"Daily loss limit hit: ${self._state.realized_pnl_today:.2f}"
            )
            self._save_state()
            return False, self._state.halt_reason

        # 4. Max daily trades
        if self._state.trades_today >= self.config.max_daily_trades:
            return False, (
                f"Max daily trades reached: {self._state.trades_today}/"
                f"{self.config.max_daily_trades}"
            )

        # 5. Max concurrent positions
        if len(self._state.open_positions) >= self.config.max_concurrent_positions:
            return False, (
                f"Max concurrent positions: "
                f"{len(self._state.open_positions)}/"
                f"{self.config.max_concurrent_positions}"
            )

        # 6. Post-loss cooldown
        if self._state.last_loss_timestamp:
            cooldown_end = self._state.last_loss_timestamp + timedelta(
                seconds=self.config.cooldown_after_loss_sec
            )
            if isinstance(cooldown_end, str):
                cooldown_end = datetime.fromisoformat(cooldown_end)
            if now < cooldown_end:
                remaining = (cooldown_end - now).total_seconds()
                return False, f"Post-loss cooldown: {remaining:.0f}s remaining"

        return True, "OK"

    def record_trade_result(self, trade: Trade):
        """Update daily risk state after a trade closes."""
        self._state.trades_today += 1
        self._state.realized_pnl_today += trade.realized_pnl

        # Remove from open positions
        if trade.trade_id in self._state.open_positions:
            self._state.open_positions.remove(trade.trade_id)

        # Record loss timestamp for cooldown
        if trade.realized_pnl < 0:
            self._state.last_loss_timestamp = trade.exit_timestamp

        self._save_state()

        logger.info(
            f"Risk state updated: "
            f"trades={self._state.trades_today}, "
            f"daily P&L=${self._state.realized_pnl_today:+.2f}"
        )

    def record_trade_open(self, trade: Trade):
        """Track a newly opened position."""
        self._state.open_positions.append(trade.trade_id)
        self._save_state()

    def activate_kill_switch(self, reason: str = "Manual kill"):
        """Create the kill switch file."""
        kill_path = os.path.join(
            os.path.dirname(__file__), "..", self.config.kill_switch_file
        )
        os.makedirs(os.path.dirname(kill_path), exist_ok=True)
        with open(kill_path, "w") as f:
            f.write(f"{reason}\n{datetime.now(timezone.utc).isoformat()}\n")
        logger.critical(f"🛑 KILL SWITCH ACTIVATED: {reason}")

    def deactivate_kill_switch(self):
        """Remove the kill switch file."""
        kill_path = os.path.join(
            os.path.dirname(__file__), "..", self.config.kill_switch_file
        )
        if os.path.exists(kill_path):
            os.remove(kill_path)
            logger.info("Kill switch deactivated")
```

---

### 4.8 `notifier.py` — Telegram Alerts

```python
"""Telegram notifications for bot detector events."""

import logging
import subprocess
from .config import BotDetectorConfig
from .models import BotSignal, Trade

logger = logging.getLogger("botdetector.notifier")


class Notifier:

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()

    def _send(self, text: str):
        """Send via openclaw CLI (same pattern as signal_check.py)."""
        try:
            result = subprocess.run(
                [
                    "openclaw", "message", "send",
                    "--to", self.config.telegram_user_id,
                    "--channel", "telegram",
                    "--message", text,
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                logger.error(f"Telegram send failed: {result.stderr}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

    def send_trade_alert(self, signal: BotSignal, trade: Trade):
        direction_emoji = "🟢" if signal.direction.value == "LONG" else "🔴"
        paper = "📝 PAPER" if trade.paper_mode else "💰 LIVE"

        text = (
            f"🤖 *Bot Detector — Trade Executed*\n\n"
            f"{paper}\n"
            f"📰 _{signal.post_text[:150]}_\n\n"
            f"🏷 Category: `{', '.join(signal.post_categories)}`\n"
            f"{direction_emoji} {signal.direction.value} {trade.shares}x "
            f"*{signal.ticker}* @ ${trade.entry_price:.2f}\n"
            f"💵 Position: ${trade.position_value:.2f}\n\n"
            f"*Bot Signature:*\n"
            f"  📊 Volume: {signal.volume_spike_ratio:.1f}x spike\n"
            f"  ⚡ Velocity: {signal.price_velocity_pct:+.2f}%\n"
            f"  📐 Spread: +{signal.spread_widening_pct:.0f}%\n"
            f"  ⏱ Detected {signal.seconds_after_post:.0f}s after post\n\n"
            f"🛑 SL: ${trade.stop_loss_price:.2f} | "
            f"🎯 TP: ${trade.take_profit_price:.2f}\n"
            f"⏰ Max hold: {self.config.max_hold_sec // 60} min"
        )
        self._send(text)

    def send_exit_alert(self, trade: Trade):
        pnl_emoji = "✅" if trade.realized_pnl >= 0 else "❌"

        text = (
            f"🤖 *Bot Detector — Position Closed*\n\n"
            f"{pnl_emoji} {trade.exit_reason.value}\n"
            f"*{trade.ticker}* {trade.direction.value}: "
            f"${trade.entry_price:.2f} → ${trade.exit_price:.2f}\n"
            f"P&L: *${trade.realized_pnl:+.2f}* "
            f"({trade.realized_pnl_pct:+.2f}%)\n"
        )
        self._send(text)

    def send_blocked_alert(self, signal: BotSignal, reason: str):
        text = (
            f"🤖 *Bot Detector — Trade BLOCKED*\n\n"
            f"⚠️ {reason}\n"
            f"Signal: {signal.direction.value} {signal.ticker}\n"
            f"Post: _{signal.post_text[:120]}_"
        )
        self._send(text)
```

---

### 4.9 `backtest.py` — Historical Validation

```python
"""
Backtest the bot detector using historical minute-bar data.

Approach:
1. Load historical posts with timestamps from data/posts_categorized.json
2. For each post, fetch 1-min bar data around the post time (±30 min)
3. Simulate the bot signature detection using bar-level approximation:
   - Volume spike: compare 1-min volume to trailing 15-min avg
   - Price velocity: % change from bar at post time to subsequent bars
   - Spread: approximate from high-low range (no tick-level quotes in bars)
4. If signature detected, simulate the trade with actual price data
5. Output: hit rate, average P&L, Sharpe ratio, drawdown

Limitations:
- Minute bars can't perfectly replicate tick-level detection
- Spread is approximated from high-low range
- Fill simulation assumes market orders fill at close of signal bar
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
    detection_bar_idx: int           # Which minute-bar triggered
    volume_spike_ratio: float
    price_velocity_pct: float
    spread_proxy_widening_pct: float

    # Trade simulation
    direction: str                   # LONG or SHORT
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_bars: int                   # Number of minute bars held
    pnl_pct: float
    pnl_dollars: float               # Based on $2500 position


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
    avg_detection_bar: float         # How quickly signature fires (minutes)
    best_trade_pnl_pct: float
    worst_trade_pnl_pct: float


class Backtester:

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()
        self.results: list[BacktestResult] = []

    def run(self, posts_file: str = None, output_dir: str = None) -> BacktestSummary:
        """
        Run full backtest.

        Args:
            posts_file: Path to posts_categorized.json
            output_dir: Directory for output files
        """
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
            start = (post_time - timedelta(minutes=30)).isoformat()
            end = (post_time + timedelta(minutes=90)).isoformat()

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
        """
        Simulate bot detection on minute-bar data.

        Approximations:
        - Volume spike: bar volume vs trailing 15-bar avg
        - Price velocity: (close - close_at_post) / close_at_post
        - Spread proxy: (high - low) / ((high + low) / 2) vs trailing avg
        """
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
        detection_window_bars = self.config.detection_window_sec // 60  # ~2 bars

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
                # Signature detected — simulate trade
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
        """
        Simulate trade exit using minute bars.

        Returns: (exit_price, exit_reason, hold_bars)
        """
        stop_pct = self.config.stop_loss_pct / 100
        tp_pct = self.config.take_profit_pct / 100
        max_bars = self.config.max_hold_sec // 60

        for i in range(entry_idx + 1,
                       min(entry_idx + max_bars + 1, len(bars))):
            bar = bars[i]
            hold_bars = i - entry_idx

            if direction == "LONG":
                # Check stop loss (using bar low)
                if bar["low"] <= entry_price * (1 - stop_pct):
                    return entry_price * (1 - stop_pct), "STOP_LOSS", hold_bars
                # Check take profit (using bar high)
                if bar["high"] >= entry_price * (1 + tp_pct):
                    return entry_price * (1 + tp_pct), "TAKE_PROFIT", hold_bars
            else:
                if bar["high"] >= entry_price * (1 + stop_pct):
                    return entry_price * (1 + stop_pct), "STOP_LOSS", hold_bars
                if bar["low"] <= entry_price * (1 - tp_pct):
                    return entry_price * (1 - tp_pct), "TAKE_PROFIT", hold_bars

        # Max hold time — exit at last bar close
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
```

---

### 4.10 `logger.py` — Structured Logging

```python
"""Structured JSON logging for signals and trades."""

import json
import logging
import os
from datetime import datetime, timezone

from .config import BotDetectorConfig


def setup_logging(level: str = "INFO"):
    """Configure logging for the package."""
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log_signal(signal, config: BotDetectorConfig = None):
    """Append a BotSignal to the signal log file."""
    config = config or BotDetectorConfig()
    log_file = os.path.join(
        os.path.dirname(__file__), "..", config.signal_log_file
    )
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    entry = {
        "signal_id": signal.signal_id,
        "ticker": signal.ticker,
        "direction": signal.direction.value,
        "post_id": signal.post_id,
        "post_text": signal.post_text[:200],
        "categories": signal.post_categories,
        "detection_timestamp": signal.detection_timestamp.isoformat(),
        "seconds_after_post": signal.seconds_after_post,
        "volume_spike_ratio": signal.volume_spike_ratio,
        "price_velocity_pct": signal.price_velocity_pct,
        "spread_widening_pct": signal.spread_widening_pct,
        "entry_price": signal.entry_price,
        "confidence": signal.confidence,
    }

    # Append to JSONL file
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_trade(trade, config: BotDetectorConfig = None):
    """Append a Trade result to the trade log file."""
    config = config or BotDetectorConfig()
    log_file = os.path.join(
        os.path.dirname(__file__), "..", config.trade_log_file
    )
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    entry = {
        "trade_id": trade.trade_id,
        "signal_id": trade.signal_id,
        "ticker": trade.ticker,
        "direction": trade.direction.value,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "shares": trade.shares,
        "realized_pnl": trade.realized_pnl,
        "realized_pnl_pct": trade.realized_pnl_pct,
        "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
        "entry_timestamp": str(trade.entry_timestamp),
        "exit_timestamp": str(trade.exit_timestamp),
        "paper_mode": trade.paper_mode,
    }

    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

---

### 4.11 `cli.py` — Entry Point

```python
"""
CLI entry point for the bot detector.

Usage:
    python -m botdetector daemon          # Long-running WebSocket listener
    python -m botdetector arm --post-id X --text "..." --categories TARIFFS
    python -m botdetector backtest        # Run historical backtest
    python -m botdetector status          # Show current risk state
    python -m botdetector kill            # Activate kill switch
    python -m botdetector unkill          # Deactivate kill switch
"""

import argparse
import asyncio
import sys

from .config import BotDetectorConfig
from .bot_detector import BotDetector
from .backtest import Backtester
from .risk_manager import RiskManager
from .logger import setup_logging


def main():
    parser = argparse.ArgumentParser(description="TrumpQuant Bot Detector")
    sub = parser.add_subparsers(dest="command")

    # daemon
    sub.add_parser("daemon", help="Run as long-running WebSocket listener")

    # arm (one-shot)
    arm_p = sub.add_parser("arm", help="One-shot detection window")
    arm_p.add_argument("--post-id", required=True)
    arm_p.add_argument("--text", required=True)
    arm_p.add_argument("--categories", nargs="+", required=True)
    arm_p.add_argument("--tickers", nargs="+", default=None)

    # backtest
    bt_p = sub.add_parser("backtest", help="Run historical backtest")
    bt_p.add_argument("--posts-file", default=None)
    bt_p.add_argument("--output-dir", default=None)

    # status
    sub.add_parser("status", help="Show risk state and active trades")

    # kill / unkill
    kill_p = sub.add_parser("kill", help="Activate kill switch")
    kill_p.add_argument("--reason", default="Manual kill via CLI")
    sub.add_parser("unkill", help="Deactivate kill switch")

    args = parser.parse_args()
    setup_logging()
    config = BotDetectorConfig()

    if args.command == "daemon":
        detector = BotDetector(config)
        asyncio.run(detector.run_daemon())

    elif args.command == "arm":
        detector = BotDetector(config)
        asyncio.run(detector.run_oneshot(
            post_id=args.post_id,
            post_text=args.text,
            categories=args.categories,
            tickers=args.tickers,
        ))

    elif args.command == "backtest":
        bt = Backtester(config)
        bt.run(posts_file=args.posts_file, output_dir=args.output_dir)

    elif args.command == "status":
        rm = RiskManager(config)
        state = rm._state
        print(f"Date: {state.date}")
        print(f"Trades today: {state.trades_today}")
        print(f"Daily P&L: ${state.realized_pnl_today:+.2f}")
        print(f"Open positions: {len(state.open_positions)}")
        print(f"Halted: {state.halted} {state.halt_reason}")

    elif args.command == "kill":
        rm = RiskManager(config)
        rm.activate_kill_switch(args.reason)
        print("🛑 Kill switch ACTIVATED")

    elif args.command == "unkill":
        rm = RiskManager(config)
        rm.deactivate_kill_switch()
        print("✅ Kill switch deactivated")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

---

## 5. Integration with Existing `signal_check.py`

The integration is minimal — add a few lines to `signal_check.py`'s `main()` function:

### Changes to `signal_check.py`

```python
# Add to imports at top:
import subprocess
import sys

# Add after the send_telegram(alert) call inside the main loop:

        # === BOT DETECTOR INTEGRATION ===
        # When a signal fires, also trigger bot detector in one-shot mode
        # It runs for ~130 seconds watching for bot signatures
        if best_signal and best_signal["confidence"] in ("HIGH", "MEDIUM"):
            try:
                cat_list = " ".join(categories)
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
                print(f"  → Bot detector armed for {post['id']}")
            except Exception as e:
                print(f"  → Bot detector launch failed: {e}")
```

### Alternative: Daemon Mode Integration

If running the daemon, `signal_check.py` communicates via a trigger file:

```python
# signal_check.py writes a trigger file:
trigger = {
    "post_id": post["id"],
    "text": post["text"],
    "categories": categories,
    "timestamp": datetime.now(timezone.utc).isoformat(),
}
trigger_path = os.path.join(DATA_DIR, "bot_trigger.json")
with open(trigger_path, "w") as f:
    json.dump(trigger, f)

# The daemon watches for this file (via asyncio file watcher or polling)
# and calls self.arm() when it appears, then deletes the trigger file.
```

### Cron Setup

The existing cron job stays the same. Bot detector is spawned as a subprocess when needed:

```cron
# Existing: check for Trump posts every 15 min
*/15 * * * * cd /Users/ronnie/hamilton/trumpquant && .venv/bin/python signal_check.py >> data/cron.log 2>&1
```

No new cron entry needed — the bot detector is triggered on-demand by `signal_check.py`.

---

## 6. Updated `requirements.txt`

```
yfinance
pandas
numpy
requests
beautifulsoup4
python-dateutil
scipy
alpaca-py>=0.30.0
websockets>=12.0
```

---

## 7. Risk Controls Summary

| Control | Default | Configurable |
|---------|---------|:---:|
| Paper mode | `True` (always default) | ✓ |
| Kill switch | File-based (`data/kill_switch.flag`) | ✓ |
| Max position size | 5% of equity OR $2,500 (whichever is less) | ✓ |
| Stop loss | 0.5% | ✓ |
| Take profit | 1.5% | ✓ |
| Trailing stop | 0.3% (activates after +0.5%) | ✓ |
| Max hold time | 60 minutes | ✓ |
| Min hold time | 60 seconds (anti-whipsaw) | ✓ |
| Daily loss limit | $500 → halt all trading | ✓ |
| Max trades/day | 5 | ✓ |
| Max concurrent positions | 2 | ✓ |
| Post-loss cooldown | 30 minutes | ✓ |
| Market hours only | Yes (skip pre/post market) | ✓ |

### Kill Switch

Three ways to activate:
1. **CLI:** `python -m botdetector kill --reason "emergency"`
2. **Telegram:** Ron could type `/kill` (future integration)
3. **Manual:** `touch data/kill_switch.flag`

Kill switch:
- Prevents ALL new trades immediately
- Forces close of ALL open positions
- Requires manual deactivation: `python -m botdetector unkill` or `rm data/kill_switch.flag`

---

## 8. Testing Plan

### Phase 1: Unit Tests (before any API calls)

| Test File | What It Tests | Key Cases |
|-----------|--------------|-----------|
| `test_market_state.py` | Rolling window math | Volume spike calculation with known data; spread widening edge cases; empty buffer handling; deque overflow behavior |
| `test_bot_detector.py` | Signature detection logic | All 3 criteria met → signal fires; 2 of 3 → no signal; criteria met after window expires → no signal; multiple arm/disarm cycles; re-arm while already armed |
| `test_trade_executor.py` | Position sizing & exit logic | Position size respects both % and $ cap; stop loss calculation long vs short; trailing stop only ratchets in favorable direction; max hold time forces exit |
| `test_risk_manager.py` | Risk control enforcement | Kill switch blocks trades; daily loss limit triggers halt; cooldown timer works; max concurrent positions enforced; state persists across restarts |

### Phase 2: Integration Tests (mock WebSocket)

| Test | Description |
|------|-------------|
| `test_integration.py::test_full_flow` | Mock WebSocket sends synthetic trade/quote messages simulating a bot signature. Verify: arm → detect → signal → mock execute → exit |
| `test_integration.py::test_no_signature` | Feed normal (non-spikey) data during armed window. Verify: disarms after timeout with no signal |
| `test_integration.py::test_risk_block` | Trigger signal but with kill switch active. Verify: signal logged but trade blocked |

### Phase 3: Paper Trading Validation (before real money)

**Minimum criteria before going live:**

1. **Run backtest** on all historical posts — minimum 55% win rate and positive Sharpe
2. **Paper trade for 2+ weeks** — minimum 10 triggered signals observed
3. **Verify Telegram alerts** work correctly for: armed, signal detected, trade executed, trade closed, trade blocked
4. **Verify kill switch** works (activate mid-trade, confirm positions close)
5. **Verify daily loss limit** (paper-lose $500, confirm system halts)
6. **Verify market hours** gate (arm during off-hours, confirm no trade)
7. **Manual review** of every paper trade: was the bot signature real? Was the direction correct?

### Phase 4: Live Trading (graduated)

1. Start with `max_position_dollars = 500` (1/5 of default)
2. Run for 1 week
3. If positive, increase to `max_position_dollars = 1000`
4. Run for 1 week
5. If still positive, increase to full `max_position_dollars = 2500`
6. Always keep `max_daily_loss_dollars = 500`

---

## 9. Implementation Order for Coding Agent

Build in this order (each step should be independently testable):

1. **`models.py`** — Pure dataclasses, no dependencies
2. **`config.py`** — Configuration, no dependencies
3. **`market_state.py`** + **`test_market_state.py`** — Core math, test with synthetic data
4. **`alpaca_client.py`** — API wrapper (can test with paper account)
5. **`risk_manager.py`** + **`test_risk_manager.py`** — Risk controls
6. **`notifier.py`** — Telegram alerts
7. **`logger.py`** — Structured logging
8. **`trade_executor.py`** + **`test_trade_executor.py`** — Execution logic
9. **`bot_detector.py`** + **`test_bot_detector.py`** — Core engine
10. **`cli.py`** — Entry point
11. **`backtest.py`** — Historical validation
12. **`test_integration.py`** — Full flow test
13. **Integration into `signal_check.py`** — Wire it up
14. **Update `requirements.txt`** and `README.md`

---

## 10. Environment Variables

```bash
# Required
export ALPACA_API_KEY="your-paper-api-key"
export ALPACA_SECRET_KEY="your-paper-secret-key"

# Optional (defaults shown)
export ALPACA_BASE_URL="https://paper-api.alpaca.markets"
export BOT_DETECTOR_PAPER_MODE="true"
```

---

## 11. Monitoring & Observability

All state is in flat files for simplicity:

| File | Format | Purpose |
|------|--------|---------|
| `data/bot_signals.json` | JSONL | Every detected bot signature |
| `data/bot_trades.json` | JSONL | Every trade entry + exit |
| `data/daily_risk_state.json` | JSON | Current day's risk metrics |
| `data/kill_switch.flag` | Text | Presence = halt all trading |
| `data/botdetector_stdout.log` | Text | Stdout from one-shot runs |
| `data/botdetector_stderr.log` | Text | Stderr/errors from one-shot runs |
| `data/backtest_results/` | JSON | Backtest output |

Ron can check status anytime:
```bash
python -m botdetector status
cat data/bot_trades.json | python -m json.tool
cat data/daily_risk_state.json
```

---

*This document is the complete implementation blueprint. Hand it to Claude Code and build in the order specified in Section 9.*
