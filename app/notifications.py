"""
Notification dispatch.

Supports Discord (rich embeds) and Telegram (HTML messages). Which provider is
used is decided purely by configuration — if neither is configured, this module
becomes a no-op so the app runs fine without alerts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("notifications")

# Discord embed accent colour (a pleasant blue-purple).
_EMBED_COLOR = 0x5865F2


def _format_nok(value: float | None) -> str:
    """Render a NOK amount the Norwegian way, e.g. 1 299 kr."""
    if value is None:
        return "—"
    whole = f"{value:,.0f}".replace(",", " ")
    return f"{whole} kr"


def _build_message(product: dict[str, Any]) -> dict[str, str]:
    """Produce title/description/url strings for a discounted watched product."""
    title = product["title"]
    url = product["url"]
    current = product.get("current_price")
    original = product.get("original_price")

    lines: list[str] = []
    if original and current and current < original:
        saved = original - current
        pct = (saved / original) * 100 if original else 0
        lines.append(
            f"💰 **{_format_nok(current)}**  "
            f"(was {_format_nok(original)}, −{pct:.0f}%, save {_format_nok(saved)})"
        )
    elif current is not None:
        lines.append(f"💰 **{_format_nok(current)}**")

    tags = product.get("campaign_tags") or []
    if tags:
        lines.append("🏷️ " + " · ".join(tags))

    if product.get("stock_status"):
        lines.append(f"📦 {product['stock_status']}")

    return {
        "title": f"📀 {title}",
        "description": "\n".join(lines) or "On sale.",
        "url": url,
        "image": product.get("image_url") or "",
    }


# --------------------------------------------------------------------------- #
# Provider implementations
# --------------------------------------------------------------------------- #
def _send_discord(msg: dict[str, str]) -> None:
    embed: dict[str, Any] = {
        "title": msg["title"],
        "description": msg["description"],
        "url": msg["url"],
        "color": _EMBED_COLOR,
        "footer": {"text": "4K Discovery · Platekompaniet tracker"},
    }
    if msg.get("image"):
        embed["thumbnail"] = {"url": msg["image"]}

    payload = {"username": "4K Discovery", "embeds": [embed]}
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(settings.DISCORD_WEBHOOK_URL, json=payload)
        resp.raise_for_status()


def _send_telegram(msg: dict[str, str]) -> None:
    # Telegram supports a small subset of HTML; convert **bold** markdown to <b>.
    body = msg["description"].replace("**", "")  # strip markdown asterisks
    text = (
        f"<b>{msg['title']}</b>\n"
        f"{body}\n"
        f'<a href="{msg["url"]}">View on Platekompaniet →</a>'
    )
    api = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(api, json=payload)
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def notify_deal(product: dict[str, Any]) -> bool:
    """
    Send a rich notification that a favourited product is on sale / cheaper.
    Returns True if a message was dispatched.
    """
    if not settings.notifications_enabled:
        logger.debug("Notifications disabled; skipping %s", product.get("title"))
        return False

    msg = _build_message(product)
    if settings.discord_enabled:
        _send_discord(msg)
        return True
    if settings.telegram_enabled:
        _send_telegram(msg)
        return True
    return False


def send_test_notification() -> bool:
    """Fire a dummy notification so users can verify their webhook config."""
    fake_product = {
        "title": "Test Movie (4K Ultra HD)",
        "url": settings.SITE_ROOT,
        "image_url": "",
        "current_price": 199.0,
        "original_price": 299.0,
        "discount_pct": 33.4,
        "campaign_tags": ["Kjøp 2, få 30%"],
        "stock_status": "På lager",
    }
    return notify_deal(fake_product)
