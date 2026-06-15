"""
Alert seam — INTENTIONALLY NOT IMPLEMENTED YET.

Out of scope for the initial read-only logger/tester, but this is the clean
hook where a phone/Telegram/Slack alert would later fire when a new green/blue
overlap paper trade opens. The engine calls `notify_trade_opened(trade, cfg)`
exactly once per newly opened overlap trade.

To wire up Telegram later (sketch — do NOT enable without adding the secrets):

    import os, requests
    def notify_trade_opened(trade, cfg):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat  = os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return
        msg = (f"🟢 New {trade['tier_at_entry']} signal: {trade['title']} "
               f"[{trade['outcome']}] @ {trade['entry_price']:.2f} "
               f"(overlap {trade['overlap_at_entry']})")
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=10)

Keeping it a no-op means the poller has zero extra dependencies or secrets
today, and adding alerts is a one-function change here.
"""

from __future__ import annotations


def notify_trade_opened(trade: dict, cfg) -> None:
    """No-op seam. See module docstring. Must never raise into the engine."""
    return None
