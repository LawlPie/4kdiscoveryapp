"""
FastAPI application entry point.

Serves the HTML UI (Jinja2 + Tailwind) and a small JSON/HTMX API. The scraper
runs in-process via APScheduler, so a single container provides the full stack:
web server, background worker, and the SQLite database file.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from math import ceil
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from . import database as db
from .notifications import send_test_notification
from .scheduler import (
    get_last_result,
    shutdown_scheduler,
    start_scheduler,
    trigger_scrape_async,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("app")


class _QuietAccessFilter(logging.Filter):
    """
    Drop access-log lines for noisy, uninteresting endpoints — chiefly the
    container healthcheck hitting /health every 30s, plus static assets and
    favicon. Real page/API requests are still logged.
    """

    _QUIET = ("/health", "/static/", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(path in msg for path in self._QUIET)


def _quiet_access_logs() -> None:
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the DB + scheduler on startup, tear down on shutdown."""
    _quiet_access_logs()
    db.init_db()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="4K Discovery", version="1.0.0", lifespan=lifespan)

BASE_DIR = settings.BASE_DIR
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "app" / "static")),
    name="static",
)


# Make a few helpers available to all templates.
def _format_nok(value):
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ") + " kr"


templates.env.filters["nok"] = _format_nok


def _asset_version() -> str:
    """
    Content hash of the JS/CSS, appended to their URLs as ?v=… so browsers fetch
    the new files after every deploy instead of serving a stale cached copy
    (which previously left click handlers like toggleCollected undefined).
    """
    import hashlib

    h = hashlib.md5()
    static_dir = BASE_DIR / "app" / "static"
    for name in ("app.js", "app.css"):
        try:
            h.update((static_dir / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


# Recomputed at import (i.e. per container start / deploy).
templates.env.globals["asset_version"] = _asset_version()


def _paginate(total: int, page: int) -> dict[str, int]:
    """Clamp the page and compute offset/total_pages for `total` rows."""
    per_page = settings.PAGE_SIZE
    total_pages = max(1, ceil(total / per_page)) if total else 1
    page = max(1, min(page, total_pages))
    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total": total,
        "offset": (page - 1) * per_page,
    }


def _base_query(**params: Any) -> str:
    """URL-encode the active filter params (minus `page`) for pagination links."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return urlencode(clean)


def _attach_imusic(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate each product with the matching iMusic offer (same EAN), if any."""
    offers = db.get_offers_by_ean([p.get("ean") for p in products])
    for p in products:
        off = offers.get(p.get("ean"))
        if off and off.get("current_price") is not None:
            p["imusic_price"] = off["current_price"]
            p["imusic_url"] = off["url"]
            p["imusic_stock"] = off.get("stock_status")
            cur = p.get("current_price")
            p["cheaper_at_imusic"] = cur is not None and off["current_price"] < cur
    return products


# --------------------------------------------------------------------------- #
# HTML pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    sort: str = "discount",
    campaign: Optional[str] = None,
    on_sale: int = 1,
    q: Optional[str] = None,
    page: int = 1,
):
    """The Trawler view — all 4K deals with filtering/search/sorting (owned hidden)."""
    search = (q or "").strip() or None
    filters = dict(
        only_on_sale=bool(on_sale),
        campaign=campaign or None,
        search=search,
        exclude_owned=True,
    )
    # Group editions of the same release; the card shows the cheapest variant.
    pg = _paginate(db.count_products(grouped=True, **filters), page)
    products = db.list_products(
        **filters, sort=sort, grouped=True, limit=pg["per_page"], offset=pg["offset"]
    )
    _attach_imusic(products)  # show "cheaper at iMusic" badges where applicable
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "products": products,
            "campaigns": db.list_campaign_tags(),
            "stats": db.get_stats(),
            "sort": sort,
            "campaign": campaign or "",
            "on_sale": on_sale,
            "q": search or "",
            "pg": pg,
            "base_query": _base_query(sort=sort, campaign=campaign or "", on_sale=on_sale, q=search or ""),
            "active_view": "dashboard",
            "last_scrape": get_last_result(),
        },
    )


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(
    request: Request, sort: str = "discount", q: Optional[str] = None, page: int = 1
):
    """The Hearts view — only favourited items (owned hidden)."""
    search = (q or "").strip() or None
    filters = dict(only_favorites=True, exclude_owned=True, search=search)
    pg = _paginate(db.count_products(**filters), page)
    products = db.list_products(
        **filters, sort=sort, limit=pg["per_page"], offset=pg["offset"]
    )
    _attach_imusic(products)  # show "cheaper at iMusic" badges here too
    return templates.TemplateResponse(
        "watchlist.html",
        {
            "request": request,
            "products": products,
            "stats": db.get_stats(),
            "sort": sort,
            "q": search or "",
            "pg": pg,
            "base_query": _base_query(sort=sort, q=search or ""),
            "active_view": "watchlist",
        },
    )


