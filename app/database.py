"""
SQLite persistence layer.

We deliberately use the standard-library `sqlite3` module (no ORM) to keep the
container small and the data model transparent. Connections use
`sqlite3.Row` so rows behave like dictionaries in templates and JSON responses.

Schema
------
products       : the current state of every discovered 4K item.
price_history  : an append-only log of (product_id, price, date) snapshots.
watchlist      : which products the user has favourited (a heart).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

from .config import settings
from .labels import (
    CRITERION,
    boutique_labels_in,
    is_us_criterion_ean,
    region_from_ean,
    region_of,
)


# --------------------------------------------------------------------------- #
# Tiny TTL cache for cheap-but-frequent lookups (stats, campaign tags) that run
# on every page load and only change when a scrape writes new data.
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _cache[key] = (now, value)
    return value


def invalidate_cache() -> None:
    """Drop cached lookups (called after a scrape so the UI reflects new data)."""
    _cache.clear()


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #
def get_connection() -> sqlite3.Connection:
    """Open a new SQLite connection with sane defaults for a web app."""
    conn = sqlite3.connect(settings.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL gives us concurrent reads while the scraper writes.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Read/write speed tuning (safe with WAL): less fsync, in-memory temp tables,
    # a larger page cache and memory-mapped I/O.
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-16000;")   # ~16 MB page cache
    conn.execute("PRAGMA mmap_size=134217728;")  # 128 MB memory-mapped reads
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and rolls back on error."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _utcnow() -> str:
    """ISO-8601 UTC timestamp string used consistently across the schema."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Schema initialisation
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id      TEXT PRIMARY KEY,          -- unique slug / SKU from the site
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    image_url       TEXT,
    current_price   REAL,                      -- NOK
    original_price  REAL,                      -- NOK, before-sale price (nullable)
    discount_pct    REAL DEFAULT 0,            -- 0..100, derived convenience field
    on_sale         INTEGER DEFAULT 0,         -- 1 if discounted or campaign tagged
    campaign_tags   TEXT DEFAULT '[]',         -- JSON list of promo strings
    stock_status    TEXT,                      -- e.g. "På lager", "Utsolgt"
    product_family  TEXT,                      -- Platekompaniet's edition-group id
    edition         TEXT,                      -- e.g. "Steelbook Edition", "Limited Edition"
    group_key       TEXT,                      -- grouping key (fam:<id> or id:<id>)
    retailer        TEXT NOT NULL DEFAULT 'platekompaniet',  -- source store
    ean             TEXT,                      -- barcode, used to match across retailers
    labels          TEXT DEFAULT '[]',         -- JSON list of releasing labels (Criterion, Arrow…)
    norm_title      TEXT,                      -- normalised title (year-aware) for grouping
    match_key       TEXT,                      -- normalised title WITHOUT year, for cross-retailer film matching
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL,
    price           REAL,
    original_price  REAL,
    date            TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS watchlist (
    product_id      TEXT PRIMARY KEY,
    is_favorited    INTEGER NOT NULL DEFAULT 0,   -- ❤️ on the wishlist
    is_owned        INTEGER NOT NULL DEFAULT 0,   -- ✓ already in my collection
    last_notified_price REAL,                     -- price of the last alert sent
    created_at      TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id);
