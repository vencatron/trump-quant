"""
Integration test — full flow with mock WebSocket.
No real API calls.
"""

import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from botdetector.config import BotDetectorConfig
from botdetector.bot_detector import BotDetector
from botdetector.models import SignalDirection, TradeStatus


@pytest.fixture
def config(tmp_path):
    return BotDetectorConfig(
        detection_window_sec=120,
        volume_spike_multiplier=3.0,
        volume_rolling_window_sec=900,
        price_velocity_pct=0.3,
        price_velocity_window_sec=60,
        spread_widening_pct=50.0,
        spread_baseline_window_sec=900,
        min_criteria_met=3,
        kill_switch_file=str(tmp_path / "kill_switch.flag"),
        trade_log_file=str(tmp_path / "bot_trades.json"),
        signal_log_file=str(tmp_path / "bot_signals.json"),
    )


def build_baseline_data(detector, ticker, now, seconds=900):
    """Populate 15 min of baseline trade/quote data."""
    for i in range(seconds):
        ts = now - timedelta(seconds=seconds - i)
        detector.market_state.on_trade(ticker, 540.0, 100, ts)
        if i % 10 == 0:
            detector.market_state.on_quote(ticker, 539.95, 540.05, ts)


class TestFullFlow:
    """Mock WebSocket sends synthetic trade/quote messages simulating a bot signature."""

    def test_arm_detect_signal(self, config):
        """arm -> detect -> signal fires"""
        now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)

        with patch("botdetector.bot_detector.AlpacaWSClient"), \
             patch("botdetector.bot_detector.TradeExecutor") as MockExecutor, \
             patch("botdetector.bot_detector.RiskManager") as MockRisk, \
             patch("botdetector.bot_detector.Notifier") as MockNotifier, \
             patch("botdetector.bot_detector.log_signal"):

            mock_risk = MagicMock()
            mock_risk.check_can_trade.return_value = (True, "OK")
            MockRisk.return_value = mock_risk

            mock_executor = MagicMock()
            mock_trade = MagicMock()
            mock_trade.paper_mode = True
            mock_executor.execute.return_value = mock_trade
            MockExecutor.return_value = mock_executor

            MockNotifier.return_value = MagicMock()

            detector = BotDetector(config)

            ticker = "SPY"
            build_baseline_data(detector, ticker, now)

            # Arm the detector
            detector.arm(
                post_id="post-123",
                post_text="Trump announces major tariff changes",
                categories=["TARIFFS"],
                tickers=[ticker],
                timestamp=now,
            )

            assert detector._armed is True

            # Simulate bot activity 30 seconds later
            spike_time = now + timedelta(seconds=30)

            # Volume spike
            for i in range(5):
                ts = spike_time - timedelta(seconds=5 - i)
                detector._handle_trade(ticker, 541.62, 500, ts)

            # Spread widening
            detector._handle_quote(ticker, 540.50, 542.50, spike_time)

            # Signal should have fired
            assert detector._signal_fired is True
            assert detector._volume_confirmed is True
            assert detector._velocity_confirmed is True
            assert detector._spread_confirmed is True

            # Executor should have been called
            mock_executor.execute.assert_called_once()


class TestNoSignature:
    """Feed normal data during armed window. Verify disarm with no signal."""

    def test_normal_data_no_signal(self, config):
        now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)

        with patch("botdetector.bot_detector.AlpacaWSClient"), \
             patch("botdetector.bot_detector.TradeExecutor") as MockExecutor, \
             patch("botdetector.bot_detector.RiskManager"), \
             patch("botdetector.bot_detector.Notifier"):

            detector = BotDetector(config)

            ticker = "SPY"
            build_baseline_data(detector, ticker, now)

            detector.arm(
                post_id="post-456",
                post_text="Trump makes a speech",
                categories=["TARIFFS"],
                tickers=[ticker],
                timestamp=now,
            )

            # Feed normal (non-spikey) data for 120 seconds
            for i in range(120):
                ts = now + timedelta(seconds=i)
                detector._handle_trade(ticker, 540.0 + (i % 3) * 0.01, 100, ts)
                if i % 5 == 0:
                    detector._handle_quote(ticker, 539.95, 540.05, ts)

            # No signal should have fired
            assert detector._signal_fired is False

            # Manually disarm (in real system, timeout_loop does this)
            detector.disarm()
            assert detector._armed is False

            # Executor should NOT have been called
            MockExecutor.return_value.execute.assert_not_called()


class TestRiskBlock:
    """Trigger signal but with risk manager blocking. Signal logged but trade blocked."""

    def test_risk_blocks_trade(self, config):
        now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)

        with patch("botdetector.bot_detector.AlpacaWSClient"), \
             patch("botdetector.bot_detector.TradeExecutor") as MockExecutor, \
             patch("botdetector.bot_detector.RiskManager") as MockRisk, \
             patch("botdetector.bot_detector.Notifier") as MockNotifier, \
             patch("botdetector.bot_detector.log_signal"):

            mock_risk = MagicMock()
            mock_risk.check_can_trade.return_value = (False, "KILL SWITCH ACTIVE")
            MockRisk.return_value = mock_risk

            mock_notifier = MagicMock()
            MockNotifier.return_value = mock_notifier

            detector = BotDetector(config)

            ticker = "SPY"
            build_baseline_data(detector, ticker, now)

            detector.arm(
                post_id="post-789",
                post_text="Trump tariff update",
                categories=["TARIFFS"],
                tickers=[ticker],
                timestamp=now,
            )

            # Trigger bot signature
            spike_time = now + timedelta(seconds=30)
            for i in range(5):
                ts = spike_time - timedelta(seconds=5 - i)
                detector._handle_trade(ticker, 541.62, 500, ts)
            detector._handle_quote(ticker, 540.50, 542.50, spike_time)

            # Signal fired but trade should be blocked
            assert detector._signal_fired is True

            # Executor should NOT have been called (risk blocked it)
            MockExecutor.return_value.execute.assert_not_called()

            # Blocked alert should have been sent
            mock_notifier.send_blocked_alert.assert_called_once()
