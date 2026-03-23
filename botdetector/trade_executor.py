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
        self._active_trades: dict[str, Trade] = {}  # trade_id -> Trade

    def execute(self, signal: BotSignal) -> Optional[Trade]:
        """
        Execute a trade based on a confirmed bot signal.

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
                logger.warning("Position size = 0 shares, skipping trade")
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
                f"ORDER FILLED: {side} {shares}x {signal.ticker} "
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
                logger.warning("KILL SWITCH ACTIVE -- closing position")
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
