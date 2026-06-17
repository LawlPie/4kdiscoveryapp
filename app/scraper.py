"""
The scraper worker.

Fetches Platekompaniet's "4K Ultra HD" category pages, walks pagination, and
extracts the fields we care about. It is built on `httpx` + `BeautifulSoup`,
which is lightweight and polite — we send realistic browser headers, throttle
between requests, and retry transient failures with backoff.

Because retailer markup changes over time, the parsing logic is intentionally
defensive: it tries several selector strategies and tolerates missing fields
rather than crashing the whole run. The CSS selectors live in `SELECTORS`
below so they can be tuned in one place if the site is restructured.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from .config import settings
from .database import db_session, upsert_product
from .notifications import notify_price_change

logger = logging.getLogger("scraper")


# --------------------------------------------------------------------------- #
# Selector configuration — adjust here if Platekompaniet changes its markup.
# Each entry is a list of candidate CSS selectors tried in order.
# --------------------------------------------------------------------------- #
SELECTORS: dict[str, list[str]] = {
    # A single product "card" in the listing grid.
    "product_card": [
        "article.product-item",
        "li.product-item",
        "div.product-item",
        "div.product-card",
        "[data-product-id]",
    ],
    # The anchor that links to the product detail page (also gives us the slug).
    "product_link": ["a.product-item-link", "a.product-link", "a[href*='/']"],
    "title": [".product-item-name", ".product-name", "h2", "h3", ".name"],
    "image": ["img"],
    # Current (possibly discounted) price.
    "price": [
        "[data-price-type='finalPrice'] .price",
        ".special-price .price",
        ".price-final_price .price",
        ".price",
    ],
    # Original price shown struck-through when on sale.
    "old_price": [
        "[data-price-type='oldPrice'] .price",
        ".old-price .price",
        ".price-was",
        "del .price",
        "del",
    ],
    # Campaign / promo badges, e.g. "Kjøp 2, få 30%".
    "campaign": [".campaign", ".promo", ".badge", ".product-label", ".label"],
    "stock": [".stock", ".availability", ".product-stock"],
    # The "next page" pagination link.
    "next_page": [
        "a.action.next",
        "a[rel='next']",
        "li.pages-item-next a",
        ".pagination a.next",
    ],
}

# Phrases that confirm an item is genuinely a 4K Ultra HD release. We match
# loosely (case-insensitive) against the card text to filter out plain Blu-ray.
FOURK_MARKERS = ("4k ultra hd", "4k uhd", "ultra hd", "4k blu", "4k-blu")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _default_headers() -> dict[str, str]:
    """Browser-like headers to reduce the chance of being blocked."""
    return {
        "User-Agent": settings.USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _fetch(client: httpx.Client, url: str) -> str | None:
    """GET a URL with retries and exponential backoff. Returns HTML or None."""
    for attempt in range(1, settings.SCRAPE_RETRIES + 1):
        try:
            resp = client.get(url, timeout=settings.SCRAPE_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            wait = settings.SCRAPE_DELAY_SECONDS * attempt
            logger.warning(
                "Fetch failed (%s/%s) for %s: %s — retrying in %.1fs",
                attempt, settings.SCRAPE_RETRIES, url, exc, wait,
            )
            time.sleep(wait)
    logger.error("Giving up on %s after %s attempts", url, settings.SCRAPE_RETRIES)
    return None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _select_first(node: Tag, keys: Iterable[str]) -> Tag | None:
    """Return the first element matching any of the candidate selectors."""
    for selector in keys:
        found = node.select_one(selector)
        if found is not None:
            return found
    return None


_PRICE_RE = re.compile(r"(\d[\d\s. ]*\d|\d)")


def _parse_price(text: str | None) -> float | None:
    """
    Turn a Norwegian price string into a float.

    Handles formats like "1 299,-", "kr 1.299,00", "299,90", "299" etc.
    """
    if not text:
        return None
    cleaned = text.replace(" ", " ").strip()
    # Norwegian uses "," as the decimal separator and "."/space for thousands.
    cleaned = cleaned.replace(".", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return round(float(match.group()), 2)
    except ValueError:
        return None


def _slug_from_url(url: str) -> str:
    """Derive a stable unique product id from its URL path."""
    path = urlparse(url).path.strip("/")
    slug = path.split("/")[-1] if path else url
    # Drop file extensions like ".html" if present.
    return re.sub(r"\.html?$", "", slug) or url


def _looks_like_4k(card: Tag, title: str) -> bool:
    """Heuristic: does this card represent a 4K Ultra HD release?"""
    haystack = f"{title} {card.get_text(' ', strip=True)}".lower()
    # Also consider explicit data attributes / class names some sites expose.
    attrs = " ".join(
        str(v) for v in card.attrs.values() if isinstance(v, (str, list))
    ).lower()
    haystack = f"{haystack} {attrs}"
    return any(marker in haystack for marker in FOURK_MARKERS)


def _extract_card(card: Tag) -> dict[str, Any] | None:
    """Parse a single product card into our item dict, or None if unusable."""
    link = _select_first(card, SELECTORS["product_link"])
    href = link.get("href") if link else None
    if not href:
        return None
    url = urljoin(settings.SITE_ROOT, href)

    title_el = _select_first(card, SELECTORS["title"])
    title = (
        title_el.get_text(strip=True)
        if title_el
        else (link.get("title") or link.get_text(strip=True))
    )
    title = (title or "").strip()
    if not title:
        return None

    # Filter strictly to 4K Ultra HD releases.
    if not _looks_like_4k(card, title):
        return None

    price_el = _select_first(card, SELECTORS["price"])
    current_price = _parse_price(price_el.get_text() if price_el else None)

    old_el = _select_first(card, SELECTORS["old_price"])
    original_price = _parse_price(old_el.get_text() if old_el else None)

    # Campaign tags — collect all matching badges, de-duplicated.
    tags: list[str] = []
    for selector in SELECTORS["campaign"]:
        for badge in card.select(selector):
            text = badge.get_text(" ", strip=True)
            if text and text not in tags:
                tags.append(text)

    stock_el = _select_first(card, SELECTORS["stock"])
    stock_status = stock_el.get_text(" ", strip=True) if stock_el else None

    img_el = _select_first(card, SELECTORS["image"])
    image_url = None
    if img_el is not None:
        raw_img = (
            img_el.get("src")
            or img_el.get("data-src")
            or img_el.get("data-original")
        )
        if raw_img:
            image_url = urljoin(settings.SITE_ROOT, raw_img)

    # Determine sale state + discount percentage.
    on_sale = False
    discount_pct = 0.0
    if original_price and current_price and original_price > current_price > 0:
        on_sale = True
        discount_pct = round((original_price - current_price) / original_price * 100, 1)
    if tags:
        on_sale = True  # an active campaign counts as "on sale" for the UI

    return {
        "product_id": _slug_from_url(url),
        "title": title,
        "url": url,
        "image_url": image_url,
        "current_price": current_price,
        "original_price": original_price,
        "discount_pct": discount_pct,
        "on_sale": on_sale,
        "campaign_tags": tags,
        "stock_status": stock_status,
    }


def _find_next_page(soup: BeautifulSoup, current_url: str) -> str | None:
    """Locate the pagination 'next' link, returning an absolute URL or None."""
    next_el = _select_first(soup, SELECTORS["next_page"])
    if next_el and next_el.get("href"):
        return urljoin(current_url, next_el["href"])
    return None


def parse_listing(html: str, page_url: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a category page; return (items, next_page_url)."""
    soup = BeautifulSoup(html, "html.parser")

    cards: list[Tag] = []
    for selector in SELECTORS["product_card"]:
        cards = soup.select(selector)
        if cards:
            break

    items: list[dict[str, Any]] = []
    for card in cards:
        try:
            item = _extract_card(card)
            if item:
                items.append(item)
        except Exception as exc:  # never let one bad card kill the run
            logger.debug("Failed to parse a card: %s", exc)

    next_url = _find_next_page(soup, page_url)
    return items, next_url


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_scrape() -> dict[str, Any]:
    """
    Execute one full scrape: crawl pagination, upsert everything, and fire
    notifications for any favourited items that improved. Returns a summary.
    """
    logger.info("Starting scrape at %s", settings.SCRAPER_BASE_URL)
    start = time.time()

    all_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    url: str | None = settings.SCRAPER_BASE_URL
    page = 0

    with httpx.Client(
        headers=_default_headers(), follow_redirects=True
    ) as client:
        while url and page < settings.SCRAPE_MAX_PAGES:
            if url in seen_urls:  # guard against pagination loops
                break
            seen_urls.add(url)
            page += 1

            html = _fetch(client, url)
            if html is None:
                break

            items, next_url = parse_listing(html, url)
            logger.info("Page %s: found %s 4K item(s)", page, len(items))
            all_items.extend(items)

            url = next_url
            if url:
                time.sleep(settings.SCRAPE_DELAY_SECONDS)

    # De-duplicate by product_id (an item can appear on overlapping pages).
    unique: dict[str, dict[str, Any]] = {}
    for item in all_items:
        unique[item["product_id"]] = item

    summary = _persist(list(unique.values()))
    summary["duration_seconds"] = round(time.time() - start, 1)
    summary["pages"] = page
    logger.info(
        "Scrape finished: %s products, %s new, %s notified in %.1fs",
        summary["total"], summary["new"], summary["notified"],
        summary["duration_seconds"],
    )
    return summary