@app.get("/owned", response_class=HTMLResponse)
def owned(
    request: Request, sort: str = "title", q: Optional[str] = None, page: int = 1
):
    """The Collection view — movies the user already owns (grouped per film)."""
    search = (q or "").strip() or None
    filters = dict(only_owned=True, search=search)
    # Group editions so a collected film shows as ONE card (like the Trawler).
    pg = _paginate(db.count_products(grouped=True, **filters), page)
    products = db.list_products(
        **filters, sort=sort, grouped=True, limit=pg["per_page"], offset=pg["offset"]
    )
    return templates.TemplateResponse(
        "owned.html",
        {
            "request": request,
            "products": products,
            "stats": db.get_stats(),
            "sort": sort,
            "q": search or "",
            "pg": pg,
            "base_query": _base_query(sort=sort, q=search or ""),
            "active_view": "owned",
        },
    )


@app.get("/criterion", response_class=HTMLResponse)
def criterion(
    request: Request,
    q: Optional[str] = None,
    show: str = "all",
    sort: str = "title",
    page: int = 1,
):
    """Criterion Collection 4K checklist (poster view) with UK-alternative flags."""
    search = (q or "").strip().lower() or None
    all_films = db.get_criterion_releases()
    summary = {
        "films": len(all_films),
        "collected": sum(1 for f in all_films if f["is_owned"]),
        "with_uk": sum(1 for f in all_films if f["has_uk_alt"]),
    }

    films = all_films
    if search:
        films = [f for f in films if search in f["title"].lower()]
    if show == "uncollected":
        films = [f for f in films if not f["is_owned"]]
    elif show == "collected":
        films = [f for f in films if f["is_owned"]]
    elif show == "uk":
        films = [f for f in films if f["has_uk_alt"]]

    _BIG = float("inf")
    sorters = {
        "title": lambda f: f["title"].lower(),
        "year": lambda f: (f.get("year") is None, f.get("year") or 0, f["title"].lower()),
        "year_desc": lambda f: (f.get("year") is None, -(f.get("year") or 0), f["title"].lower()),
        "price": lambda f: (f.get("from_price") is None, f.get("from_price") or _BIG),
    }
    films = sorted(films, key=sorters.get(sort, sorters["title"]))

    pg = _paginate(len(films), page)
    page_items = films[pg["offset"]: pg["offset"] + pg["per_page"]]

    return templates.TemplateResponse(
        "criterion.html",
        {
            "request": request,
            "films": page_items,
            "summary": summary,
            "stats": db.get_stats(),
            "q": search or "",
            "show": show,
            "sort": sort,
            "pg": pg,
            "base_query": _base_query(
                q=search or "", show=show if show != "all" else "",
                sort=sort if sort != "title" else "",
            ),
            "active_view": "criterion",
        },
    )


@app.get("/criterion/{product_id}", response_class=HTMLResponse)
def criterion_detail(request: Request, product_id: str):
    """Comparison page for one Criterion film: every edition across both stores."""
    film = db.get_criterion_film(product_id)
    status = 200 if film else 404
    return templates.TemplateResponse(
        "criterion_detail.html",
        {
            "request": request,
            "film": film,
            "stats": db.get_stats(),
            "active_view": "criterion",
        },
        status_code=status,
    )


