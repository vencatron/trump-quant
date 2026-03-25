"""
Shared Alpaca API utilities for TrumpQuant.
Centralizes all Alpaca REST interactions: auth, pricing, positions, orders.
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger("trumpquant.alpaca_utils")

# --- Configuration ---
ALPACA_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")

# Warn at import time if keys are missing
if not ALPACA_KEY or not ALPACA_SECRET:
    logger.warning("WARNING: Alpaca API keys not set in environment variables. Trading will fail.")


def get_headers() -> dict:
    """Return Alpaca API authentication headers."""
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def get_price(ticker: str, timeout: int = 10) -> float | None:
    """Get latest mid-price for a ticker from Alpaca market data.
    
    Args:
        ticker: Stock symbol (e.g. 'SPY')
        timeout: Request timeout in seconds
        
    Returns:
        Mid-price as float, or None if unavailable.
    """
    try:
        url = f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/quotes/latest"
        resp = requests.get(url, headers=get_headers(), timeout=timeout)
        if resp.status_code == 200:
            quote = resp.json().get("quote", {})
            ask = quote.get("ap", 0)
            bid = quote.get("bp", 0)
            mid = (ask + bid) / 2
            if mid > 0:
                return round(mid, 2)
    except Exception as e:
        logger.error("Price fetch error for %s: %s", ticker, e)
    return None


def get_positions() -> dict:
    """Get all open Alpaca positions as {ticker: position_dict}.
    
    Returns:
        Dict mapping ticker symbols to their position data.
    """
    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/positions",
            headers=get_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            positions = resp.json()
            if isinstance(positions, list):
                return {p["symbol"]: p for p in positions}
    except Exception as e:
        logger.error("Failed to fetch positions: %s", e)
    return {}


def get_positions_list() -> list:
    """Get all open Alpaca positions as a list.
    
    Returns:
        List of position dicts, or empty list on error.
    """
    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/positions",
            headers=get_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            result = resp.json()
            return result if isinstance(result, list) else []
    except Exception as e:
        logger.error("Failed to fetch positions: %s", e)
    return []


def get_total_exposure() -> float:
    """Get total dollar exposure across all open positions.
    
    Returns:
        Total absolute market value of all positions.
    """
    positions = get_positions_list()
    return sum(abs(float(p.get("market_value", 0))) for p in positions)


def submit_order(ticker: str, qty: int, side: str = "buy", retries: int = 2, timeout: int = 15) -> dict | None:
    """Submit a market order to Alpaca with retry logic.
    
    Args:
        ticker: Stock symbol
        qty: Number of shares
        side: 'buy' or 'sell'
        retries: Number of retry attempts on failure
        timeout: Request timeout in seconds
        
    Returns:
        Order dict on success, None on failure.
    """
    url = f"{ALPACA_URL}/v2/orders"
    payload = {
        "symbol": ticker,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=get_headers(), timeout=timeout)
            if resp.status_code in (200, 201):
                order = resp.json()
                logger.info("ORDER SUBMITTED: %s %s %s — order_id=%s",
                           side.upper(), qty, ticker, order.get("id", "unknown"))
                return order
            else:
                logger.warning("ORDER FAILED (attempt %d/%d, HTTP %d): %s",
                             attempt + 1, retries + 1, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("ORDER ERROR (attempt %d/%d): %s", attempt + 1, retries + 1, e)
        
        if attempt < retries:
            time.sleep(1)  # Brief delay before retry
    
    return None


def close_position(ticker: str, timeout: int = 10) -> bool:
    """Close an open position by ticker.
    
    Args:
        ticker: Stock symbol to close
        timeout: Request timeout in seconds
        
    Returns:
        True if successfully closed, False otherwise.
    """
    try:
        resp = requests.delete(
            f"{ALPACA_URL}/v2/positions/{ticker}",
            headers=get_headers(),
            timeout=timeout,
        )
        if resp.status_code in (200, 204):
            logger.info("Position closed: %s", ticker)
            return True
        else:
            logger.warning("Failed to close %s: HTTP %d", ticker, resp.status_code)
            return False
    except Exception as e:
        logger.error("Failed to close %s: %s", ticker, e)
        return False


def check_connection() -> tuple[bool, str]:
    """Check if Alpaca API is reachable and credentials are valid.
    
    Returns:
        (success, message) tuple.
    """
    try:
        resp = requests.get(
            f"{ALPACA_URL}/v2/account",
            headers=get_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            account = resp.json()
            equity = account.get("equity", "?")
            return True, f"Connected — equity: ${equity}"
        elif resp.status_code == 403:
            return False, "Authentication failed — check API keys"
        else:
            return False, f"Unexpected status: {resp.status_code}"
    except Exception as e:
        return False, f"Connection error: {e}"
