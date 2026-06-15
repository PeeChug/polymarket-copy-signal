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

import os


def notify_trade_opened(trade: dict, cfg) -> None:
    """
    Fire a Telegram alert when a new green/blue overlap paper trade opens — but
    ONLY if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set (add them as Actions
    secrets to enable). With no secrets this stays a pure no-op. Must never raise
    into the engine.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        import requests
        tier = trade.get("tier_at_entry") or "?"
        emoji = "🟢" if tier == "green" else "🔵"
        entry = trade.get("entry_price")
        msg = (f"{emoji} New {tier} consensus paper trade\n"
               f"{trade.get('title')} [{trade.get('outcome')}]\n"
               f"entry {entry:.3f} · {trade.get('overlap_at_entry')} of the cohort agree"
               if isinstance(entry, (int, float)) else f"{emoji} New {tier} consensus signal")
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg, "disable_web_page_preview": True},
                      timeout=10)
    except Exception:
        pass  # alerts must never break the poller
