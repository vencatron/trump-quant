"""Tests for MarketState rolling window calculations."""

import pytest
from datetime import datetime, timedelta, timezone

from botdetector.market_state import TickerState, MarketState


@pytest.fixture
def now():
    return datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ticker_state():
    return TickerState(ticker="SPY")


@pytest.fixture
def market_state():
    return MarketState(["SPY", "QQQ", "DJT"])


class TestTickerState:

    def test_update_trade(self, ticker_state, now):
        ticker_state.update_trade(540.0, 100, now)
        assert ticker_state.last_price == 540.0
        assert ticker_state.last_price_time == now
        assert len(ticker_state.trade_buffer) == 1

    def test_update_quote(self, ticker_state, now):
        ticker_state.update_quote(540.0, 540.10, now)
        assert ticker_state.last_bid == 540.0
        assert ticker_state.last_ask == 540.10
        assert len(ticker_state.spread_buffer) == 1

    def test_arm_disarm(self, ticker_state, now):
        ticker_state.update_trade(540.0, 100, now)
        ticker_state.arm(now)
        assert ticker_state.price_at_arm == 540.0
        assert ticker_state.arm_time == now

        ticker_state.disarm()
        assert ticker_state.price_at_arm is None
        assert ticker_state.arm_time is None

    def test_volume_in_window(self, ticker_state, now):
        # Add trades over 10 seconds
        for i in range(10):
            ts = now - timedelta(seconds=10 - i)
            ticker_state.update_trade(540.0, 100, ts)

        # All 10 trades within last 15 seconds
        assert ticker_state.get_volume_in_window(now, 15) == 1000
        # Only last 5 seconds
        assert ticker_state.get_volume_in_window(now, 5) == 500

    def test_volume_spike_ratio(self, ticker_state, now):
        # Baseline: 100 shares/sec over 900 seconds (simulated with sparse trades)
        for i in range(900):
            ts = now - timedelta(seconds=900 - i)
            ticker_state.update_trade(540.0, 100, ts)

        # Recent spike: 500 shares/sec in last 5 seconds
        for i in range(5):
            ts = now - timedelta(seconds=5 - i)
            # Add extra volume on top
            ticker_state.update_trade(540.0, 400, ts)

        ratio = ticker_state.get_volume_spike_ratio(now, 900, 5)
        # Recent: (100+400)*5/5 = 500/s, Rolling: 900*100 + 5*400 = 92000/900 ~= 102.2/s
        # ratio ~= 500/102.2 ~= 4.89
        assert ratio > 3.0

    def test_volume_spike_ratio_empty(self, ticker_state, now):
        assert ticker_state.get_volume_spike_ratio(now) == 0.0

    def test_price_velocity_pct(self, ticker_state, now):
        ticker_state.update_trade(100.0, 100, now)
        ticker_state.arm(now)
        # Price moves up
        ticker_state.update_trade(100.5, 100, now + timedelta(seconds=30))
        assert abs(ticker_state.get_price_velocity_pct() - 0.5) < 0.01

    def test_price_velocity_not_armed(self, ticker_state):
        assert ticker_state.get_price_velocity_pct() == 0.0

    def test_spread_baseline(self, ticker_state, now):
        # Add spread data over 15 minutes
        for i in range(100):
            ts = now - timedelta(seconds=900 - i * 9)
            ticker_state.update_quote(540.0, 540.10, ts)

        baseline = ticker_state.get_spread_baseline(now, 900)
        # spread_pct = 0.10 / 540.05 * 100 ~= 0.0185%
        assert baseline > 0

    def test_spread_widening_pct(self, ticker_state, now):
        # Build baseline with tight spreads
        for i in range(100):
            ts = now - timedelta(seconds=900 - i * 9)
            ticker_state.update_quote(540.00, 540.10, ts)

        # Current spread is much wider
        ticker_state.update_quote(539.50, 540.50, now)
        widening = ticker_state.get_spread_widening_pct(now, 900)
        # baseline ~0.0185%, current ~0.185% => widening ~900%
        assert widening > 50.0

    def test_spread_widening_no_data(self, ticker_state, now):
        assert ticker_state.get_spread_widening_pct(now) == 0.0

    def test_get_snapshot(self, ticker_state, now):
        ticker_state.update_trade(540.0, 100, now)
        ticker_state.update_quote(539.90, 540.10, now)
        snapshot = ticker_state.get_snapshot(now)

        assert snapshot.ticker == "SPY"
        assert snapshot.last_price == 540.0
        assert snapshot.bid == 539.90
        assert snapshot.ask == 540.10
        assert snapshot.spread == pytest.approx(0.20, abs=0.01)


class TestMarketState:

    def test_on_trade(self, market_state, now):
        market_state.on_trade("SPY", 540.0, 100, now)
        state = market_state.get_state("SPY")
        assert state.last_price == 540.0

    def test_on_trade_unknown_ticker(self, market_state, now):
        # Should not raise
        market_state.on_trade("AAPL", 150.0, 50, now)

    def test_on_quote(self, market_state, now):
        market_state.on_quote("SPY", 540.0, 540.10, now)
        state = market_state.get_state("SPY")
        assert state.last_bid == 540.0

    def test_arm_disarm(self, market_state, now):
        market_state.on_trade("SPY", 540.0, 100, now)
        market_state.arm("SPY", now)
        state = market_state.get_state("SPY")
        assert state.price_at_arm == 540.0

        market_state.disarm("SPY")
        assert state.price_at_arm is None

    def test_arm_all_disarm_all(self, market_state, now):
        for ticker in ["SPY", "QQQ", "DJT"]:
            market_state.on_trade(ticker, 100.0, 50, now)

        market_state.arm_all(now)
        for ticker in ["SPY", "QQQ", "DJT"]:
            assert market_state.get_state(ticker).price_at_arm == 100.0

        market_state.disarm_all()
        for ticker in ["SPY", "QQQ", "DJT"]:
            assert market_state.get_state(ticker).price_at_arm is None

    def test_get_snapshot(self, market_state, now):
        market_state.on_trade("SPY", 540.0, 100, now)
        market_state.on_quote("SPY", 539.90, 540.10, now)
        snapshot = market_state.get_snapshot("SPY", now)
        assert snapshot is not None
        assert snapshot.ticker == "SPY"

    def test_get_snapshot_unknown_ticker(self, market_state, now):
        assert market_state.get_snapshot("AAPL", now) is None

    def test_get_state_unknown(self, market_state):
        assert market_state.get_state("AAPL") is None
