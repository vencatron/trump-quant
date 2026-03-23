"""
CLI entry point for the bot detector.

Usage:
    python -m botdetector daemon          # Long-running WebSocket listener
    python -m botdetector arm --post-id X --text "..." --categories TARIFFS
    python -m botdetector backtest        # Run historical backtest
    python -m botdetector status          # Show current risk state
    python -m botdetector kill            # Activate kill switch
    python -m botdetector unkill          # Deactivate kill switch
"""

import argparse
import asyncio
import sys

from .config import BotDetectorConfig
from .bot_detector import BotDetector
from .backtest import Backtester
from .risk_manager import RiskManager
from .logger import setup_logging


def main():
    parser = argparse.ArgumentParser(description="TrumpQuant Bot Detector")
    sub = parser.add_subparsers(dest="command")

    # daemon
    sub.add_parser("daemon", help="Run as long-running WebSocket listener")

    # arm (one-shot)
    arm_p = sub.add_parser("arm", help="One-shot detection window")
    arm_p.add_argument("--post-id", required=True)
    arm_p.add_argument("--text", required=True)
    arm_p.add_argument("--categories", nargs="+", required=True)
    arm_p.add_argument("--tickers", nargs="+", default=None)

    # backtest
    bt_p = sub.add_parser("backtest", help="Run historical backtest")
    bt_p.add_argument("--posts-file", default=None)
    bt_p.add_argument("--output-dir", default=None)

    # status
    sub.add_parser("status", help="Show risk state and active trades")

    # kill / unkill
    kill_p = sub.add_parser("kill", help="Activate kill switch")
    kill_p.add_argument("--reason", default="Manual kill via CLI")
    sub.add_parser("unkill", help="Deactivate kill switch")

    args = parser.parse_args()
    setup_logging()
    config = BotDetectorConfig()

    if args.command == "daemon":
        detector = BotDetector(config)
        asyncio.run(detector.run_daemon())

    elif args.command == "arm":
        detector = BotDetector(config)
        asyncio.run(detector.run_oneshot(
            post_id=args.post_id,
            post_text=args.text,
            categories=args.categories,
            tickers=args.tickers,
        ))

    elif args.command == "backtest":
        bt = Backtester(config)
        bt.run(posts_file=args.posts_file, output_dir=args.output_dir)

    elif args.command == "status":
        rm = RiskManager(config)
        state = rm._state
        print(f"Date: {state.date}")
        print(f"Trades today: {state.trades_today}")
        print(f"Daily P&L: ${state.realized_pnl_today:+.2f}")
        print(f"Open positions: {len(state.open_positions)}")
        print(f"Halted: {state.halted} {state.halt_reason}")

    elif args.command == "kill":
        rm = RiskManager(config)
        rm.activate_kill_switch(args.reason)
        print("Kill switch ACTIVATED")

    elif args.command == "unkill":
        rm = RiskManager(config)
        rm.deactivate_kill_switch()
        print("Kill switch deactivated")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
