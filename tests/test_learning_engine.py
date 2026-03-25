"""Tests for learning_engine.py — record_outcome, calculate_signal_weights."""

import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


@pytest.fixture
def tmp_data(tmp_path):
    """Set up temp data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "learning_log.jsonl").write_text("")
    (data_dir / "signal_weights.json").write_text("{}")
    (data_dir / "market_regime.json").write_text('{"regime": "MEDIUM_SENSITIVITY"}')
    return str(data_dir)


class TestRecordOutcome:
    def test_basic_record(self, tmp_data, monkeypatch):
        import learning_engine as le
        monkeypatch.setattr(le, "DATA_DIR", tmp_data)
        monkeypatch.setattr(le, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        monkeypatch.setattr(le, "WEIGHTS_FILE", os.path.join(tmp_data, "signal_weights.json"))
        monkeypatch.setattr(le, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))

        trade = {
            "signal_category": "TARIFFS",
            "signal_ticker": "SQQQ",
            "actual_ticker": "SQQQ",
            "direction": "LONG",
            "entry_price": 15.0,
            "exit_price": 15.30,
            "avg_return": 1.8,
            "target_pct": 1.5,
            "stop_loss_pct": -0.5,
            "trade_id": "test-1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        record = le.record_outcome(trade)
        assert record["signal_category"] == "TARIFFS"
        assert record["pnl_pct"] > 0  # 15 -> 15.30 = +2%

        # Verify it was written to log
        with open(os.path.join(tmp_data, "learning_log.jsonl")) as f:
            lines = f.readlines()
        assert len(lines) == 1

    def test_short_direction_flips_pnl(self, tmp_data, monkeypatch):
        import learning_engine as le
        monkeypatch.setattr(le, "DATA_DIR", tmp_data)
        monkeypatch.setattr(le, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        monkeypatch.setattr(le, "WEIGHTS_FILE", os.path.join(tmp_data, "signal_weights.json"))
        monkeypatch.setattr(le, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))

        trade = {
            "signal_category": "TARIFFS",
            "direction": "SHORT",
            "entry_price": 100.0,
            "exit_price": 98.0,  # Price went DOWN — good for short
            "avg_return": -2.0,
            "trade_id": "test-2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        record = le.record_outcome(trade)
        assert record["pnl_pct"] > 0  # Short profit when price drops

    def test_zero_entry_price(self, tmp_data, monkeypatch):
        import learning_engine as le
        monkeypatch.setattr(le, "DATA_DIR", tmp_data)
        monkeypatch.setattr(le, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        monkeypatch.setattr(le, "WEIGHTS_FILE", os.path.join(tmp_data, "signal_weights.json"))
        monkeypatch.setattr(le, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))

        trade = {
            "signal_category": "TARIFFS",
            "direction": "LONG",
            "entry_price": 0,
            "exit_price": 15.0,
            "trade_id": "test-3",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        record = le.record_outcome(trade)
        assert record["pnl_pct"] == 0


class TestCalculateSignalWeights:
    def test_empty_log(self, tmp_data, monkeypatch):
        import learning_engine as le
        monkeypatch.setattr(le, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        monkeypatch.setattr(le, "WEIGHTS_FILE", os.path.join(tmp_data, "signal_weights.json"))

        weights = le.calculate_signal_weights()
        assert "_meta" in weights
        assert weights["_meta"]["total_trades"] == 0

    def test_weights_computed(self, tmp_data, monkeypatch):
        import learning_engine as le
        monkeypatch.setattr(le, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        monkeypatch.setattr(le, "WEIGHTS_FILE", os.path.join(tmp_data, "signal_weights.json"))

        # Write some fake log entries
        entries = []
        for i in range(10):
            entries.append(json.dumps({
                "signal_category": "TARIFFS",
                "ticker": "SQQQ",
                "direction": "LONG",
                "predicted_move_pct": 1.8,
                "actual_move_pct": 1.5 if i % 3 != 0 else -0.5,
                "pnl_pct": 1.5 if i % 3 != 0 else -0.5,
                "time_of_day": "10:30",
                "market_regime": "HIGH_SENSITIVITY",
                "closed_at_ts": 0,
            }))
        with open(os.path.join(tmp_data, "learning_log.jsonl"), "w") as f:
            f.write("\n".join(entries) + "\n")

        weights = le.calculate_signal_weights()
        assert "TARIFFS" in weights
        assert weights["TARIFFS"]["total_trades"] == 10
        assert weights["TARIFFS"]["rolling_win_rate"] > 0

    def test_low_win_rate_reduces_multiplier(self, tmp_data, monkeypatch):
        import learning_engine as le
        monkeypatch.setattr(le, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        monkeypatch.setattr(le, "WEIGHTS_FILE", os.path.join(tmp_data, "signal_weights.json"))

        # Write entries with low win rate (only 2/10 wins)
        entries = []
        for i in range(10):
            entries.append(json.dumps({
                "signal_category": "BAD_SIGNAL",
                "ticker": "SPY",
                "direction": "LONG",
                "predicted_move_pct": 1.0,
                "actual_move_pct": 1.0 if i < 2 else -0.5,
                "pnl_pct": 1.0 if i < 2 else -0.5,
                "time_of_day": "11:00",
                "market_regime": "LOW_SENSITIVITY",
                "closed_at_ts": 0,
            }))
        with open(os.path.join(tmp_data, "learning_log.jsonl"), "w") as f:
            f.write("\n".join(entries) + "\n")

        weights = le.calculate_signal_weights()
        assert weights["BAD_SIGNAL"]["recommended_size_multiplier"] <= 0.75


class TestApplyWeights:
    def test_apply_multiplier(self):
        import learning_engine as le
        signal = {"signal_category": "TARIFFS", "ticker": "SQQQ", "avg_return": 1.8}
        weights = {
            "TARIFFS": {
                "recommended_size_multiplier": 1.5,
                "rolling_win_rate": 0.75,
            }
        }
        result = le.apply_weights_to_signal(signal, weights)
        assert result["learned_size_multiplier"] == 1.5
        assert result["learned_win_rate"] == 0.75

    def test_unknown_category_defaults(self):
        import learning_engine as le
        signal = {"signal_category": "UNKNOWN", "ticker": "SPY"}
        weights = {"TARIFFS": {"recommended_size_multiplier": 1.5}}
        result = le.apply_weights_to_signal(signal, weights)
        assert result["learned_size_multiplier"] == 1.0
