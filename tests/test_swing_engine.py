"""Tests for swing_engine.py — open/close/monitor logic with mocked Alpaca."""

import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


@pytest.fixture
def tmp_data(tmp_path):
    """Set up temp data directory with required files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "swing_positions.json").write_text("[]")
    (data_dir / "swing_trailing_stops.json").write_text("{}")
    return str(data_dir)


class TestLoadSavePositions:
    def test_load_empty(self, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        positions = se.load_swing_positions()
        assert positions == []

    def test_save_and_load(self, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        test_positions = [{"ticker": "GLD", "entry_price": 290.0}]
        se.save_swing_positions(test_positions)
        loaded = se.load_swing_positions()
        assert len(loaded) == 1
        assert loaded[0]["ticker"] == "GLD"

    def test_load_corrupted_file(self, tmp_data, monkeypatch):
        import swing_engine as se
        filepath = os.path.join(tmp_data, "swing_positions.json")
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", filepath)
        with open(filepath, "w") as f:
            f.write("not json {{{")
        positions = se.load_swing_positions()
        assert positions == []


class TestOpenSwingPosition:
    @patch("swing_engine.submit_order")
    @patch("swing_engine.get_price")
    def test_open_position_success(self, mock_price, mock_order, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        monkeypatch.setattr(se, "SWING_LOG_FILE", os.path.join(tmp_data, "swing_log.jsonl"))

        mock_price.return_value = 290.50
        mock_order.return_value = {"id": "order-123", "status": "filled"}

        result = se.open_swing_position(
            ticker="GLD", direction="BUY", signal_category="TARIFFS",
            thesis="Gold rises on tariffs", target_pct=2.3, stop_pct=1.5,
            hold_days=7, conviction="VERY_HIGH"
        )

        assert result is not None
        assert result["ticker"] == "GLD"
        assert result["direction"] == "BUY"
        assert result["shares"] == 17  # 5000 / 290.50 = 17
        mock_order.assert_called_once_with("GLD", 17, "buy")

    @patch("swing_engine.get_price")
    def test_max_positions_blocks(self, mock_price, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))

        # Pre-fill 2 positions (max)
        existing = [
            {"ticker": "GLD", "entry_price": 290},
            {"ticker": "XLE", "entry_price": 85},
        ]
        se.save_swing_positions(existing)

        result = se.open_swing_position(
            ticker="LMT", direction="BUY", signal_category="WAR",
            thesis="test", target_pct=5.0, stop_pct=2.0,
            hold_days=7, conviction="HIGH"
        )
        assert result is None
        mock_price.assert_not_called()

    @patch("swing_engine.get_price")
    def test_duplicate_ticker_blocks(self, mock_price, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))

        existing = [{"ticker": "GLD", "entry_price": 290}]
        se.save_swing_positions(existing)

        result = se.open_swing_position(
            ticker="GLD", direction="BUY", signal_category="TARIFFS",
            thesis="test", target_pct=2.3, stop_pct=1.5,
            hold_days=7, conviction="HIGH"
        )
        assert result is None

    @patch("swing_engine.get_price")
    def test_no_price_blocks(self, mock_price, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        mock_price.return_value = None

        result = se.open_swing_position(
            ticker="GLD", direction="BUY", signal_category="TARIFFS",
            thesis="test", target_pct=2.3, stop_pct=1.5,
            hold_days=7, conviction="HIGH"
        )
        assert result is None


class TestMonitorSwingPositions:
    @patch("swing_engine.alpaca_close_position")
    @patch("swing_engine.get_price")
    def test_target_hit_closes(self, mock_price, mock_close, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        monkeypatch.setattr(se, "SWING_LOG_FILE", os.path.join(tmp_data, "swing_log.jsonl"))
        monkeypatch.setattr(se, "SWING_TRAILING_FILE", os.path.join(tmp_data, "swing_trailing_stops.json"))

        # Position with 2.3% target
        positions = [{
            "ticker": "GLD", "direction": "BUY", "entry_price": 290.0,
            "position_value": 5000, "target_pct": 2.3, "stop_pct": 1.5,
            "hold_days": 7, "shares": 17,
            "exit_by": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        }]
        se.save_swing_positions(positions)

        # Price is up 3% (above 2.3% target)
        mock_price.return_value = 298.70  # 290 * 1.03
        mock_close.return_value = True

        closed = se.monitor_swing_positions()
        assert len(closed) == 1
        assert "TARGET_HIT" in closed[0]["close_reason"]

    @patch("swing_engine.alpaca_close_position")
    @patch("swing_engine.get_price")
    def test_stop_loss_closes(self, mock_price, mock_close, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        monkeypatch.setattr(se, "SWING_LOG_FILE", os.path.join(tmp_data, "swing_log.jsonl"))
        monkeypatch.setattr(se, "SWING_TRAILING_FILE", os.path.join(tmp_data, "swing_trailing_stops.json"))

        positions = [{
            "ticker": "GLD", "direction": "BUY", "entry_price": 290.0,
            "position_value": 5000, "target_pct": 2.3, "stop_pct": 1.5,
            "hold_days": 7, "shares": 17,
            "exit_by": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        }]
        se.save_swing_positions(positions)

        # Price is down 2% (below 1.5% stop)
        mock_price.return_value = 284.20  # 290 * 0.98
        mock_close.return_value = True

        closed = se.monitor_swing_positions()
        assert len(closed) == 1
        assert "STOP_LOSS" in closed[0]["close_reason"]

    @patch("swing_engine.get_price")
    def test_no_close_in_range(self, mock_price, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        monkeypatch.setattr(se, "SWING_LOG_FILE", os.path.join(tmp_data, "swing_log.jsonl"))
        monkeypatch.setattr(se, "SWING_TRAILING_FILE", os.path.join(tmp_data, "swing_trailing_stops.json"))

        positions = [{
            "ticker": "GLD", "direction": "BUY", "entry_price": 290.0,
            "position_value": 5000, "target_pct": 2.3, "stop_pct": 1.5,
            "hold_days": 7, "shares": 17,
            "exit_by": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        }]
        se.save_swing_positions(positions)

        # Price is up 1% (within range)
        mock_price.return_value = 292.90  # 290 * 1.01
        closed = se.monitor_swing_positions()
        assert len(closed) == 0

    @patch("swing_engine.alpaca_close_position")
    @patch("swing_engine.get_price")
    def test_time_exit_closes(self, mock_price, mock_close, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        monkeypatch.setattr(se, "SWING_LOG_FILE", os.path.join(tmp_data, "swing_log.jsonl"))
        monkeypatch.setattr(se, "SWING_TRAILING_FILE", os.path.join(tmp_data, "swing_trailing_stops.json"))

        positions = [{
            "ticker": "GLD", "direction": "BUY", "entry_price": 290.0,
            "position_value": 5000, "target_pct": 2.3, "stop_pct": 1.5,
            "hold_days": 7, "shares": 17,
            "exit_by": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),  # Expired yesterday
        }]
        se.save_swing_positions(positions)

        mock_price.return_value = 291.0
        mock_close.return_value = True

        closed = se.monitor_swing_positions()
        assert len(closed) == 1
        assert "TIME_EXIT" in closed[0]["close_reason"]


class TestProcessSignalForSwing:
    @patch("swing_engine.open_swing_position")
    def test_valid_category(self, mock_open, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        mock_open.return_value = {"ticker": "GLD", "position_value": 5000}

        result = se.process_signal_for_swing("TARIFFS", "Trump announces tariffs")
        assert len(result) >= 1
        mock_open.assert_called()

    def test_invalid_category(self, tmp_data, monkeypatch):
        import swing_engine as se
        result = se.process_signal_for_swing("NONSENSE_CATEGORY")
        assert result == []

    @patch("swing_engine.open_swing_position")
    def test_iran_escalation_signals(self, mock_open, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        mock_open.return_value = {"ticker": "XLE", "position_value": 5000}

        result = se.process_signal_for_swing("IRAN_ESCALATION")
        # Should try to open XLE and LMT
        assert mock_open.call_count >= 1


class TestGetSwingSummary:
    def test_empty_positions(self, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        summary = se.get_swing_summary()
        assert summary["positions"] == 0
        assert summary["total_value"] == 0

    def test_with_positions(self, tmp_data, monkeypatch):
        import swing_engine as se
        monkeypatch.setattr(se, "SWING_POSITIONS_FILE", os.path.join(tmp_data, "swing_positions.json"))
        positions = [{
            "ticker": "GLD", "direction": "BUY", "position_value": 5000,
            "current_pnl_dollars": 150.0, "current_pnl_pct": 3.0,
            "thesis": "Gold on tariffs", "hold_days": 7,
        }]
        se.save_swing_positions(positions)
        summary = se.get_swing_summary()
        assert summary["positions"] == 1
        assert summary["total_pnl"] == 150.0
