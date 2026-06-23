"""
iMusic scraper (second retailer, for price comparison).

iMusic (imusic.co) hard-blocks ordinary HTTP clients with HTTP 418 — the block
keys off the TLS/JA3 fingerprint, so realistic headers alone don't help. We use
`curl_cffi`, which performs the request with Chrome's actual TLS fingerprint,
and parse the resulting HTML with BeautifulSoup.

Listings are the 4K UHD exposure page, paginated with `?offset=`. Each product
URL embeds its EAN (`/movies/<EAN>/...`), which is how we later match the exact
same disc against Platekompaniet. Prices are shown in NOK, so they compare
directly — no currency conversion needed.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from .config import settings
from .database import db_session, invalidate_cache, upsert_product

logger = logging.getLogger("imusic")

_SITE_ROOT = "https://imusic.co"
_EAN_RE = re.compile(r"/movies/(\d{8,14})/")
_PRICE_RE = re.compile(r"[\d.,]+")


def _parse_price(text: str | None) -> float | None:
    """Parse an iMusic price like 'NOK 1,049' → 1049.0 (NOK, whole kroner)."""
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    raw = m.group(0)
    # iMusic uses a comma as the thousands separator (e.g. 1,049). Drop commas;
    # keep a dot as a decimal point if present.
    raw = raw.replace(",", "")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


def _page_url(offset: int) -> str:
    base = settings.IMUSIC_4K_URL
    if offset <= 0:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}offset={offset}"


def _fetch(url: str) -> str | None:
    """GET a page via curl_cffi (Chrome TLS impersonation), with retries."""
    for attempt in range(1, settings.SCRAPE_RETRIES + 1):
        try:
            resp = cffi_requests.get(
                url,
                impersonate=settings.IMUSIC_IMPERSONATE,
                timeout=settings.SCRAPE_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                return resp.text
            logger.warning("iMusic %s returned HTTP %s", url, resp.status_code)
        except Exception as exc:  # curl_cffi raises its own error types
            logger.warning(
                "iMusic fetch failed (%s/%s) for %s: %s",
                attempt, settings.SCRAPE_RETRIES, url, exc,
            )
        time.sleep(settings.SCRAPE_DELAY_SECONDS * attempt)
    logger.error("Giving up on iMusic page %s", url)
    return None


def _parse_listing(html: str) -> list[dict[str, Any]]:
    """Extract product items from one iMusic listing page."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []

    for item in soup.select(".list-item"):
        link = item.select_one("a[href*='/movies/']")
        if not link:
            continue
        href = link.get("href", "")
        ean_m = _EAN_RE.search(href)
        if not ean_m:
            continue
        ean = ean_m.group(1)

        title = (link.get("title") or link.get_text(" ", strip=True) or "").strip()
        if not title:
            continue

        price_el = item.select_one(".price")
        current_price = _parse_price(price_el.get_text() if price_el else None)

        img = item.select_one("img")
        image_url = None
        if img is not None:
            raw = img.get("src") or img.get("data-src")
            if raw and not raw.endswith("missing-tall.png"):
                image_url = raw

        # Availability: look for a pre-order / sold-out hint among the buttons.
        stock_status = None
        btn_text = item.get_text(" ", strip=True).lower()
        if "pre-order" in btn_text or "preorder" in btn_text:
            stock_status = "Pre-order"
        elif "sold out" in btn_text or "not available" in btn_text:
            stock_status = "Sold out"
        elif current_price is not None:
            stock_status = "In stock"

        items.append(
            {
                "product_id": f"im:{ean}",
                "title": title,
                "url": urljoin(_SITE_ROOT, href),
                "image_url": image_url,
                "current_price": current_price,
                "original_price": None,
                "discount_pct": 0.0,
                "on_sale": False,
                "campaign_tags": [],
                "stock_status": stock_status,
                "product_family": None,
                "edition": None,
                "group_key": f"im:{ean}",
                "retailer": "imusic",
                "ean": ean,
            }
        )
    return items


def run_imusic_scrape() -> dict[str, Any]:
    """Crawl the iMusic 4K listing (paginated) and upsert all items."""
    if not settings.SCRAPE_IMUSIC:
        logger.info("iMusic scraping disabled (SCRAPE_IMUSIC=false).")
        return {"total": 0, "new": 0, "pages": 0, "disabled": True}

    logger.info("Starting iMusic scrape at %s", settings.IMUSIC_4K_URL)
    start = time.time()
    items: dict[str, dict[str, Any]] = {}
    pages = 0

    for page in range(settings.IMUSIC_MAX_PAGES):
        offset = page * settings.IMUSIC_PAGE_SIZE
        html = _fetch(_page_url(offset))
        if html is None:
            break
        batch = _parse_listing(html)
        pages += 1
        logger.info("iMusic page %s (offset %s): %s items (collected %s)",
                    page + 1, offset, len(batch), len(items) + len(batch))
        if not batch:
            break
        for it in batch:
            items[it["product_id"]] = it
        # Last page reached when fewer than a full page of items is returned.
        if len(batch) < settings.IMUSIC_PAGE_SIZE:
            break
        time.sleep(settings.SCRAPE_DELAY_SECONDS)

    new_count = 0
    with db_session() as conn:
        for it in items.values():
            result = upsert_product(conn, it)
            if result["is_new"]:
                new_count += 1
    invalidate_cache()  # refresh cached stats so the iMusic count updates

    summary = {
        "total": len(items),
        "new": new_count,
        "pages": pages,
        "duration_seconds": round(time.time() - start, 1),
    }
    logger.info(
        "iMusic scrape finished: %s products, %s new, in %.1fs (%s pages)",
        summary["total"], summary["new"], summary["duration_seconds"], pages,
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .database import init_db

    init_db()
    print(run_imusic_scrape())
