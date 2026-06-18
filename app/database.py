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
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .config import settings


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

    # Add edition-grouping columns to products (cheapest-of-duplicates feature).
    p_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
    for col in ("product_family", "edition", "group_key"):
        if col not in p_cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
    # Backfill a sane group_key for rows that predate the column; the next scrape
    # replaces it with the real product_family-based key.
    conn.execute(
        "UPDATE products SET group_key = 'id:' || product_id WHERE group_key IS NULL"
    )
    # Safe to create now that the column is guaranteed to exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_group ON products(group_key)")


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
                product_family, edition, group_key,
                first_seen, last_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["product_id"], item["title"], item["url"], item.get("image_url"),
                current_price, original_price, discount_pct, on_sale, tags_json,
                item.get("stock_status"), family, edition, group_key, now, now, now,
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
            product_family = ?, edition = ?, group_key = ?,
            last_seen = ?, updated_at = ?
        WHERE product_id = ?
        """,
        (
            item["title"], item["url"], item.get("image_url"), current_price,
            original_price, discount_pct, on_sale, tags_json,
            item.get("stock_status"), family, edition, group_key,
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
) -> tuple[str, list[Any]]:
    """Build a shared WHERE clause (+params) reused by list/count queries."""
    where: list[str] = []
    params: list[Any] = []

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
    sort_clause = {
        "discount": "discount_pct DESC, current_price ASC",
        "price_asc": "current_price IS NULL, current_price ASC",
        "price_desc": "current_price DESC",
        "title": "title COLLATE NOCASE ASC",
        "recent": "updated_at DESC",
    }.get(sort, "discount_pct DESC")

    if grouped:
        # Rank variants within each group by price; keep the cheapest as the
        # representative (rn = 1) and expose how many editions matched.
        sql = (
            "WITH base AS ("
            "  SELECT p.*, COALESCE(w.is_favorited, 0) AS is_favorited, "
            "         COALESCE(w.is_owned, 0) AS is_owned "
            "  FROM products p "
            "  LEFT JOIN watchlist w ON w.product_id = p.product_id "
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
            "COALESCE(w.is_owned, 0) AS is_owned "
            "FROM products p "
            "LEFT JOIN watchlist w ON w.product_id = p.product_id "
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
    """Return the distinct set of campaign tags currently in the catalogue."""
    tags: set[str] = set()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT campaign_tags FROM products WHERE campaign_tags != '[]'"
        ).fetchall()
    for r in rows:
        try:
            for t in json.loads(r["campaign_tags"] or "[]"):
                if t:
                    tags.add(t)
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(tags)


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
    """Small dashboard summary used in the header."""
    with db_session() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        on_sale = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE on_sale = 1"
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
        "last_updated": last,
    }
