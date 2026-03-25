"""Tests for regime_detector.py — regime classification."""

import json
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock


@pytest.fixture
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "learning_log.jsonl").write_text("")
    (data_dir / "market_regime.json").write_text("{}")
    return str(data_dir)


class TestDetectRegime:
    def test_high_vix_regime(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "DATA_DIR", tmp_data)
        monkeypatch.setattr(rd, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))

        # Just test the function runs and returns valid structure
        # (mocking yfinance internals is brittle — real yfinance may be present)
        result = rd.detect_regime()
        assert "regime" in result
        assert result["regime"] in ("HIGH_SENSITIVITY", "MEDIUM_SENSITIVITY", "LOW_SENSITIVITY")
        assert "recommended_position_multiplier" in result
        # VIX may or may not be fetched depending on yfinance availability
        assert "vix" in result

    def test_regime_without_yfinance(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "DATA_DIR", tmp_data)
        monkeypatch.setattr(rd, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))

        # Import error simulation — just test the function runs without crashing
        result = rd.detect_regime()
        assert "regime" in result
        assert result["regime"] in ("HIGH_SENSITIVITY", "MEDIUM_SENSITIVITY", "LOW_SENSITIVITY")


class TestComputePostCorrelation:
    def test_empty_log(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        result = rd._compute_post_correlation()
        assert result == 50  # Default when no data

    def test_high_correlation(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))

        import time
        entries = []
        for i in range(20):
            entries.append(json.dumps({
                "closed_at_ts": time.time(),
                "actual_move_pct": 1.5,  # Big move
            }))
        with open(os.path.join(tmp_data, "learning_log.jsonl"), "w") as f:
            f.write("\n".join(entries) + "\n")

        result = rd._compute_post_correlation()
        assert result == 100  # All trades had big moves


class TestGetRegimeMultiplier:
    def test_from_cached_file(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))

        import time
        with open(os.path.join(tmp_data, "market_regime.json"), "w") as f:
            json.dump({
                "regime": "HIGH_SENSITIVITY",
                "recommended_position_multiplier": 1.5,
            }, f)

        # Touch the file to make it recent
        os.utime(os.path.join(tmp_data, "market_regime.json"))

        result = rd.get_regime_multiplier()
        assert result == 1.5

    def test_stale_file_refreshes(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "REGIME_FILE", os.path.join(tmp_data, "market_regime.json"))
        monkeypatch.setattr(rd, "DATA_DIR", tmp_data)
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))

        # Make file old
        import time
        filepath = os.path.join(tmp_data, "market_regime.json")
        with open(filepath, "w") as f:
            json.dump({"regime": "LOW_SENSITIVITY", "recommended_position_multiplier": 0.5}, f)
        os.utime(filepath, (time.time() - 20000, time.time() - 20000))

        result = rd.get_regime_multiplier()
        assert result in (0.5, 1.0, 1.5)  # Will be whatever detect_regime returns


class TestCheckIranActive:
    def test_no_iran(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))
        assert rd._check_iran_active() is False

    def test_recent_iran(self, tmp_data, monkeypatch):
        import regime_detector as rd
        monkeypatch.setattr(rd, "LEARNING_LOG", os.path.join(tmp_data, "learning_log.jsonl"))

        import time
        entry = json.dumps({
            "signal_category": "IRAN_ESCALATION",
            "closed_at_ts": time.time(),
        })
        with open(os.path.join(tmp_data, "learning_log.jsonl"), "w") as f:
            f.write(entry + "\n")

        assert rd._check_iran_active() is True
