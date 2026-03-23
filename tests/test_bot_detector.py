"""Tests for BotDetector signature detection logic."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from botdetector.config import BotDetectorConfig
from botdetector.bot_detector import BotDetector
from botdetector.models import SignalDirection


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


@pytest.fixture
def detector(config):
    with patch("botdetector.bot_detector.AlpacaWSClient"), \
         patch("botdetector.bot_detector.TradeExecutor") as MockExecutor, \
         patch("botdetector.bot_detector.RiskManager") as MockRisk, \
         patch("botdetector.bot_detector.Notifier"):
        mock_risk = MagicMock()
        mock_risk.check_can_trade.return_value = (True, "OK")
        MockRisk.return_value = mock_risk
        mock_executor = MagicMock()
        mock_executor.execute.return_value = MagicMock()
        MockExecutor.return_value = mock_executor
        d = BotDetector(config)
        return d


@pytest.fixture
def now():
    return datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)


def build_baseline(detector, ticker, now, seconds=900):
    """Populate 15 min of baseline data."""
    for i in range(seconds):
        ts = now - timedelta(seconds=seconds - i)
        detector.market_state.on_trade(ticker, 540.0, 100, ts)
        if i % 10 == 0:
            detector.market_state.on_quote(ticker, 539.95, 540.05, ts)


class TestArmDisarm:

    def test_arm_sets_state(self, detector, now):
        detector.arm("post-1", "Trump tariffs", ["TARIFFS"], timestamp=now)

        assert detector._armed is True
        assert detector._arm_time == now
        assert detector._arm_post["id"] == "post-1"
        assert "SPY" in detector._arm_tickers

    def test_arm_uses_category_mapping(self, detector, now):
        detector.arm("post-1", "Crypto", ["CRYPTO"], timestamp=now)
        assert "COIN" in detector._arm_tickers

    def test_arm_custom_tickers(self, detector, now):
        detector.arm("post-1", "Test", ["TARIFFS"], tickers=["TSLA", "NVDA"], timestamp=now)
        assert detector._arm_tickers == ["TSLA", "NVDA"]

    def test_arm_while_armed_ignored(self, detector, now):
        detector.arm("post-1", "First", ["TARIFFS"], timestamp=now)
        detector.arm("post-2", "Second", ["CRYPTO"], timestamp=now)
        # Should still be armed with first post
        assert detector._arm_post["id"] == "post-1"

    def test_disarm(self, detector, now):
        detector.arm("post-1", "Test", ["TARIFFS"], timestamp=now)
        detector.disarm()
        assert detector._armed is False

    def test_disarm_when_not_armed(self, detector):
        # Should not raise
        detector.disarm()
        assert detector._armed is False


class TestSignatureDetection:

    def test_all_three_criteria_fires_signal(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)

        detector.arm("post-1", "Tariffs", ["TARIFFS"], timestamp=now)

        # Simulate bot activity: volume spike + price move + spread widening
        spike_time = now + timedelta(seconds=30)

        # Volume spike: inject 3x+ volume in recent 5 seconds
        for i in range(5):
            ts = spike_time - timedelta(seconds=5 - i)
            detector.market_state.on_trade(ticker, 541.62, 500, ts)

        # Price move: 0.3%+ from arm price (540.0 -> 541.62 = 0.3%)
        # Already done above

        # Spread widening: wide quotes
        detector.market_state.on_quote(ticker, 540.50, 542.50, spike_time)

        # Trigger check
        with patch("botdetector.bot_detector.log_signal"), \
             patch("asyncio.create_task"):
            detector._check_signature(ticker, spike_time)

        assert detector._signal_fired is True

    def test_two_of_three_does_not_fire(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)

        detector.arm("post-1", "Tariffs", ["TARIFFS"], timestamp=now)

        spike_time = now + timedelta(seconds=30)

        # Volume spike only (no price move, no spread widening)
        for i in range(5):
            ts = spike_time - timedelta(seconds=5 - i)
            detector.market_state.on_trade(ticker, 540.0, 500, ts)

        # Spread widening but no price velocity
        detector.market_state.on_quote(ticker, 539.00, 541.00, spike_time)

        detector._check_signature(ticker, spike_time)

        # Volume + spread confirmed, but velocity not (price didn't move from arm)
        assert detector._volume_confirmed is True
        assert detector._velocity_confirmed is False
        assert detector._signal_fired is False

    def test_criteria_after_window_expires_no_signal(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)

        detector.arm("post-1", "Tariffs", ["TARIFFS"], timestamp=now)

        # Beyond 120s window
        late_time = now + timedelta(seconds=150)

        for i in range(5):
            ts = late_time - timedelta(seconds=5 - i)
            detector.market_state.on_trade(ticker, 541.62, 500, ts)
        detector.market_state.on_quote(ticker, 540.50, 542.50, late_time)

        detector._check_signature(ticker, late_time)

        assert detector._signal_fired is False

    def test_handle_trade_triggers_check(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)

        detector.arm("post-1", "Test", ["TARIFFS"], tickers=[ticker], timestamp=now)

        # Feed trade data - should call _check_signature internally
        spike_time = now + timedelta(seconds=30)
        detector._handle_trade(ticker, 540.0, 100, spike_time)

        # No signal because not enough activity, but _check was called
        assert detector._signal_fired is False

    def test_handle_quote_triggers_check(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)

        detector.arm("post-1", "Test", ["TARIFFS"], tickers=[ticker], timestamp=now)

        spike_time = now + timedelta(seconds=30)
        detector._handle_quote(ticker, 539.90, 540.10, spike_time)

        assert detector._signal_fired is False

    def test_non_armed_ticker_ignored(self, detector, now):
        build_baseline(detector, "SPY", now)

        detector.arm("post-1", "Test", ["TARIFFS"], tickers=["SPY"], timestamp=now)

        # QQQ data should not trigger check
        spike_time = now + timedelta(seconds=30)
        detector._handle_trade("QQQ", 450.0, 1000, spike_time)
        assert detector._signal_fired is False

    def test_signal_direction_long(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)
        detector.arm("post-1", "Tariffs", ["TARIFFS"], timestamp=now)

        spike_time = now + timedelta(seconds=30)

        # Positive price move
        for i in range(5):
            ts = spike_time - timedelta(seconds=5 - i)
            detector.market_state.on_trade(ticker, 541.62, 500, ts)
        detector.market_state.on_quote(ticker, 540.50, 542.50, spike_time)

        with patch("botdetector.bot_detector.log_signal"), \
             patch("asyncio.create_task"):
            detector._check_signature(ticker, spike_time)

        assert detector._signal_fired is True

    def test_assess_confidence_detected(self, detector, now):
        ticker = "SPY"
        build_baseline(detector, ticker, now)
        state = detector.market_state.get_state(ticker)

        # Metrics at threshold level (1x)
        confidence = detector._assess_confidence(state, now)
        assert confidence == "DETECTED"


class TestMultipleArmCycles:

    def test_arm_disarm_rearm(self, detector, now):
        detector.arm("post-1", "First", ["TARIFFS"], timestamp=now)
        detector.disarm()

        second_time = now + timedelta(seconds=300)
        detector.arm("post-2", "Second", ["CRYPTO"], timestamp=second_time)

        assert detector._armed is True
        assert detector._arm_post["id"] == "post-2"
        assert "COIN" in detector._arm_tickers
        assert detector._signal_fired is False
