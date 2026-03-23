"""Tests for RiskManager risk control enforcement."""

import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from botdetector.config import BotDetectorConfig
from botdetector.risk_manager import RiskManager
from botdetector.models import (
    BotSignal, Trade, SignalDirection, TradeStatus,
    ExitReason, MarketSnapshot, DailyRiskState,
)


@pytest.fixture
def config(tmp_path):
    return BotDetectorConfig(
        kill_switch_file=str(tmp_path / "kill_switch.flag"),
        trade_log_file=str(tmp_path / "bot_trades.json"),
        signal_log_file=str(tmp_path / "bot_signals.json"),
        max_daily_loss_dollars=500.0,
        max_daily_trades=5,
        max_concurrent_positions=2,
        cooldown_after_loss_sec=1800,
    )


@pytest.fixture
def signal():
    now = datetime.now(timezone.utc)
    snapshot = MarketSnapshot(
        ticker="SPY", timestamp=now, last_price=540.0,
        bid=539.90, ask=540.10, spread=0.20, spread_pct=0.037,
        volume_1s=1000, volume_rolling=100.0, volume_spike_ratio=4.0,
        price_at_arm=539.0, price_velocity_pct=0.185,
        spread_baseline=0.02, spread_widening_pct=85.0,
    )
    return BotSignal(
        signal_id="test-signal-1",
        ticker="SPY",
        direction=SignalDirection.LONG,
        post_id="post-1",
        post_text="Trump announces tariffs",
        post_categories=["TARIFFS"],
        post_timestamp=now - timedelta(seconds=60),
        detection_timestamp=now,
        seconds_after_post=60.0,
        volume_spike_ratio=4.0,
        price_velocity_pct=0.45,
        spread_widening_pct=85.0,
        entry_price=540.0,
        snapshot=snapshot,
    )


def make_trade(pnl: float = 0.0, trade_id: str = "t1") -> Trade:
    now = datetime.now(timezone.utc)
    return Trade(
        trade_id=trade_id,
        signal_id="s1",
        ticker="SPY",
        direction=SignalDirection.LONG,
        status=TradeStatus.CLOSED,
        entry_price=540.0,
        exit_price=540.0 + (pnl / 4),
        shares=4,
        realized_pnl=pnl,
        exit_timestamp=now,
        exit_reason=ExitReason.TAKE_PROFIT if pnl >= 0 else ExitReason.STOP_LOSS,
    )


class TestRiskManagerChecks:

    @patch("botdetector.risk_manager.datetime")
    def test_passes_all_checks(self, mock_dt, config, signal):
        # Mock to be during market hours
        mock_now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = RiskManager(config)
        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is True
        assert reason == "OK"

    def test_kill_switch_blocks(self, config, signal):
        # Create kill switch file
        os.makedirs(os.path.dirname(config.kill_switch_file), exist_ok=True)
        with open(config.kill_switch_file, "w") as f:
            f.write("test kill\n")

        rm = RiskManager(config)
        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is False
        assert "KILL SWITCH" in reason

        # Cleanup
        os.remove(config.kill_switch_file)

    @patch("botdetector.risk_manager.datetime")
    def test_outside_market_hours_blocks(self, mock_dt, config, signal):
        # 5 AM UTC = before market open
        mock_now = datetime(2026, 3, 23, 5, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = RiskManager(config)
        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is False
        assert "market hours" in reason.lower()

    @patch("botdetector.risk_manager.datetime")
    def test_daily_loss_limit_blocks(self, mock_dt, config, signal):
        mock_now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = RiskManager(config)
        rm._state.realized_pnl_today = -501.0

        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is False
        assert "loss limit" in reason.lower() or "HALTED" in reason

    @patch("botdetector.risk_manager.datetime")
    def test_max_daily_trades_blocks(self, mock_dt, config, signal):
        mock_now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = RiskManager(config)
        rm._state.trades_today = 5

        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is False
        assert "daily trades" in reason.lower()

    @patch("botdetector.risk_manager.datetime")
    def test_max_concurrent_positions_blocks(self, mock_dt, config, signal):
        mock_now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = RiskManager(config)
        rm._state.open_positions = ["trade1", "trade2"]

        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is False
        assert "concurrent" in reason.lower()

    @patch("botdetector.risk_manager.datetime")
    def test_cooldown_blocks(self, mock_dt, config, signal):
        mock_now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        rm = RiskManager(config)
        # Loss 10 minutes ago (within 30 min cooldown)
        rm._state.last_loss_timestamp = mock_now - timedelta(minutes=10)

        can_trade, reason = rm.check_can_trade(signal)
        assert can_trade is False
        assert "cooldown" in reason.lower()


class TestRiskManagerRecording:

    def test_record_trade_result(self, config):
        rm = RiskManager(config)
        trade = make_trade(pnl=50.0, trade_id="t1")
        rm._state.open_positions = ["t1"]

        rm.record_trade_result(trade)

        assert rm._state.trades_today == 1
        assert rm._state.realized_pnl_today == 50.0
        assert "t1" not in rm._state.open_positions

    def test_record_loss_sets_cooldown(self, config):
        rm = RiskManager(config)
        trade = make_trade(pnl=-100.0, trade_id="t1")

        rm.record_trade_result(trade)

        assert rm._state.last_loss_timestamp is not None

    def test_record_trade_open(self, config):
        rm = RiskManager(config)
        trade = make_trade(trade_id="t1")

        rm.record_trade_open(trade)

        assert "t1" in rm._state.open_positions


class TestKillSwitch:

    def test_activate_kill_switch(self, config):
        rm = RiskManager(config)
        rm.activate_kill_switch("test emergency")

        assert os.path.exists(config.kill_switch_file)
        with open(config.kill_switch_file) as f:
            content = f.read()
        assert "test emergency" in content

    def test_deactivate_kill_switch(self, config):
        rm = RiskManager(config)
        rm.activate_kill_switch("test")
        assert os.path.exists(config.kill_switch_file)

        rm.deactivate_kill_switch()
        assert not os.path.exists(config.kill_switch_file)
