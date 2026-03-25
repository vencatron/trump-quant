"""Tests for signal_check.py — cooldown logic, dedup, after-hours gate, position sizing."""

import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


# We need to patch env vars BEFORE importing signal_check
@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory with required files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "active_scalps.json").write_text("[]")
    (data_dir / "bot_trades.json").write_text("[]")
    (data_dir / "monitor_seen.json").write_text("[]")
    (data_dir / "traded_today.json").write_text('{"date": "", "trades": []}')
    (data_dir / "signal_cooldowns.json").write_text("{}")
    (data_dir / "daily_trade_count.json").write_text('{"date": "", "count": 0}')
    return str(data_dir)


class TestCooldownLogic:
    """Test signal cooldown enforcement."""

    def test_cooldown_not_set(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SIGNAL_COOLDOWN_FILE", os.path.join(tmp_data_dir, "signal_cooldowns.json"))
        assert sc.is_on_cooldown("TARIFFS", "SQQQ") is False

    def test_set_cooldown(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SIGNAL_COOLDOWN_FILE", os.path.join(tmp_data_dir, "signal_cooldowns.json"))
        sc.set_cooldown("TARIFFS", "SQQQ")
        assert sc.is_on_cooldown("TARIFFS", "SQQQ") is True

    def test_cooldown_expires(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SIGNAL_COOLDOWN_FILE", os.path.join(tmp_data_dir, "signal_cooldowns.json"))
        # Set a cooldown that expired 1 hour ago
        cooldowns = {
            "TARIFFS_SQQQ": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        }
        with open(os.path.join(tmp_data_dir, "signal_cooldowns.json"), "w") as f:
            json.dump(cooldowns, f)
        assert sc.is_on_cooldown("TARIFFS", "SQQQ") is False

    def test_cooldown_key_format(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SIGNAL_COOLDOWN_FILE", os.path.join(tmp_data_dir, "signal_cooldowns.json"))
        sc.set_cooldown("IRAN_ESCALATION", "UVIX")
        cooldowns = sc.load_signal_cooldowns()
        assert "IRAN_ESCALATION_UVIX" in cooldowns

    def test_different_category_not_on_cooldown(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SIGNAL_COOLDOWN_FILE", os.path.join(tmp_data_dir, "signal_cooldowns.json"))
        sc.set_cooldown("TARIFFS", "SQQQ")
        assert sc.is_on_cooldown("FED_ATTACK", "SQQQ") is False


class TestDedupLogic:
    """Test post/trade deduplication."""

    def test_not_traded_today(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "TRADED_TODAY_FILE", os.path.join(tmp_data_dir, "traded_today.json"))
        assert sc.was_traded_today("post-123", "SPY") is False

    def test_record_and_check_traded(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "TRADED_TODAY_FILE", os.path.join(tmp_data_dir, "traded_today.json"))
        sc.record_traded_today("post-123", "SPY")
        assert sc.was_traded_today("post-123", "SPY") is True

    def test_different_ticker_not_deduped(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "TRADED_TODAY_FILE", os.path.join(tmp_data_dir, "traded_today.json"))
        sc.record_traded_today("post-123", "SPY")
        assert sc.was_traded_today("post-123", "QQQ") is False

    def test_different_post_not_deduped(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "TRADED_TODAY_FILE", os.path.join(tmp_data_dir, "traded_today.json"))
        sc.record_traded_today("post-123", "SPY")
        assert sc.was_traded_today("post-456", "SPY") is False

    def test_traded_today_resets_at_midnight(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "TRADED_TODAY_FILE", os.path.join(tmp_data_dir, "traded_today.json"))
        # Write data for yesterday
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        with open(os.path.join(tmp_data_dir, "traded_today.json"), "w") as f:
            json.dump({"date": yesterday, "trades": [{"post_id": "old", "ticker": "SPY"}]}, f)
        assert sc.was_traded_today("old", "SPY") is False


class TestAfterHoursGate:
    """Test that trades are blocked outside market hours."""

    def test_market_open_weekday(self):
        import signal_check as sc
        # 15:00 UTC on Monday = 11:00 ET (market open)
        with patch("signal_check.datetime") as mock_dt:
            mock_now = datetime(2026, 3, 23, 15, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert sc.is_market_open() is True

    def test_market_closed_weekend(self):
        import signal_check as sc
        # Saturday
        with patch("signal_check.datetime") as mock_dt:
            mock_now = datetime(2026, 3, 28, 15, 0, 0, tzinfo=timezone.utc)  # Saturday
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert sc.is_market_open() is False

    def test_market_closed_late(self):
        import signal_check as sc
        # 22:00 UTC = 18:00 ET (market closed)
        with patch("signal_check.datetime") as mock_dt:
            mock_now = datetime(2026, 3, 23, 22, 0, 0, tzinfo=timezone.utc)  # Monday
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert sc.is_market_open() is False

    def test_execute_trade_blocks_after_hours(self, tmp_data_dir, monkeypatch):
        """execute_paper_trade should return None outside market hours."""
        import signal_check as sc
        monkeypatch.setattr(sc, "DATA_DIR", tmp_data_dir)
        monkeypatch.setattr(sc, "TRADES_FILE", os.path.join(tmp_data_dir, "bot_trades.json"))
        monkeypatch.setattr(sc, "TRADED_TODAY_FILE", os.path.join(tmp_data_dir, "traded_today.json"))
        monkeypatch.setattr(sc, "SIGNAL_COOLDOWN_FILE", os.path.join(tmp_data_dir, "signal_cooldowns.json"))
        monkeypatch.setattr(sc, "DAILY_TRADE_COUNT_FILE", os.path.join(tmp_data_dir, "daily_trade_count.json"))

        signal = {"ticker": "SPY", "action": "BUY", "window": "same day",
                  "confidence": "HIGH", "avg_return": 1.5, "direction": "BULLISH"}
        post = {"id": "test-1", "text": "test post"}

        # Mock to 22:00 ET (after hours)
        with patch("signal_check.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 22  # 10 PM ET — way after hours
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = sc.execute_paper_trade(signal, post, "TARIFFS")
            assert result is None


class TestDailyTradeLimit:
    """Test daily trade count enforcement."""

    def test_initial_count_zero(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "DAILY_TRADE_COUNT_FILE", os.path.join(tmp_data_dir, "daily_trade_count.json"))
        assert sc.get_daily_trade_count() == 0

    def test_increment_count(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "DAILY_TRADE_COUNT_FILE", os.path.join(tmp_data_dir, "daily_trade_count.json"))
        sc.increment_daily_trade_count()
        assert sc.get_daily_trade_count() == 1
        sc.increment_daily_trade_count()
        assert sc.get_daily_trade_count() == 2

    def test_count_resets_new_day(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "DAILY_TRADE_COUNT_FILE", os.path.join(tmp_data_dir, "daily_trade_count.json"))
        # Write count for yesterday
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        with open(os.path.join(tmp_data_dir, "daily_trade_count.json"), "w") as f:
            json.dump({"date": yesterday, "count": 5}, f)
        assert sc.get_daily_trade_count() == 0


class TestPositionSizing:
    """Test that position sizes are capped correctly."""

    def test_max_per_ticker_daily(self):
        import signal_check as sc
        assert sc.MAX_PER_TICKER_DAILY == 2500

    def test_max_concurrent_positions(self):
        import signal_check as sc
        assert sc.MAX_CONCURRENT_POSITIONS == 2

    def test_max_daily_exposure(self):
        import signal_check as sc
        assert sc.MAX_DAILY_EXPOSURE == 10000

    def test_max_trades_per_day(self):
        import signal_check as sc
        assert sc.MAX_TRADES_PER_DAY == 6


class TestHeadlineNormalization:
    """Test content-based dedup normalization."""

    def test_normalize_removes_urls(self):
        import signal_check as sc
        result = sc._normalize_headline("Trump says https://example.com tariffs")
        assert "example" not in result
        assert "trump" in result

    def test_normalize_lowercases(self):
        import signal_check as sc
        result = sc._normalize_headline("TRUMP ANNOUNCES TARIFFS")
        assert result == "trump announces tariffs"

    def test_normalize_strips_punctuation(self):
        import signal_check as sc
        result = sc._normalize_headline("Trump's tariffs!!!")
        assert "!" not in result
        assert "'" not in result


class TestSeenPersistence:
    """Test seen post ID tracking."""

    def test_load_empty(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SEEN_FILE", os.path.join(tmp_data_dir, "monitor_seen.json"))
        seen = sc.load_seen()
        assert isinstance(seen, set)
        assert len(seen) == 0

    def test_save_and_load(self, tmp_data_dir, monkeypatch):
        import signal_check as sc
        monkeypatch.setattr(sc, "SEEN_FILE", os.path.join(tmp_data_dir, "monitor_seen.json"))
        seen = {"post-1", "post-2", "post-3"}
        sc.save_seen(seen)
        loaded = sc.load_seen()
        assert loaded == seen
