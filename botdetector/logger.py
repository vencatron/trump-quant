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
