"""Out-of-band Telegram messages (API retry previews, budget alerts). Sync; call from a thread."""

import logging

import httpx

from app.config import get_settings
from app.tools.http_retry import retry_connect
from app.util.telegram_text import message_parts

log = logging.getLogger("notify")


def telegram_send(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    s = get_settings()
    if not s.telegram_bot_token:
        return False
    base = (s.telegram_api_base_url or "https://api.telegram.org").rstrip("/")
    try:
        url = f"{base}/bot{s.telegram_bot_token}/sendMessage"
        parts = message_parts(text)
        for index, part in enumerate(parts):
            payload: dict = {"chat_id": chat_id, "text": part}
            if reply_markup and index == len(parts) - 1:
                payload["reply_markup"] = reply_markup
            resp = retry_connect(
                lambda: httpx.post(url, json=payload, timeout=15, proxy=s.telegram_proxy_url or None)
            )
            resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # Never log the exception text: for HTTP errors it embeds the URL, which contains the token.
        log.warning("Telegram sendMessage failed (%s)", type(exc).__name__)
        return False
