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
    watchlist: tuple[str, ...] = (
        "SPY", "QQQ", "DJT", "COIN", "TSLA", "NVDA", "GME", "META", "GLD",
    )
    category_tickers: dict[str, str] = field(default_factory=lambda: {
        "TARIFFS": "SPY",
        "TRADE_DEAL": "SPY",
        "CRYPTO": "COIN",
        "FED_ATTACK": "SPY",
        "MARKET_PUMP": "SPY",
        "SPECIFIC_TICKER": "DJT",
    })

    # === Bot Signature Detection Thresholds ===
    detection_window_sec: int = 120
    volume_spike_multiplier: float = 3.0
    volume_rolling_window_sec: int = 900
    price_velocity_pct: float = 0.3
    price_velocity_window_sec: int = 60
    spread_widening_pct: float = 50.0
    spread_baseline_window_sec: int = 900
    min_criteria_met: int = 3

    # === Trade Execution ===
    max_position_pct: float = 0.05
    max_position_dollars: float = 2500.0
    stop_loss_pct: float = 0.5
    take_profit_pct: float = 1.5
    trailing_stop_pct: float = 0.3
    trailing_stop_activation_pct: float = 0.5
    min_hold_sec: int = 60
    max_hold_sec: int = 3600
    default_hold_sec: int = 1800
    exit_check_interval_sec: int = 5

    # === Risk Controls ===
    max_daily_loss_dollars: float = 500.0
    max_daily_trades: int = 5
    max_concurrent_positions: int = 2
    cooldown_after_loss_sec: int = 1800
    kill_switch_file: str = "data/kill_switch.flag"

    # === Paths ===
    trade_log_file: str = "data/bot_trades.json"
    signal_log_file: str = "data/bot_signals.json"
    backtest_dir: str = "data/backtest_results"

    # === Telegram ===
    telegram_user_id: str = "8387647137"
