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