def _persist(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert all items in one transaction and dispatch notifications."""
    from .database import get_favorite_ids

    favorites = get_favorite_ids()
    new_count = 0
    notified = 0
    changes: list[dict[str, Any]] = []

    with db_session() as conn:
        for item in items:
            result = upsert_product(conn, item)
            if result["is_new"]:
                new_count += 1
            # Decide whether a watched item warrants an alert.
            if item["product_id"] in favorites and not result["is_new"]:
                if _is_improvement(result):
                    changes.append(result)

    # Send notifications outside the DB transaction so a slow webhook does not
    # hold a write lock on SQLite.
    for change in changes:
        try:
            notify_price_change(change)
            notified += 1
        except Exception as exc:
            logger.warning("Notification failed: %s", exc)

    return {"total": len(items), "new": new_count, "notified": notified}


def _is_improvement(change: dict[str, Any]) -> bool:
    """A watched item 'improved' if it got cheaper or entered a campaign."""
    old_price = change["old_price"]
    new_price = change["new_price"]

    entered_campaign = (not change["old_on_sale"]) and change["new_on_sale"]

    dropped = (
        old_price is not None
        and new_price is not None
        and new_price < old_price
    )
    if dropped:
        drop_pct = (old_price - new_price) / old_price if old_price else 0
        if drop_pct < settings.NOTIFY_MIN_DROP_PCT:
            dropped = False

    return bool(dropped or entered_campaign)


if __name__ == "__main__":
    # Allow running an ad-hoc scrape from the command line:  python -m app.scraper
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .database import init_db

    init_db()
    print(run_scrape())
