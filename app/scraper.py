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
import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings
from .database import (
    db_session,
    get_favorites_alert_state,
    get_owned_ids,
    invalidate_cache,
    record_notified_price,
    upsert_product,
)
from .notifications import notify_deal

logger = logging.getLogger("scraper")

# Only pull back the attributes we actually use — keeps responses small/fast.
_ATTRIBUTES = [
    "name", "url", "image_url", "thumbnail_url",
    "price", "active_price", "pimcore", "campaigns",
    "stock_status", "custom_stock_status_plp", "in_stock",
    "objectID", "sku", "product_id",
    "product_family", "edition",  # for grouping editions of the same release
    "ean", "bestillingsnummer",   # barcode, for cross-retailer (iMusic) matching
    "product_collection",          # releasing label (Criterion, Arrow, …)
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


def _facet_filters() -> list[list[str]]:
    """The base facet filters applied to every query (4K, optionally on-offer)."""
    filters: list[list[str]] = [[settings.ALGOLIA_4K_FILTER]]
    if settings.SCRAPE_ONLY_ON_OFFER:
        filters.append(["pimcore.OnOffer:true"])
    return filters


def _search(client: httpx.Client, params: dict[str, Any]) -> dict[str, Any] | None:
    """POST one Algolia query with retries + backoff. Returns parsed JSON or None.

    `params` is a plain dict; we JSON-encode list/dict values and URL-encode the
    whole thing into Algolia's expected `{"params": "<query-string>"}` body.
    """
    encoded = urlencode(
        {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
            for k, v in params.items()
        }
    )
    body = {"params": encoded}
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
                "Algolia query failed (%s/%s): %s — retrying in %.1fs",
                attempt, settings.SCRAPE_RETRIES, exc, wait,
            )
            time.sleep(wait)
    logger.error("Giving up on query after %s attempts", settings.SCRAPE_RETRIES)
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

    # Edition grouping: Platekompaniet's product_family ties variants (steelbook,
    # imports, limited editions) of the same release together. The final
    # group_key is assigned in a post-pass (_assign_group_keys) which adds a
    # title-based fallback for the ~10% of titles that lack a family.
    family = hit.get("product_family")
    family = str(family) if family else None
    edition = (hit.get("edition") or "").strip() or None
    ean = hit.get("ean") or hit.get("bestillingsnummer")
    ean = str(ean).strip() if ean else None

    # Releasing label(s) — Algolia returns a string or a list. Normalise to list.
    raw_collection = hit.get("product_collection")
    if isinstance(raw_collection, str):
        labels = [raw_collection] if raw_collection else []
    elif isinstance(raw_collection, list):
        labels = [c for c in raw_collection if c]
    else:
        labels = []

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
        "product_family": family,
        "edition": edition,
        "group_key": f"fam:{family}" if family else f"id:{product_id}",
        "retailer": "platekompaniet",
        "ean": ean,
        "labels": labels,
        "norm_title": _norm_title(name),
    }


# Noise stripped from titles before deriving a fallback grouping key. We keep
# the year (so remakes stay separate) and Norwegian letters.
_TITLE_NOISE = re.compile(
    r"\b(4k ultra hd|ultra hd|blu-?ray|dvd|steelbook|"
    r"the criterion collection|criterion|anniversary edition|limited edition|"
    r"special edition|collector'?s edition|edition|uk import|usa? import|import)\b",
    re.IGNORECASE,
)
_NON_KEY = re.compile(r"[^a-z0-9æøå ]")


def _norm_title(title: str) -> str:
    """
    Conservative title normalisation used as a fallback grouping key.

    Punctuation (incl. brackets) becomes spaces, but digits are kept — so the
    film year in "The Mummy (1999)" survives and keeps remakes ("(2017)")
    separate. We only strip format/edition noise, never the year.
    """
    t = (title or "").lower()
    t = _TITLE_NOISE.sub(" ", t)
    t = _NON_KEY.sub(" ", t)   # "(2008)" -> " 2008 " (year preserved)
    return re.sub(r"\s+", " ", t).strip()


