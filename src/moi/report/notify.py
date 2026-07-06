"""Notifications (Telegram, best-effort). Silently no-ops when unconfigured."""

from __future__ import annotations

import httpx

from moi.config import get_settings
from moi.logging import get_logger

log = get_logger(__name__)


def send(text: str) -> bool:
    """Send a Telegram message; returns True on success, False otherwise."""
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        log.info("notify_skipped", reason="telegram not configured")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": s.telegram_chat_id, "text": text[:4000]},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("notify_failed", error=str(exc)[:120])
        return False
