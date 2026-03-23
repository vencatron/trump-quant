"""Fetch market data using yfinance for analysis tickers."""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "market_data")

TICKERS = ["SPY", "QQQ", "^DJI", "NVDA", "GME", "DJT", "TSLA", "META", "COIN", "GLD", "BTC-USD"]

# Friendly filenames for tickers with special characters
TICKER_FILENAMES = {
    "^DJI": "DJI",
    "BTC-USD": "BTC-USD",
}


def fetch_daily(ticker: str, period: str = "6mo") -> pd.DataFrame:
    """Fetch daily OHLCV data."""
    print(f"  Fetching daily data for {ticker}...")
    t = yf.Ticker(ticker)
    df = t.history(period=period, interval="1d")
    if df.empty:
        print(f"  WARNING: No daily data for {ticker}")
    return df


def fetch_hourly(ticker: str, period: str = "1mo") -> pd.DataFrame:
    """Fetch 1-hour OHLCV data (yfinance limits hourly to ~730 days max, 60d typical)."""
    print(f"  Fetching hourly data for {ticker}...")
    t = yf.Ticker(ticker)
    # yfinance allows max 730 days for hourly, but practically ~60d works best
    df = t.history(period="60d", interval="1h")
    if df.empty:
        print(f"  WARNING: No hourly data for {ticker}")
    return df


def save_data(df: pd.DataFrame, ticker: str, interval: str):
    """Save DataFrame to CSV."""
    fname = TICKER_FILENAMES.get(ticker, ticker)
    path = os.path.join(DATA_DIR, f"{fname}_{interval}.csv")
    df.to_csv(path)
    print(f"  Saved {len(df)} rows to {path}")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    print("Fetching market data...")

    for ticker in TICKERS:
        print(f"\n--- {ticker} ---")
        try:
            daily = fetch_daily(ticker)
            if not daily.empty:
                save_data(daily, ticker, "daily")
        except Exception as e:
            print(f"  ERROR (daily) {ticker}: {e}")

        try:
            hourly = fetch_hourly(ticker)
            if not hourly.empty:
                save_data(hourly, ticker, "hourly")
        except Exception as e:
            print(f"  ERROR (hourly) {ticker}: {e}")

    print("\nMarket data fetch complete.")


if __name__ == "__main__":
    main()
