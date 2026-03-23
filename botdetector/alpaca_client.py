"""
Thin wrapper around Alpaca REST + WebSocket.
Isolates all Alpaca-specific API calls.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from .config import BotDetectorConfig

logger = logging.getLogger("botdetector.alpaca")


class AlpacaRESTClient:
    """REST API for account info, orders, positions."""

    def __init__(self, config: BotDetectorConfig):
        self.config = config
        self.client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper_mode,
        )
        self.data_client = StockHistoricalDataClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
        )

    def get_account(self) -> dict:
        """Get account info (buying power, equity, etc.)."""
        acct = self.client.get_account()
        return {
            "equity": float(acct.equity),
            "buying_power": float(acct.buying_power),
            "cash": float(acct.cash),
            "portfolio_value": float(acct.portfolio_value),
            "pattern_day_trader": acct.pattern_day_trader,
        }

    def submit_market_order(self, ticker: str, qty: int,
                             side: str, time_in_force: str = "day") -> dict:
        """Submit a market order. Returns order dict."""
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC
        req = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=order_side,
            time_in_force=tif,
        )
        order = self.client.submit_order(req)
        return {
            "order_id": str(order.id),
            "status": str(order.status),
            "filled_qty": float(order.filled_qty or 0),
            "filled_avg_price": float(order.filled_avg_price or 0),
            "submitted_at": str(order.submitted_at),
        }

    def get_position(self, ticker: str) -> Optional[dict]:
        """Get current position for a ticker."""
        try:
            pos = self.client.get_open_position(ticker)
            return {
                "ticker": pos.symbol,
                "qty": int(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "unrealized_pl": float(pos.unrealized_pl),
                "market_value": float(pos.market_value),
            }
        except Exception:
            return None

    def close_position(self, ticker: str) -> dict:
        """Close entire position for a ticker."""
        order = self.client.close_position(ticker)
        return {"order_id": str(order.id), "status": str(order.status)}

    def get_bars(self, ticker: str, timeframe: str = "1Min",
                  start: str = None, end: str = None, limit: int = 1000) -> list[dict]:
        """Fetch historical bars for backtesting."""
        tf = TimeFrame.Minute if timeframe == "1Min" else TimeFrame.Day
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
        )
        bars = self.data_client.get_stock_bars(req)
        keys = list(bars.data.keys()) if bars.data else []
        bar_list = bars[ticker] if ticker in bars.data else (bars.data.get(keys[0], []) if keys else [])
        return [
            {
                "timestamp": str(bar.timestamp),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            }
            for bar in bar_list
        ]


class AlpacaWSClient:
    """
    WebSocket client for real-time trades and quotes.

    Connects to Alpaca IEX data stream. Calls registered
    handlers on each trade/quote message.
    """

    def __init__(self, config: BotDetectorConfig):
        self.config = config
        self._ws = None
        self._on_trade: Optional[Callable] = None
        self._on_quote: Optional[Callable] = None
        self._running = False

    def set_handlers(self, on_trade: Callable, on_quote: Callable):
        """
        Register callbacks.
        on_trade(ticker: str, price: float, size: int, timestamp: datetime)
        on_quote(ticker: str, bid: float, ask: float, timestamp: datetime)
        """
        self._on_trade = on_trade
        self._on_quote = on_quote

    async def connect_and_stream(self, tickers: list[str]):
        """
        Connect to Alpaca WebSocket, authenticate, subscribe, and stream.
        Reconnects on disconnect with exponential backoff.
        """
        self._running = True
        backoff = 1

        while self._running:
            try:
                async with websockets.connect(self.config.alpaca_data_ws) as ws:
                    self._ws = ws
                    # Read welcome
                    await ws.recv()

                    # Authenticate
                    auth_msg = {
                        "action": "auth",
                        "key": self.config.alpaca_api_key,
                        "secret": self.config.alpaca_secret_key,
                    }
                    await ws.send(json.dumps(auth_msg))
                    auth_resp = await ws.recv()
                    logger.info(f"Auth response: {auth_resp}")

                    # Subscribe to trades and quotes
                    sub_msg = {
                        "action": "subscribe",
                        "trades": tickers,
                        "quotes": tickers,
                    }
                    await ws.send(json.dumps(sub_msg))
                    sub_resp = await ws.recv()
                    logger.info(f"Subscribe response: {sub_resp}")

                    backoff = 1  # Reset backoff on successful connect

                    # Stream loop
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        msgs = json.loads(raw_msg)
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        for msg in msgs:
                            self._dispatch(msg)

            except (websockets.ConnectionClosed, ConnectionError) as e:
                if not self._running:
                    break
                logger.warning(f"WebSocket disconnected: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                if not self._running:
                    break
                logger.error(f"WebSocket error: {e}", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _dispatch(self, msg: dict):
        """Route a message to the appropriate handler."""
        msg_type = msg.get("T")
        if msg_type == "t" and self._on_trade:
            # Trade message
            ts = datetime.fromisoformat(msg["t"].replace("Z", "+00:00"))
            self._on_trade(
                ticker=msg["S"],
                price=float(msg["p"]),
                size=int(msg["s"]),
                timestamp=ts,
            )
        elif msg_type == "q" and self._on_quote:
            # Quote message
            ts = datetime.fromisoformat(msg["t"].replace("Z", "+00:00"))
            self._on_quote(
                ticker=msg["S"],
                bid=float(msg["bp"]),
                ask=float(msg["ap"]),
                timestamp=ts,
            )

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
