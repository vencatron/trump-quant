"""Tests for TradeExecutor position sizing and exit logic."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from botdetector.config import BotDetectorConfig
from botdetector.trade_executor import TradeExecutor
from botdetector.models import (
    BotSignal, Trade, TradeStatus, ExitReason,
    SignalDirection, MarketSnapshot,
)


@pytest.fixture
def config():
    return BotDetectorConfig(
        max_position_pct=0.05,
        max_position_dollars=2500.0,
        stop_loss_pct=0.5,
        take_profit_pct=1.5,
        trailing_stop_pct=0.3,
        trailing_stop_activation_pct=0.5,
        min_hold_sec=60,
        max_hold_sec=3600,
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


class TestPositionSizing:

    def test_position_size_by_pct(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # Equity $50k, 5% = $2500, price $540 => 4 shares
        shares, value = executor._calculate_position_size(540.0, 50000.0)
        assert shares == 4
        assert value == pytest.approx(4 * 540.0)

    def test_position_size_capped_by_dollars(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # Equity $200k, 5% = $10k, but hard cap $2500, price $540 => 4 shares
        shares, value = executor._calculate_position_size(540.0, 200000.0)
        assert shares == 4
        assert value == pytest.approx(4 * 540.0)

    def test_position_size_zero_price(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config
        shares, value = executor._calculate_position_size(0.0, 50000.0)
        assert shares == 0
        assert value == 0.0

    def test_position_size_expensive_stock(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # Price $3000, max $2500 => 0 shares
        shares, value = executor._calculate_position_size(3000.0, 50000.0)
        assert shares == 0
        assert value == 0.0

    def test_position_size_small_equity(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # Equity $1000, 5% = $50, price $10 => 5 shares
        shares, value = executor._calculate_position_size(10.0, 1000.0)
        assert shares == 5
        assert value == pytest.approx(50.0)


class TestStopLossCalculation:

    def test_stop_loss_long(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # 0.5% stop loss on $100 entry LONG => $99.50
        sl = executor._calculate_stop_loss(100.0, SignalDirection.LONG)
        assert sl == pytest.approx(99.50)

    def test_stop_loss_short(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # 0.5% stop loss on $100 entry SHORT => $100.50
        sl = executor._calculate_stop_loss(100.0, SignalDirection.SHORT)
        assert sl == pytest.approx(100.50)

    def test_take_profit_long(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # 1.5% TP on $100 entry LONG => $101.50
        tp = executor._calculate_take_profit(100.0, SignalDirection.LONG)
        assert tp == pytest.approx(101.50)

    def test_take_profit_short(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        # 1.5% TP on $100 entry SHORT => $98.50
        tp = executor._calculate_take_profit(100.0, SignalDirection.SHORT)
        assert tp == pytest.approx(98.50)


class TestTrailingStop:

    def test_trailing_stop_activates_long(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        trade = Trade(
            trade_id="t1", signal_id="s1", ticker="SPY",
            direction=SignalDirection.LONG,
            entry_price=100.0,
            trailing_stop_price=99.50,  # Initial stop
        )

        # Price moves up 0.6% (above 0.5% activation threshold)
        executor._update_trailing_stop(trade, 100.60)
        # New trailing stop: 100.60 * (1 - 0.003) = 100.2982
        assert trade.trailing_stop_price > 99.50
        assert trade.trailing_stop_price == pytest.approx(100.60 * 0.997, rel=1e-4)

    def test_trailing_stop_only_ratchets_up_long(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        trade = Trade(
            trade_id="t1", signal_id="s1", ticker="SPY",
            direction=SignalDirection.LONG,
            entry_price=100.0,
            trailing_stop_price=100.30,  # Already ratcheted up
        )

        # Price at 100.55 (still above activation but trailing stop would be lower)
        executor._update_trailing_stop(trade, 100.55)
        # 100.55 * 0.997 = 100.2484 < 100.30, so should NOT update
        assert trade.trailing_stop_price == 100.30

    def test_trailing_stop_does_not_activate_below_threshold(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        trade = Trade(
            trade_id="t1", signal_id="s1", ticker="SPY",
            direction=SignalDirection.LONG,
            entry_price=100.0,
            trailing_stop_price=99.50,
        )

        # Only 0.3% gain - below 0.5% activation
        executor._update_trailing_stop(trade, 100.30)
        assert trade.trailing_stop_price == 99.50  # Unchanged

    def test_trailing_stop_short(self, config):
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.config = config

        trade = Trade(
            trade_id="t1", signal_id="s1", ticker="SPY",
            direction=SignalDirection.SHORT,
            entry_price=100.0,
            trailing_stop_price=100.50,  # Initial stop for short
        )

        # Price drops 0.6% (favorable for short)
        executor._update_trailing_stop(trade, 99.40)
        # New trailing stop: 99.40 * 1.003 = 99.6982
        assert trade.trailing_stop_price < 100.50
        assert trade.trailing_stop_price == pytest.approx(99.40 * 1.003, rel=1e-4)


class TestExecute:

    @patch("botdetector.trade_executor.AlpacaRESTClient")
    @patch("botdetector.trade_executor.Notifier")
    def test_execute_success(self, MockNotifier, MockREST, config, signal):
        mock_rest = MagicMock()
        mock_rest.get_account.return_value = {"equity": 50000.0}
        mock_rest.submit_market_order.return_value = {
            "order_id": "ord-123",
            "status": "filled",
            "filled_avg_price": 540.0,
            "submitted_at": "2026-03-23T15:00:00Z",
        }
        MockREST.return_value = mock_rest
        MockNotifier.return_value = MagicMock()

        executor = TradeExecutor(config)
        trade = executor.execute(signal)

        assert trade is not None
        assert trade.ticker == "SPY"
        assert trade.shares == 4  # $2500 / $540 = 4
        assert trade.status == TradeStatus.FILLED
        assert trade.paper_mode is True
        assert trade.stop_loss_price < trade.entry_price
        assert trade.take_profit_price > trade.entry_price

    @patch("botdetector.trade_executor.AlpacaRESTClient")
    @patch("botdetector.trade_executor.Notifier")
    def test_execute_zero_shares(self, MockNotifier, MockREST, config):
        mock_rest = MagicMock()
        mock_rest.get_account.return_value = {"equity": 50000.0}
        MockREST.return_value = mock_rest
        MockNotifier.return_value = MagicMock()

        now = datetime.now(timezone.utc)
        snapshot = MarketSnapshot(
            ticker="BRK.A", timestamp=now, last_price=600000.0,
            bid=599990.0, ask=600010.0, spread=20.0, spread_pct=0.003,
            volume_1s=10, volume_rolling=1.0, volume_spike_ratio=4.0,
            price_at_arm=599000.0, price_velocity_pct=0.17,
            spread_baseline=0.002, spread_widening_pct=50.0,
        )
        expensive_signal = BotSignal(
            signal_id="s2", ticker="BRK.A",
            direction=SignalDirection.LONG,
            post_id="p2", post_text="test",
            post_categories=["MARKET_PUMP"],
            post_timestamp=now, detection_timestamp=now,
            seconds_after_post=30.0,
            volume_spike_ratio=4.0,
            price_velocity_pct=0.3,
            spread_widening_pct=60.0,
            entry_price=600000.0,
            snapshot=snapshot,
        )

        executor = TradeExecutor(config)
        trade = executor.execute(expensive_signal)
        assert trade is None

    @patch("botdetector.trade_executor.AlpacaRESTClient")
    @patch("botdetector.trade_executor.Notifier")
    def test_execute_api_failure(self, MockNotifier, MockREST, config, signal):
        mock_rest = MagicMock()
        mock_rest.get_account.side_effect = Exception("API down")
        MockREST.return_value = mock_rest
        MockNotifier.return_value = MagicMock()

        executor = TradeExecutor(config)
        trade = executor.execute(signal)
        assert trade is None