@app.get("/movie/{product_id}", response_class=HTMLResponse)
def movie_detail(request: Request, product_id: str):
    """Detail view: every edition of a release with prices, format flags, stock."""
    product = db.get_product(product_id)
    if product is None:
        return templates.TemplateResponse(
            "movie.html",
            {"request": request, "product": None, "variants": [], "stats": db.get_stats()},
            status_code=404,
        )
    variants = db.get_group_variants(product["group_key"])
    _attach_imusic(variants)  # per-edition iMusic price (matched by EAN)

    # The cheapest in-scope variant is the "headline" of the page.
    cheapest = min(
        (v for v in variants if v.get("current_price") is not None),
        key=lambda v: v["current_price"],
        default=variants[0] if variants else product,
    )
    # Film-level cheapest at each retailer (possibly different editions).
    pk_prices = [v["current_price"] for v in variants if v.get("current_price") is not None]
    im_prices = [v["imusic_price"] for v in variants if v.get("imusic_price") is not None]
    compare = {
        "best_pk": min(pk_prices) if pk_prices else None,
        "best_imusic": min(im_prices) if im_prices else None,
    }
    return templates.TemplateResponse(
        "movie.html",
        {
            "request": request,
            "product": product,
            "cheapest": cheapest,
            "variants": variants,
            "compare": compare,
            "stats": db.get_stats(),
            "active_view": "",
        },
    )


# --------------------------------------------------------------------------- #
# JSON / HTMX API
# --------------------------------------------------------------------------- #
@app.post("/api/favorite/{product_id}")
def api_toggle_favorite(product_id: str):
    """Toggle a product's favourite flag (called from the heart button)."""
    product = db.get_product(product_id)
    if product is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    new_state = db.toggle_favorite(product_id)
    # Favouriting clears 'owned' (mutually exclusive) — report both states.
    return {"product_id": product_id, "is_favorited": new_state, "is_owned": False}


@app.post("/api/owned/{product_id}")
def api_toggle_owned(product_id: str, group: int = 0):
    """
    Toggle a product's owned/collection flag. With `group=1`, toggle every
    edition that shares the film's group (the Trawler/Collection grouping), so
    collecting a film marks all its editions at once.
    """
    product = db.get_product(product_id)
    if product is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    new_state = db.toggle_owned_group(product_id) if group else db.toggle_owned(product_id)
    # Marking owned clears the favourite flag (mutually exclusive).
    return {
        "product_id": product_id,
        "is_owned": new_state,
        "is_favorited": False if new_state else product["is_favorited"],
    }


@app.get("/api/products")
def api_products(
    sort: str = "discount",
    campaign: Optional[str] = None,
    on_sale: int = 0,
    favorites: int = 0,
    owned: int = 0,
    exclude_owned: int = 0,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
):
    """JSON list endpoint, useful for debugging or external integrations."""
    return db.list_products(
        only_on_sale=bool(on_sale),
        only_favorites=bool(favorites),
        only_owned=bool(owned),
        exclude_owned=bool(exclude_owned),
        campaign=campaign or None,
        search=(search or "").strip() or None,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@app.get("/api/products/{product_id}/history")
def api_history(product_id: str):
    """Price history for a single product (drives the sparkline)."""
    return {
        "product_id": product_id,
        "history": db.get_price_history(product_id),
    }


@app.post("/api/scrape")
def api_scrape():
    """Manually trigger a scrape in the background."""
    started = trigger_scrape_async()
    return {"started": started, "message": "Scrape started" if started else "Already running"}


@app.post("/api/test-notification")
def api_test_notification():
    """Send a test notification to verify webhook configuration."""
    if not settings.notifications_enabled:
        return JSONResponse(
            {"sent": False, "message": "No webhook configured"}, status_code=400
        )
    sent = send_test_notification()
    return {"sent": sent}


@app.get("/api/stats")
def api_stats():
    return db.get_stats()


@app.get("/health")
def health():
    return {"status": "ok"}


# Convenience: allow toggling favourites via a non-JS form fallback too.
@app.post("/favorite")
def form_toggle_favorite(product_id: str = Form(...), next: str = Form("/")):
    db.toggle_favorite(product_id)
    return RedirectResponse(url=next, status_code=303)
