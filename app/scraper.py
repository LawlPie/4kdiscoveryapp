"""
The scraper worker.

Platekompaniet's website is a client-side SPA: its category pages contain no
product HTML — the listing is rendered in the browser from **Algolia**, a hosted
search API. Scraping the raw HTML therefore yields nothing. Instead we query the
exact same public, search-only Algolia index the site's own frontend uses, which
returns clean, structured JSON (title, price, regular price, campaign, stock).

This is both more reliable and far gentler than HTML scraping: a handful of small
JSON requests per run, with realistic browser headers and a polite delay.

We filter strictly to "4K Ultra HD" via the `format_media` facet.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings
from .database import db_session, get_favorite_ids, get_owned_ids, upsert_product
from .notifications import notify_price_change

logger = logging.getLogger("scraper")

# Only pull back the attributes we actually use — keeps responses small/fast.
_ATTRIBUTES = [
    "name", "url", "image_url", "thumbnail_url",
    "price", "active_price", "pimcore", "campaigns",
    "stock_status", "custom_stock_status_plp", "in_stock",
    "objectID", "sku", "product_id",
]


def _algolia_url() -> str:
    return (
        f"https://{settings.ALGOLIA_APP_ID}-dsn.algolia.net"
        f"/1/indexes/{settings.ALGOLIA_INDEX}/query"
    )


def _headers() -> dict[str, str]:
    """Algolia auth headers + a realistic UA so we blend in with normal traffic."""
    return {
        "X-Algolia-Application-Id": settings.ALGOLIA_APP_ID,
        "X-Algolia-API-Key": settings.ALGOLIA_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": settings.USER_AGENT,
        "Accept": "application/json",
        "Origin": settings.SITE_ROOT,
        "Referer": settings.SITE_ROOT + "/",
    }


def _build_params(page: int) -> str:
    """Build the URL-encoded Algolia `params` string for one page of results."""
    facet_filters: list[list[str]] = [[settings.ALGOLIA_4K_FILTER]]
    if settings.SCRAPE_ONLY_ON_OFFER:
        facet_filters.append(["pimcore.OnOffer:true"])

    return urlencode(
        {
            "query": "",
            "hitsPerPage": settings.SCRAPE_HITS_PER_PAGE,
            "page": page,
            "facetFilters": json.dumps(facet_filters, ensure_ascii=False),
            "attributesToRetrieve": json.dumps(_ATTRIBUTES),
        }
    )


def _fetch_page(client: httpx.Client, page: int) -> dict[str, Any] | None:
    """POST one search page with retries + backoff. Returns parsed JSON or None."""
    body = {"params": _build_params(page)}
    for attempt in range(1, settings.SCRAPE_RETRIES + 1):
        try:
            resp = client.post(
                _algolia_url(),
                json=body,
                headers=_headers(),
                timeout=settings.SCRAPE_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            wait = settings.SCRAPE_DELAY_SECONDS * attempt
            logger.warning(
                "Algolia page %s failed (%s/%s): %s — retrying in %.1fs",
                page, attempt, settings.SCRAPE_RETRIES, exc, wait,
            )
            time.sleep(wait)
    logger.error("Giving up on page %s after %s attempts", page, settings.SCRAPE_RETRIES)
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _parse_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Algolia product hit into our normalised item dict."""
    name = (hit.get("name") or "").strip()
    url = hit.get("url")
    if not name or not url:
        return None

    pimcore = hit.get("pimcore") or {}
    nok = (hit.get("price") or {}).get("NOK") or {}

    # Current price: prefer the campaign-aware pimcore price, then active_price.
    current_price = (
        _to_float(pimcore.get("price"))
        or _to_float(hit.get("active_price"))
        or _to_float(nok.get("default"))
    )
    # Original/before price: the regular (pre-discount) price.
    original_price = (
        _to_float(pimcore.get("regularPrice")) or _to_float(nok.get("default"))
    )

    # Campaign tags — combine the human-readable campaign name + label, deduped.
    tags: list[str] = []
    for key in ("campaignName", "campaignLabel"):
        val = (pimcore.get(key) or "").strip()
        if val and val not in tags:
            tags.append(val)

    on_offer = bool(pimcore.get("OnOffer"))
    discount_pct = 0.0
    if original_price and current_price and original_price > current_price > 0:
        discount_pct = round((original_price - current_price) / original_price * 100, 1)
    on_sale = on_offer or discount_pct > 0 or bool(tags)

    product_id = str(hit.get("objectID") or hit.get("sku") or hit.get("product_id") or url)

    return {
        "product_id": product_id,
        "title": name,
        "url": url,
        "image_url": hit.get("image_url") or hit.get("thumbnail_url"),
        "current_price": current_price,
        "original_price": original_price,
        "discount_pct": discount_pct,
        "on_sale": on_sale,
        "campaign_tags": tags,
        "stock_status": hit.get("stock_status") or hit.get("custom_stock_status_plp"),
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_scrape() -> dict[str, Any]:
    """Pull all 4K items from Algolia, upsert them, and fire notifications."""
    logger.info(
        "Starting scrape via Algolia index '%s' (filter: %s)",
        settings.ALGOLIA_INDEX, settings.ALGOLIA_4K_FILTER,
    )
    start = time.time()

    items: dict[str, dict[str, Any]] = {}
    page = 0
    pages_fetched = 0

    with httpx.Client(follow_redirects=True) as client:
        while page < settings.SCRAPE_MAX_PAGES:
            data = _fetch_page(client, page)
            if data is None:
                break

            hits = data.get("hits", [])
            for hit in hits:
                try:
                    item = _parse_hit(hit)
                    if item:
                        items[item["product_id"]] = item
                except Exception as exc:  # never let one bad hit kill the run
                    logger.debug("Failed to parse hit: %s", exc)

            nb_pages = data.get("nbPages", 0)
            logger.info(
                "Page %s/%s: %s hits (collected %s)",
                page + 1, nb_pages, len(hits), len(items),
            )
            pages_fetched += 1

            page += 1
            if page >= nb_pages or not hits:
                break
            if len(items) >= settings.SCRAPE_MAX_ITEMS:
                logger.info("Reached item cap (%s); stopping.", settings.SCRAPE_MAX_ITEMS)
                break
            time.sleep(settings.SCRAPE_DELAY_SECONDS)

    summary = _persist(list(items.values()))
    summary["duration_seconds"] = round(time.time() - start, 1)
    summary["pages"] = pages_fetched
    logger.info(
        "Scrape finished: %s products, %s new, %s notified in %.1fs",
        summary["total"], summary["new"], summary["notified"],
        summary["duration_seconds"],
    )
    return summary


def _persist(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert all items in one transaction and dispatch notifications."""
    favorites = get_favorite_ids()
    owned = get_owned_ids()
    new_count = 0
    skipped = 0
    notified = 0
    changes: list[dict[str, Any]] = []

    with db_session() as conn:
        for item in items:
            # Items the user already owns are not tracked/updated.
            if item["product_id"] in owned:
                skipped += 1
                continue
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

    return {
        "total": len(items),
        "new": new_count,
        "skipped_owned": skipped,
        "notified": notified,
    }


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