def _assign_group_keys(items: dict[str, dict[str, Any]]) -> None:
    """
    Finalise each item's group_key.

    Items with a product_family use `fam:<id>` (authoritative). For items
    *without* a family, fall back to the title: if exactly one family shares the
    same normalised title, attach to it; otherwise group such items together by
    title. Ambiguous titles (matching >1 family) never merge — keeping it safe.
    """
    title_to_families: dict[str, set[str]] = {}
    for it in items.values():
        if it.get("product_family"):
            title_to_families.setdefault(_norm_title(it["title"]), set()).add(
                it["product_family"]
            )

    for it in items.values():
        fam = it.get("product_family")
        if fam:
            it["group_key"] = f"fam:{fam}"
            continue
        norm = _norm_title(it["title"])
        families = title_to_families.get(norm)
        if families and len(families) == 1:
            it["group_key"] = f"fam:{next(iter(families))}"
        elif norm:
            it["group_key"] = f"title:{norm}"
        else:
            it["group_key"] = f"id:{it['product_id']}"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _collect_hits(hits: list[dict[str, Any]], items: dict[str, dict[str, Any]]) -> None:
    """Parse a batch of Algolia hits into the accumulating items dict."""
    for hit in hits:
        try:
            item = _parse_hit(hit)
            if item:
                items[item["product_id"]] = item
        except Exception as exc:  # never let one bad hit kill the run
            logger.debug("Failed to parse hit: %s", exc)


def _crawl_capped(client: httpx.Client) -> tuple[dict[str, dict[str, Any]], int]:
    """Fetch the top SCRAPE_MAX_ITEMS popular 4K items via normal pagination."""
    items: dict[str, dict[str, Any]] = {}
    queries = 0
    page = 0
    while page < settings.SCRAPE_MAX_PAGES:
        data = _search(client, {
            "query": "",
            "hitsPerPage": settings.SCRAPE_HITS_PER_PAGE,
            "page": page,
            "facetFilters": _facet_filters(),
            "attributesToRetrieve": _ATTRIBUTES,
        })
        queries += 1
        if data is None:
            break
        hits = data.get("hits", [])
        _collect_hits(hits, items)
        nb_pages = data.get("nbPages", 0)
        logger.info("Page %s/%s: %s hits (collected %s)", page + 1, nb_pages, len(hits), len(items))
        page += 1
        if page >= nb_pages or not hits:
            break
        if len(items) >= settings.SCRAPE_MAX_ITEMS:
            logger.info("Reached item cap (%s); stopping.", settings.SCRAPE_MAX_ITEMS)
            break
        time.sleep(settings.SCRAPE_DELAY_SECONDS)
    return items, queries


def _crawl_full(client: httpx.Client) -> tuple[dict[str, dict[str, Any]], int]:
    """
    Fetch the ENTIRE 4K catalogue by recursively bisecting the numeric
    `product_id` space. Any sub-range with ≤1000 hits is fetched in a single
    1000-result query; larger ranges are split in half. Empty ranges prune
    instantly, so this converges in a few dozen queries regardless of catalogue
    size — sidestepping Algolia's 1000-results-per-query ceiling entirely.
    """
    items: dict[str, dict[str, Any]] = {}
    attr = settings.SCRAPE_PARTITION_ATTR
    budget = [settings.SCRAPE_MAX_QUERIES]
    HITS_MAX = 1000  # Algolia's hard per-query result cap

    def visit(lo: int, hi: int) -> None:
        if lo > hi or budget[0] <= 0:
            if budget[0] <= 0:
                logger.warning("Query budget exhausted; catalogue may be partial.")
            return
        budget[0] -= 1
        data = _search(client, {
            "query": "",
            "hitsPerPage": HITS_MAX,
            "facetFilters": _facet_filters(),
            "numericFilters": [f"{attr}>={lo}", f"{attr}<={hi}"],
            "attributesToRetrieve": _ATTRIBUTES,
        })
        if data is None:
            return
        n = data.get("nbHits", 0)
        if n == 0:
            return
        # Small enough to fetch whole, or we can't split further → take it.
        if n <= HITS_MAX or lo >= hi:
            _collect_hits(data.get("hits", []), items)
            logger.info("range [%d, %d]: %d hits (collected %d)", lo, hi, n, len(items))
            time.sleep(settings.SCRAPE_DELAY_SECONDS)
            return
        mid = (lo + hi) // 2
        visit(lo, mid)
        visit(mid + 1, hi)

    visit(settings.SCRAPE_ID_MIN, settings.SCRAPE_ID_MAX)
    return items, settings.SCRAPE_MAX_QUERIES - budget[0]


