"""Telegram notifications for bot detector events."""

import logging
import subprocess
from .config import BotDetectorConfig
from .models import BotSignal, Trade

logger = logging.getLogger("botdetector.notifier")


class Notifier:

    def __init__(self, config: BotDetectorConfig = None):
        self.config = config or BotDetectorConfig()

    def _send(self, text: str):
        """Send via openclaw CLI (same pattern as signal_check.py)."""
        try:
            result = subprocess.run(
                [
                    "openclaw", "message", "send",
                    "--to", self.config.telegram_user_id,
                    "--channel", "telegram",
                    "--message", text,
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                logger.error(f"Telegram send failed: {result.stderr}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

    def send_trade_alert(self, signal: BotSignal, trade: Trade):
        direction_emoji = "\U0001f7e2" if signal.direction.value == "LONG" else "\U0001f534"
        paper = "\U0001f4dd PAPER" if trade.paper_mode else "\U0001f4b0 LIVE"

        text = (
            f"\U0001f916 *Bot Detector -- Trade Executed*\n\n"
            f"{paper}\n"
            f"\U0001f4f0 _{signal.post_text[:150]}_\n\n"
            f"\U0001f3f7 Category: `{', '.join(signal.post_categories)}`\n"
            f"{direction_emoji} {signal.direction.value} {trade.shares}x "
            f"*{signal.ticker}* @ ${trade.entry_price:.2f}\n"
            f"\U0001f4b5 Position: ${trade.position_value:.2f}\n\n"
            f"*Bot Signature:*\n"
            f"  \U0001f4ca Volume: {signal.volume_spike_ratio:.1f}x spike\n"
            f"  \u26a1 Velocity: {signal.price_velocity_pct:+.2f}%\n"
            f"  \U0001f4d0 Spread: +{signal.spread_widening_pct:.0f}%\n"
            f"  \u23f1 Detected {signal.seconds_after_post:.0f}s after post\n\n"
            f"\U0001f6d1 SL: ${trade.stop_loss_price:.2f} | "
            f"\U0001f3af TP: ${trade.take_profit_price:.2f}\n"
            f"\u23f0 Max hold: {self.config.max_hold_sec // 60} min"
        )
        self._send(text)

    def send_exit_alert(self, trade: Trade):
        pnl_emoji = "\u2705" if trade.realized_pnl >= 0 else "\u274c"

        text = (
            f"\U0001f916 *Bot Detector -- Position Closed*\n\n"
            f"{pnl_emoji} {trade.exit_reason.value}\n"
            f"*{trade.ticker}* {trade.direction.value}: "
            f"${trade.entry_price:.2f} -> ${trade.exit_price:.2f}\n"
            f"P&L: *${trade.realized_pnl:+.2f}* "
            f"({trade.realized_pnl_pct:+.2f}%)\n"
        )
        self._send(text)

    def send_blocked_alert(self, signal: BotSignal, reason: str):
        text = (
            f"\U0001f916 *Bot Detector -- Trade BLOCKED*\n\n"
            f"\u26a0\ufe0f {reason}\n"
            f"Signal: {signal.direction.value} {signal.ticker}\n"
            f"Post: _{signal.post_text[:120]}_"
        )
        self._send(text)
