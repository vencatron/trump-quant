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
MARKET_OPEN_UTC_HOUR = 13   # 9:30 AM ET ~ 13:30 UTC (approx)
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
            try:
                with open(state_file) as f:
                    data = json.load(f)
                    if data.get("date") == today:
                        # Handle last_loss_timestamp deserialization
                        llt = data.get("last_loss_timestamp")
                        if llt and isinstance(llt, str):
                            try:
                                data["last_loss_timestamp"] = datetime.fromisoformat(llt)
                            except (ValueError, TypeError):
                                data["last_loss_timestamp"] = None
                        state = DailyRiskState(**data)
                        return state
            except (json.JSONDecodeError, TypeError):
                pass

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
            last_loss = self._state.last_loss_timestamp
            if isinstance(last_loss, str):
                last_loss = datetime.fromisoformat(last_loss)
            cooldown_end = last_loss + timedelta(
                seconds=self.config.cooldown_after_loss_sec
            )
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
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

    def deactivate_kill_switch(self):
        """Remove the kill switch file."""
        kill_path = os.path.join(
            os.path.dirname(__file__), "..", self.config.kill_switch_file
        )
        if os.path.exists(kill_path):
            os.remove(kill_path)
            logger.info("Kill switch deactivated")
