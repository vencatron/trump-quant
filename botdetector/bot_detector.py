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

    # --- Public API ---

    def arm(self, post_id: str, post_text: str, categories: list[str],
            tickers: list[str] = None, timestamp: datetime = None):
        """
        Start a detection window.

        Called by signal_check.py when a new Trump post matches
        a signal category.
        """
        if self._armed:
            logger.warning("Already armed -- ignoring new arm request")
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
            logger.info("Detection window expired -- no bot signature confirmed")
        finally:
            self.disarm()
            await self.ws_client.stop()
            ws_task.cancel()

    # --- Internal Handlers ---

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

        # -- Criterion 1: Volume Spike --
        if not self._volume_confirmed:
            ratio = state.get_volume_spike_ratio(
                now, cfg.volume_rolling_window_sec, recent_sec=5
            )
            if ratio >= cfg.volume_spike_multiplier:
                self._volume_confirmed = True
                self._volume_confirm_time = now
                logger.info(
                    f"[{ticker}] Volume spike confirmed: {ratio:.1f}x "
                    f"(threshold: {cfg.volume_spike_multiplier}x)"
                )

        # -- Criterion 2: Price Velocity --
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
                            f"[{ticker}] Price velocity confirmed: "
                            f"{state.get_price_velocity_pct():+.3f}% "
                            f"in {secs_since_arm:.0f}s "
                            f"(threshold: +/-{cfg.price_velocity_pct}%)"
                        )

        # -- Criterion 3: Spread Widening --
        if not self._spread_confirmed:
            widening = state.get_spread_widening_pct(
                now, cfg.spread_baseline_window_sec
            )
            if widening >= cfg.spread_widening_pct:
                self._spread_confirmed = True
                self._spread_confirm_time = now
                logger.info(
                    f"[{ticker}] Spread widening confirmed: {widening:.1f}% "
                    f"(threshold: {cfg.spread_widening_pct}%)"
                )

        # -- Check if all 3 confirmed --
        criteria_met = sum([
            self._volume_confirmed,
            self._velocity_confirmed,
            self._spread_confirmed,
        ])

        if criteria_met >= cfg.min_criteria_met:
            self._fire_signal(ticker, now)

    def _fire_signal(self, ticker: str, now: datetime):
        """All criteria met -- create signal and hand off to executor."""
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
            f"BOT SIGNATURE CONFIRMED: {ticker} {direction.value} "
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
        STRONG if all 3 criteria are >= 2x the threshold.
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
                        logger.info("Detection window expired -- no signature")
                    self.disarm()
            await asyncio.sleep(1)