def run_scrape() -> dict[str, Any]:
    """Pull 4K items from Algolia, upsert them, and fire notifications."""
    mode = "full catalogue" if settings.SCRAPE_FULL_CATALOGUE else "top-popular"
    logger.info(
        "Starting scrape via Algolia index '%s' (filter: %s, mode: %s)",
        settings.ALGOLIA_INDEX, settings.ALGOLIA_4K_FILTER, mode,
    )
    start = time.time()

    with httpx.Client(follow_redirects=True) as client:
        if settings.SCRAPE_FULL_CATALOGUE:
            items, queries = _crawl_full(client)
        else:
            items, queries = _crawl_capped(client)

    # Assign final grouping keys (with title-based fallback) across all items.
    _assign_group_keys(items)

    summary = _persist(list(items.values()))
    summary["duration_seconds"] = round(time.time() - start, 1)
    summary["queries"] = queries
    logger.info(
        "Scrape finished: %s products, %s new, %s skipped(owned), %s notified "
        "in %.1fs (%s queries)",
        summary["total"], summary["new"], summary["skipped_owned"],
        summary["notified"], summary["duration_seconds"], queries,
    )
    return summary


def _persist(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert all items in one transaction and dispatch deal notifications."""
    fav_alert = get_favorites_alert_state()  # favourited id -> last alerted price
    owned = get_owned_ids()
    new_count = 0
    skipped = 0

    # Candidate alerts: (product_id, product dict). Collected during the upsert
    # pass, sent afterwards so a slow webhook never holds the write lock.
    deals: list[tuple[str, dict[str, Any]]] = []

    with db_session() as conn:
        for item in items:
            # Items the user already owns are not tracked/updated.
            if item["product_id"] in owned:
                skipped += 1
                continue
            result = upsert_product(conn, item)
            if result["is_new"]:
                new_count += 1
            pid = item["product_id"]
            if pid in fav_alert and not result["is_new"]:
                product = result["product"]
                if _is_deal(product, fav_alert[pid]):
                    deals.append((pid, product))

    notified = _dispatch_deals(deals)
    invalidate_cache()  # so stats/tag dropdown reflect the fresh scrape

    return {
        "total": len(items),
        "new": new_count,
        "skipped_owned": skipped,
        "notified": notified,
    }


def _is_deal(product: dict[str, Any], last_notified_price: float | None) -> bool:
    """
    True when a watched item is genuinely worth an alert: actually discounted
    (current below its original price) by at least the configured threshold, and
    cheaper than the price we last alerted at (or never alerted before). This
    deliberately ignores campaign-tag churn that doesn't change the price.
    """
    current = product.get("current_price")
    original = product.get("original_price")
    if current is None or original is None or current >= original:
        return False
    if (original - current) / original < settings.NOTIFY_MIN_DROP_PCT:
        return False
    if last_notified_price is not None and current >= last_notified_price - 1e-9:
        return False
    return True


def _dispatch_deals(deals: list[tuple[str, dict[str, Any]]]) -> int:
    """
    Send up to NOTIFY_MAX_PER_RUN alerts (biggest discount first). Every deal —
    even those over the cap — records its price so we don't re-alert next run.
    """
    if not deals:
        return 0

    def discount(p: dict[str, Any]) -> float:
        o, c = p.get("original_price"), p.get("current_price")
        return (o - c) / o if o and c else 0.0

    deals.sort(key=lambda d: discount(d[1]), reverse=True)
    cap = settings.NOTIFY_MAX_PER_RUN
    notified = 0
    sent_prices: dict[str, float] = {}

    for idx, (pid, product) in enumerate(deals):
        price = product["current_price"]
        if idx < cap:
            try:
                if notify_deal(product):
                    notified += 1
            except Exception as exc:
                logger.warning("Notification failed for %s: %s", pid, exc)
        sent_prices[pid] = price  # record regardless, to avoid repeat alerts

    if len(deals) > cap:
        logger.info("Suppressed %s extra alerts (cap %s)", len(deals) - cap, cap)

    with db_session() as conn:
        for pid, price in sent_prices.items():
            record_notified_price(conn, pid, price)
    return notified


if __name__ == "__main__":
    # Allow running an ad-hoc scrape from the command line:  python -m app.scraper
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .database import init_db

    init_db()
    print(run_scrape())
