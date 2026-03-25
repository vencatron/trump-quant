"""Tests for categorize.py — post categorization logic."""

import pytest


class TestCategorizePost:
    def test_tariff_post(self):
        from categorize import categorize_post
        result = categorize_post("Trump announces new tariffs on China imports")
        assert "TARIFFS" in result["categories"]

    def test_iran_escalation(self):
        from categorize import categorize_post
        result = categorize_post("Trump orders military strike on Iran nuclear facility")
        assert "IRAN_ESCALATION" in result["categories"]

    def test_iran_deescalation(self):
        from categorize import categorize_post
        result = categorize_post("Iran deal reached, peace negotiations succeed")
        assert "IRAN_DEESCALATION" in result["categories"]

    def test_iran_escalation_requires_military_words(self):
        from categorize import categorize_post
        # "iran deal" without military words should be deescalation
        result = categorize_post("Iran deal postponed, negotiations continue")
        assert "IRAN_ESCALATION" not in result["categories"]

    def test_crypto_post(self):
        from categorize import categorize_post
        result = categorize_post("Bitcoin crypto reserve announced by Trump")
        assert "CRYPTO" in result["categories"]

    def test_fed_attack(self):
        from categorize import categorize_post
        result = categorize_post("Trump attacks Powell, says Fed should cut rates now")
        assert "FED_ATTACK" in result["categories"]

    def test_musk_trump(self):
        from categorize import categorize_post
        result = categorize_post("Elon Musk meets with Trump at White House")
        assert "MUSK_TRUMP" in result["categories"]

    def test_ticker_detection(self):
        from categorize import categorize_post
        result = categorize_post("Tesla stock TSLA is going to the moon")
        assert "TSLA" in result["mentioned_tickers"]
        assert "SPECIFIC_TICKER" in result["categories"]

    def test_empty_text(self):
        from categorize import categorize_post
        result = categorize_post("")
        assert result["categories"] == []

    def test_sentiment_positive(self):
        from categorize import categorize_post
        result = categorize_post("This is great, amazing, incredible news!")
        assert result["sentiment"] > 0

    def test_sentiment_negative(self):
        from categorize import categorize_post
        result = categorize_post("This is terrible, worst disaster ever")
        assert result["sentiment"] < 0

    def test_no_category_noise(self):
        from categorize import categorize_post
        result = categorize_post("The weather is nice today in Washington")
        assert result["categories"] == []