CREATE INDEX IF NOT EXISTS idx_products_onsale ON products(on_sale);
-- idx_products_group is created in _migrate(), after the group_key column exists
-- (so this script also succeeds on databases that predate that column).
"""


def init_db() -> None:
    """Create tables and indexes if they do not yet exist, then migrate."""
    with db_session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent, additive migrations for databases created before a feature."""
    # Add watchlist.is_owned to pre-existing databases (the "owned" collection).
    wl_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)")}
    if "is_owned" not in wl_cols:
        conn.execute(
            "ALTER TABLE watchlist ADD COLUMN is_owned INTEGER NOT NULL DEFAULT 0"
        )
    # Price at which we last notified for this watched item (alert de-duplication).
    if "last_notified_price" not in wl_cols:
        conn.execute("ALTER TABLE watchlist ADD COLUMN last_notified_price REAL")

    # Add edition-grouping columns to products (cheapest-of-duplicates feature).
    p_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
    for col in ("product_family", "edition", "group_key", "ean", "norm_title", "match_key"):
        if col not in p_cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
    # Releasing-label list (Criterion view / boutique alternatives).
    if "labels" not in p_cols:
        conn.execute("ALTER TABLE products ADD COLUMN labels TEXT DEFAULT '[]'")
    # Multi-retailer support (iMusic price comparison).
    if "retailer" not in p_cols:
        conn.execute(
            "ALTER TABLE products ADD COLUMN retailer TEXT NOT NULL "
            "DEFAULT 'platekompaniet'"
        )
    # Backfill a sane group_key for rows that predate the column; the next scrape
    # replaces it with the real product_family-based key.
    conn.execute(
        "UPDATE products SET group_key = 'id:' || product_id WHERE group_key IS NULL"
    )
    # Safe to create now that the columns are guaranteed to exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_group ON products(group_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_retailer ON products(retailer)")
    # Composite index for the iMusic price-comparison join (retailer + ean).
    # Without it the join scans all iMusic rows per product (~6s page loads).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_retailer_ean "
        "ON products(retailer, ean)"
    )
    # Cross-label / cross-retailer film matching for the Criterion collector view.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_norm_title ON products(norm_title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_match_key ON products(match_key)")


