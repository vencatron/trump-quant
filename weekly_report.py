"""
TrumpQuant Weekly Report
Called by cron every Sunday at 6pm PT.
Generates performance report, sends to Telegram, updates signal weights.
"""

import subprocess
import sys

from learning_engine import generate_weekly_report, calculate_signal_weights


def send_telegram(text):
    """Use openclaw to send a Telegram message."""
    try:
        result = subprocess.run(
            ["openclaw", "message", "send", "--to", "8387647137", "--channel", "telegram", "--message", text],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False


def main():
    # Generate weekly report
    report = generate_weekly_report()
    print(report)
    print()

    # Send to Telegram
    if send_telegram(report):
        print("Report sent to Telegram.")
    else:
        print("Failed to send report to Telegram.")

    # Update weights
    weights = calculate_signal_weights()
    print(f"Signal weights updated ({weights.get('_meta', {}).get('total_trades', 0)} total trades)")


if __name__ == "__main__":
    main()
