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
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

from .config import settings


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
    for col in ("product_family", "edition", "group_key", "ean"):
        if col not in p_cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
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
                first_seen, last_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["product_id"], item["title"], item["url"], item.get("image_url"),
                current_price, original_price, discount_pct, on_sale, tags_json,
                item.get("stock_status"), family, edition, group_key, retailer, ean,
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
            last_seen = ?, updated_at = ?
        WHERE product_id = ?
        """,
        (
            item["title"], item["url"], item.get("image_url"), current_price,
            original_price, discount_pct, on_sale, tags_json,
            item.get("stock_status"), family, edition, group_key, retailer, ean,
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