# --------------------------------------------------------------------------- #
# Row helpers
# --------------------------------------------------------------------------- #
def _row_to_product(row: sqlite3.Row) -> dict[str, Any]:
    """Normalise a product row into a JSON/template-friendly dict."""
    data = dict(row)
    # campaign_tags is stored as a JSON string; expose it as a real list.
    try:
        data["campaign_tags"] = json.loads(data.get("campaign_tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        data["campaign_tags"] = []
    data["is_favorited"] = bool(data.get("is_favorited"))
    data["is_owned"] = bool(data.get("is_owned"))
    try:
        data["labels"] = json.loads(data.get("labels") or "[]")
    except (json.JSONDecodeError, TypeError):
        data["labels"] = []
    return data


# --------------------------------------------------------------------------- #
# Upsert / write operations (used by the scraper)
# --------------------------------------------------------------------------- #
def upsert_product(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    """
    Insert or update a scraped product.

    Returns a dict describing the change so the caller can decide whether a
    notification is warranted:
        {
            "is_new": bool,
            "old_price": float | None,
            "new_price": float | None,
            "old_on_sale": bool,
            "new_on_sale": bool,
            "product": <product dict>,
        }
    """
    now = _utcnow()
    tags_json = json.dumps(item.get("campaign_tags", []), ensure_ascii=False)

    current_price = item.get("current_price")
    original_price = item.get("original_price")
    on_sale = 1 if item.get("on_sale") else 0
    discount_pct = item.get("discount_pct") or 0.0
    family = item.get("product_family")
    edition = item.get("edition")
    retailer = item.get("retailer", "platekompaniet")
    ean = item.get("ean")
    labels_json = json.dumps(item.get("labels", []), ensure_ascii=False)
    norm_title = item.get("norm_title")
    match_key = item.get("match_key")
    # Group editions of the same release together; fall back to a per-item group.
    group_key = item.get("group_key") or (
        f"fam:{family}" if family else f"id:{item['product_id']}"
    )

    existing = conn.execute(
        "SELECT * FROM products WHERE product_id = ?", (item["product_id"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO products (
                product_id, title, url, image_url, current_price, original_price,
                discount_pct, on_sale, campaign_tags, stock_status,
                product_family, edition, group_key, retailer, ean,
                labels, norm_title, match_key,
                first_seen, last_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["product_id"], item["title"], item["url"], item.get("image_url"),
                current_price, original_price, discount_pct, on_sale, tags_json,
                item.get("stock_status"), family, edition, group_key, retailer, ean,
                labels_json, norm_title, match_key,
                now, now, now,
            ),
        )
        _record_history(conn, item["product_id"], current_price, original_price, now)
        product = conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (item["product_id"],)
        ).fetchone()
        return {
            "is_new": True,
            "old_price": None,
            "new_price": current_price,
            "old_on_sale": False,
            "new_on_sale": bool(on_sale),
            "product": _row_to_product(product),
        }

    old_price = existing["current_price"]
    old_on_sale = bool(existing["on_sale"])

    conn.execute(
        """
        UPDATE products SET
            title = ?, url = ?, image_url = ?, current_price = ?, original_price = ?,
            discount_pct = ?, on_sale = ?, campaign_tags = ?, stock_status = ?,
            product_family = ?, edition = ?, group_key = ?, retailer = ?, ean = ?,
            labels = ?, norm_title = ?, match_key = ?,
            last_seen = ?, updated_at = ?
        WHERE product_id = ?
        """,
        (
            item["title"], item["url"], item.get("image_url"), current_price,
            original_price, discount_pct, on_sale, tags_json,
            item.get("stock_status"), family, edition, group_key, retailer, ean,
            labels_json, norm_title, match_key,
            now, now, item["product_id"],
        ),
    )

    # Only append to history when the price actually changes — keeps the log tidy.
    if old_price != current_price:
        _record_history(conn, item["product_id"], current_price, original_price, now)

    product = conn.execute(
        "SELECT * FROM products WHERE product_id = ?", (item["product_id"],)
    ).fetchone()
    return {
        "is_new": False,
        "old_price": old_price,
        "new_price": current_price,
        "old_on_sale": old_on_sale,
        "new_on_sale": bool(on_sale),
        "product": _row_to_product(product),
    }


def _record_history(
    conn: sqlite3.Connection,
    product_id: str,
    price: float | None,
    original_price: float | None,
    when: str,
) -> None:
    conn.execute(
        "INSERT INTO price_history (product_id, price, original_price, date) "
        "VALUES (?, ?, ?, ?)",
        (product_id, price, original_price, when),
    )


# --------------------------------------------------------------------------- #
# Read operations (used by the web UI / API)
# --------------------------------------------------------------------------- #
def _build_filters(
    *,
    only_on_sale: bool = False,
    only_favorites: bool = False,
    only_owned: bool = False,
    exclude_owned: bool = False,
    campaign: str | None = None,
    search: str | None = None,
    retailer: str | None = "platekompaniet",
) -> tuple[str, list[Any]]:
    """Build a shared WHERE clause (+params) reused by list/count queries.

    `retailer` defaults to Platekompaniet so the browsable views never show raw
    iMusic rows (those are used only as price-comparison data, matched by EAN).
    Pass retailer=None to span all retailers.
    """
    where: list[str] = []
    params: list[Any] = []

    if retailer:
        where.append("p.retailer = ?")
        params.append(retailer)
    if only_on_sale:
        where.append("p.on_sale = 1")
    if only_favorites:
        where.append("COALESCE(w.is_favorited, 0) = 1")
    if only_owned:
        where.append("COALESCE(w.is_owned, 0) = 1")
    if exclude_owned:
        # Hide items already in the collection from the deal/wishlist views.
        where.append("COALESCE(w.is_owned, 0) = 0")
    if campaign:
        # campaign_tags is JSON text; a LIKE match is good enough for filtering.
        where.append("p.campaign_tags LIKE ?")
        params.append(f"%{campaign}%")
    if search:
        where.append("p.title LIKE ? COLLATE NOCASE")
        params.append(f"%{search}%")

    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    return clause, params


def count_products(*, grouped: bool = False, **filters: Any) -> int:
    """
    Number of rows matching the filters (for pagination).
    When `grouped`, counts distinct edition-groups instead of individual items.
    """
    clause, params = _build_filters(**filters)
    select = "COUNT(DISTINCT p.group_key)" if grouped else "COUNT(*)"
    sql = (
        f"SELECT {select} AS c FROM products p "
        "LEFT JOIN watchlist w ON w.product_id = p.product_id " + clause
    )
    with db_session() as conn:
        return conn.execute(sql, params).fetchone()["c"]


def list_products(
    *,
    only_on_sale: bool = False,
    only_favorites: bool = False,
    only_owned: bool = False,
    exclude_owned: bool = False,
    campaign: str | None = None,
    search: str | None = None,
    sort: str = "discount",
    grouped: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Fetch products joined with their watchlist (favourite/owned) state.

    sort: "discount" | "price_asc" | "price_desc" | "title" | "recent"
    When `grouped`, returns one row per edition-group (the cheapest variant),
    plus a `variant_count` of how many editions matched. Pass `limit`/`offset`
    for pagination.
    """
    clause, params = _build_filters(
        only_on_sale=only_on_sale,
        only_favorites=only_favorites,
        only_owned=only_owned,
        exclude_owned=exclude_owned,
        campaign=campaign,
        search=search,
    )

    # Unprefixed columns so the same ORDER BY works for the plain and grouped SQL.
    # Price sorts use `effective_price` = the lower of Platekompaniet's price and
    # the matching iMusic offer, so "cheaper at iMusic" deals sort to the top.
    sort_clause = {
        "discount": "discount_pct DESC, effective_price ASC",
        "price_asc": "(effective_price IS NULL), effective_price ASC",
        "price_desc": "effective_price DESC",
        "title": "title COLLATE NOCASE ASC",
        "recent": "updated_at DESC",
    }.get(sort, "discount_pct DESC")

    # Match the same disc at iMusic by EAN and take the cheaper of the two prices.
    imusic_join = (
        "LEFT JOIN products im ON im.retailer = 'imusic' "
        "AND p.ean IS NOT NULL AND im.ean = p.ean "
    )
    effective_expr = (
        "CASE "
        "WHEN p.current_price IS NULL THEN im.current_price "
        "WHEN im.current_price IS NULL THEN p.current_price "
        "WHEN im.current_price < p.current_price THEN im.current_price "
        "ELSE p.current_price END AS effective_price"
    )

    if grouped:
        # Rank variants within each group by Platekompaniet price; keep the
        # cheapest as the representative (rn = 1) and expose how many editions
        # matched. Sorting then uses the representative's effective_price.
        sql = (
            "WITH base AS ("
            "  SELECT p.*, COALESCE(w.is_favorited, 0) AS is_favorited, "
            "         COALESCE(w.is_owned, 0) AS is_owned, "
            f"        {effective_expr} "
            "  FROM products p "
            "  LEFT JOIN watchlist w ON w.product_id = p.product_id "
            "  " + imusic_join
            + clause
            + "), ranked AS ("
            "  SELECT *, "
            "    COUNT(*) OVER (PARTITION BY group_key) AS variant_count, "
            "    ROW_NUMBER() OVER (PARTITION BY group_key "
            "      ORDER BY (current_price IS NULL), current_price ASC, product_id ASC) AS rn "
            "  FROM base "
            ") SELECT * FROM ranked WHERE rn = 1 "
            + f"ORDER BY {sort_clause}"
        )
    else:
        sql = (
            "SELECT p.*, COALESCE(w.is_favorited, 0) AS is_favorited, "
            "COALESCE(w.is_owned, 0) AS is_owned, "
            f"{effective_expr} "
            "FROM products p "
            "LEFT JOIN watchlist w ON w.product_id = p.product_id "
            + imusic_join
            + clause
            + f"ORDER BY {sort_clause}"
        )

    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]

    with db_session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_product(r) for r in rows]


def get_group_variants(group_key: str) -> list[dict[str, Any]]:
    """All editions in a group (cheapest first) with favourite/owned state."""
    sql = (
        "SELECT p.*, COALESCE(w.is_favorited, 0) AS is_favorited, "
        "COALESCE(w.is_owned, 0) AS is_owned "
        "FROM products p LEFT JOIN watchlist w ON w.product_id = p.product_id "
        "WHERE p.group_key = ? "
        "ORDER BY (p.current_price IS NULL), p.current_price ASC"
    )
    with db_session() as conn:
        rows = conn.execute(sql, (group_key,)).fetchall()
    return [_row_to_product(r) for r in rows]


def _better_rep(new: dict[str, Any], cur: dict[str, Any]) -> bool:
    """Prefer a representative edition that has cover art, then the cheaper one."""
    ni, ci = bool(new.get("image_url")), bool(cur.get("image_url"))
    if ni != ci:
        return ni and not ci
    np, cp = new.get("current_price"), cur.get("current_price")
    if np is None:
        return False
    if cp is None:
        return True
    return np < cp


def _edition_display(d: dict[str, Any], region: str | None, source: str,
                     inferred_label: str | None) -> tuple[str | None, str]:
    """Return (boutique_label, display_name) for an edition row."""
    boutique = boutique_labels_in(d["labels"])
    label = boutique[0] if boutique else inferred_label
    if CRITERION in boutique:
        display = f"Criterion ({region})" if region else "Criterion"
    elif label:
        display = label
    elif region:
        display = f"{region} edition"
    else:
        display = "Other edition"
    return label, display


def _film_editions(conn: sqlite3.Connection, match_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
    """
    Map match_key -> all 4K editions of that film across both retailers, each
    annotated with source, label, region (from barcode) and price. iMusic rows
    carry no label, so we infer one when the same barcode is sold (with a label)
    at Platekompaniet.
    """
    match_keys = [k for k in match_keys if k]
    if not match_keys:
        return {}
    placeholders = ",".join("?" * len(match_keys))
    rows = conn.execute(
        "SELECT product_id, title, url, current_price, original_price, discount_pct, "
        "image_url, labels, match_key, ean, retailer FROM products "
        f"WHERE match_key IN ({placeholders})",
        match_keys,
    ).fetchall()
    parsed = [_row_to_product(r) for r in rows]

    # Barcode -> boutique label, learned from Platekompaniet, to label iMusic rows.
    ean_to_label: dict[str, str] = {}
    for d in parsed:
        if d["retailer"] == "platekompaniet" and d["ean"]:
            bl = boutique_labels_in(d["labels"])
            if bl:
                ean_to_label[d["ean"]] = bl[0]

    by_title: dict[str, list[dict[str, Any]]] = {}
    for d in parsed:
        region = region_from_ean(d["ean"])
        source = "iMusic" if d["retailer"] == "imusic" else "Platekompaniet"
        inferred = ean_to_label.get(d["ean"]) if d["retailer"] == "imusic" else None
        label, display = _edition_display(d, region, source, inferred)
        by_title.setdefault(d["match_key"], []).append({
            "source": source,
            "label": label,
            "display": display,
            "region": region,
            "price": d["current_price"],
            "original_price": d["original_price"],
            "discount_pct": d["discount_pct"],
            "url": d["url"],
            "product_id": d["product_id"],
            "ean": d["ean"],
            "is_us_criterion": is_us_criterion_ean(d["ean"]),
            "is_criterion_uk": CRITERION in boutique_labels_in(d["labels"]) and region in ("UK", "EU"),
        })
    return by_title


def _alt_sort_key(a: dict[str, Any]) -> tuple:
    """UK first; UK Criterion ahead of UK boutique; then cheapest."""
    return (
        a["region"] != "UK",
        not a["is_criterion_uk"],
        a["price"] if a["price"] is not None else 1e9,
    )


def _criterion_spine() -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """The deduped US Criterion 4K films (one representative per film) + owned map."""
    with db_session() as conn:
        crit_rows = conn.execute(
            "SELECT p.*, COALESCE(w.is_favorited, 0) AS is_favorited, "
            "COALESCE(w.is_owned, 0) AS is_owned "
            "FROM products p LEFT JOIN watchlist w ON w.product_id = p.product_id "
            "WHERE p.retailer = 'platekompaniet' "
            "AND (p.ean LIKE '715515%' OR p.ean LIKE '0715515%') "
            "ORDER BY p.title COLLATE NOCASE ASC",
        ).fetchall()
    criterion = [_row_to_product(r) for r in crit_rows]
    films: dict[str, dict[str, Any]] = {}
    owned_film: dict[str, bool] = {}
    for p in criterion:
        key = p.get("match_key") or p["product_id"]
        owned_film[key] = owned_film.get(key, False) or p["is_owned"]
        if key not in films or _better_rep(p, films[key]):
            films[key] = p
    return list(films.values()), owned_film


def get_criterion_releases() -> list[dict[str, Any]]:
    """
    One entry per Criterion 4K film (US barcode 715515), with `alternatives` —
    same-film UK/boutique editions from BOTH retailers — sorted UK-first, plus
    `has_uk_alt`/`uk_alt` for the headline badge.
    """
    films, owned_film = _criterion_spine()
    keys = [p.get("match_key") for p in films if p.get("match_key")]
    with db_session() as conn:
        editions = _film_editions(conn, keys)

    for p in films:
        key = p.get("match_key") or p["product_id"]
        eds = editions.get(key, [])
        # Alternatives: not the US Criterion disc, and either a UK/EU edition or
        # something carrying a boutique label. Keep the cheapest per label+region.
        dedup: dict[tuple, dict[str, Any]] = {}
        for e in eds:
            if e["is_us_criterion"]:
                continue
            if e["region"] not in ("UK", "EU") and not e["label"]:
                continue
            k = (e["display"], e["region"], e["source"])
            cur = e["price"]
            exi = dedup[k]["price"] if k in dedup else None
            if k not in dedup or (cur is not None and (exi is None or cur < exi)):
                dedup[k] = e
        alts = sorted(dedup.values(), key=_alt_sort_key)
        uk_alts = [a for a in alts if a["region"] == "UK"]
        p["alternatives"] = alts
        p["has_uk_alt"] = bool(uk_alts)
        p["uk_alt"] = uk_alts[0] if uk_alts else None
        p["is_owned"] = owned_film.get(key, p["is_owned"])

        # Headline figures for the card (cheapest across every edition + store).
        priced = [e for e in eds if e["price"] is not None]
        cheapest = min(priced, key=lambda e: e["price"]) if priced else None
        p["edition_count"] = len(eds)
        p["from_price"] = cheapest["price"] if cheapest else p.get("current_price")
        p["from_original"] = cheapest["original_price"] if cheapest else None
        p["from_discount"] = cheapest["discount_pct"] if cheapest else 0
        im = [e["price"] for e in eds if e["source"] == "iMusic" and e["price"] is not None]
        p["imusic_price"] = min(im) if im else None
        p["year"] = _year_from_title(p["title"])
    films.sort(key=lambda p: p["title"].lower())
    return films


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _year_from_title(title: str) -> int | None:
    """Pull the film year out of a title like 'Mean Streets (1973) / …'."""
    m = _YEAR_RE.search(title or "")
    return int(m.group(0)) if m else None


def get_criterion_film(product_id: str) -> dict[str, Any] | None:
    """A single Criterion film with every known edition across both retailers."""
    product = get_product(product_id)
    if product is None:
        return None
    key = product.get("match_key")
    with db_session() as conn:
        editions = _film_editions(conn, [key] if key else [])
    eds = editions.get(key, [])
    eds.sort(key=lambda e: (e["price"] is None, e["price"] if e["price"] is not None else 1e9))
    cheapest = next((e for e in eds if e["price"] is not None), None)
    product["editions"] = eds
    product["cheapest_edition"] = cheapest
    return product


def get_offers_by_ean(eans: list[str], retailer: str = "imusic") -> dict[str, dict[str, Any]]:
    """Map EAN -> product offer from another retailer (for price comparison)."""
    eans = [e for e in {e for e in eans if e}]  # de-dupe, drop blanks
    if not eans:
        return {}
    placeholders = ",".join("?" * len(eans))
    sql = (
        f"SELECT * FROM products WHERE retailer = ? AND ean IN ({placeholders})"
    )
    with db_session() as conn:
        rows = conn.execute(sql, (retailer, *eans)).fetchall()
    # If a retailer somehow has duplicate EANs, keep the cheapest.
    offers: dict[str, dict[str, Any]] = {}
    for r in rows:
        o = _row_to_product(r)
        cur = offers.get(o["ean"])
        if cur is None or (
            o.get("current_price") is not None
            and (cur.get("current_price") is None or o["current_price"] < cur["current_price"])
        ):
            offers[o["ean"]] = o
    return offers


def get_product(product_id: str) -> dict[str, Any] | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT p.*, COALESCE(w.is_favorited, 0) AS is_favorited, "
            "COALESCE(w.is_owned, 0) AS is_owned "
            "FROM products p LEFT JOIN watchlist w ON w.product_id = p.product_id "
            "WHERE p.product_id = ?",
            (product_id,),
        ).fetchone()
    return _row_to_product(row) if row else None


def get_price_history(product_id: str) -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT price, original_price, date FROM price_history "
            "WHERE product_id = ? ORDER BY date ASC",
            (product_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_campaign_tags() -> list[str]:
    """Distinct campaign tags in the catalogue (cached — full scan otherwise)."""
    def _compute() -> list[str]:
        tags: set[str] = set()
        with db_session() as conn:
            rows = conn.execute(
                "SELECT campaign_tags FROM products "
                "WHERE campaign_tags != '[]' AND retailer = 'platekompaniet'"
            ).fetchall()
        for r in rows:
            try:
                for t in json.loads(r["campaign_tags"] or "[]"):
                    if t:
                        tags.add(t)
            except (json.JSONDecodeError, TypeError):
                continue
        return sorted(tags)

    return _cached("campaign_tags", ttl=300.0, producer=_compute)


def get_favorites_alert_state() -> dict[str, float | None]:
    """Map favourited product_id -> the price we last alerted at (or None)."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT product_id, last_notified_price FROM watchlist "
            "WHERE is_favorited = 1"
        ).fetchall()
    return {r["product_id"]: r["last_notified_price"] for r in rows}


def record_notified_price(conn: sqlite3.Connection, product_id: str, price: float) -> None:
    """Remember the price we just alerted at, to avoid repeat notifications."""
    conn.execute(
        "UPDATE watchlist SET last_notified_price = ? WHERE product_id = ?",
        (price, product_id),
    )


def get_favorite_ids() -> set[str]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT product_id FROM watchlist WHERE is_favorited = 1"
        ).fetchall()
    return {r["product_id"] for r in rows}


def set_favorite(product_id: str, favorited: bool) -> bool:
    """
    Toggle a product's favourite flag. Returns the new state.
    Favouriting clears the 'owned' flag — the two states are mutually exclusive
    (you wishlist what you don't own yet).
    """
    now = _utcnow()
    with db_session() as conn:
        if favorited:
            conn.execute(
                "INSERT INTO watchlist (product_id, is_favorited, is_owned, created_at) "
                "VALUES (?, 1, 0, ?) "
                "ON CONFLICT(product_id) DO UPDATE SET is_favorited = 1, is_owned = 0",
                (product_id, now),
            )
        else:
            conn.execute(
                "UPDATE watchlist SET is_favorited = 0 WHERE product_id = ?",
                (product_id,),
            )
    return favorited


def toggle_favorite(product_id: str) -> bool:
    """Flip the favourite flag and return the resulting state."""
    favs = get_favorite_ids()
    return set_favorite(product_id, product_id not in favs)


# --------------------------------------------------------------------------- #
# Owned collection
# --------------------------------------------------------------------------- #
def get_owned_ids() -> set[str]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT product_id FROM watchlist WHERE is_owned = 1"
        ).fetchall()
    return {r["product_id"] for r in rows}


def set_owned(product_id: str, owned: bool) -> bool:
    """
    Toggle a product's owned flag. Returns the new state.
    Marking owned clears the favourite flag (mutually exclusive). Owned items
    are hidden from the deal/wishlist views and skipped by the scraper.
    """
    now = _utcnow()
    with db_session() as conn:
        if owned:
            conn.execute(
                "INSERT INTO watchlist (product_id, is_favorited, is_owned, created_at) "
                "VALUES (?, 0, 1, ?) "
                "ON CONFLICT(product_id) DO UPDATE SET is_owned = 1, is_favorited = 0",
                (product_id, now),
            )
        else:
            conn.execute(
                "UPDATE watchlist SET is_owned = 0 WHERE product_id = ?",
                (product_id,),
            )
    return owned


def toggle_owned(product_id: str) -> bool:
    """Flip the owned flag and return the resulting state."""
    owned = get_owned_ids()
    return set_owned(product_id, product_id not in owned)


def toggle_owned_group(product_id: str) -> bool | None:
    """
    Flip the owned flag for EVERY Platekompaniet edition that shares this
    product's group (the same editions collapsed into one card on the Trawler),
    so collecting a film marks all its editions at once. Returns the new state,
    or None if the product is missing.
    """
    product = get_product(product_id)
    if product is None:
        return None
    group_key = product.get("group_key")
    new_state = not bool(product.get("is_owned"))
    now = _utcnow()
    with db_session() as conn:
        if group_key:
            ids = [
                r["product_id"]
                for r in conn.execute(
                    "SELECT product_id FROM products "
                    "WHERE group_key = ? AND retailer = 'platekompaniet'",
                    (group_key,),
                )
            ]
        else:
            ids = [product_id]
        for pid in ids:
            if new_state:
                conn.execute(
                    "INSERT INTO watchlist (product_id, is_favorited, is_owned, created_at) "
                    "VALUES (?, 0, 1, ?) "
                    "ON CONFLICT(product_id) DO UPDATE SET is_owned = 1, is_favorited = 0",
                    (pid, now),
                )
            else:
                conn.execute(
                    "UPDATE watchlist SET is_owned = 0 WHERE product_id = ?", (pid,)
                )
    return new_state


def get_stats() -> dict[str, Any]:
    """Small dashboard summary used in the header (cached briefly)."""
    return _cached("stats", ttl=30.0, producer=_compute_stats)


def _compute_stats() -> dict[str, Any]:
    with db_session() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE retailer = 'platekompaniet'"
        ).fetchone()["c"]
        on_sale = conn.execute(
            "SELECT COUNT(*) AS c FROM products "
            "WHERE on_sale = 1 AND retailer = 'platekompaniet'"
        ).fetchone()["c"]
        imusic = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE retailer = 'imusic'"
        ).fetchone()["c"]
        favs = conn.execute(
            "SELECT COUNT(*) AS c FROM watchlist WHERE is_favorited = 1"
        ).fetchone()["c"]
        owned = conn.execute(
            "SELECT COUNT(*) AS c FROM watchlist WHERE is_owned = 1"
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT MAX(updated_at) AS t FROM products"
        ).fetchone()["t"]
    return {
        "total": total,
        "on_sale": on_sale,
        "favorites": favs,
        "owned": owned,
        "imusic": imusic,
        "last_updated": last,
    }
