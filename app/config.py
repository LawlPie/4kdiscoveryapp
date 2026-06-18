"""
Central configuration for the 4K Discovery App.

Every value can be overridden with an environment variable, which makes the
container easy to configure via `docker-compose.yml` or a `.env` file without
touching the source code.
"""

from __future__ import annotations

import os
from pathlib import Path


def _get_bool(name: str, default: bool) -> bool:
    """Parse a truthy/falsy environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    """Application-wide settings, populated from the environment."""

    # ----- Paths ---------------------------------------------------------
    # The database lives in a dedicated `data/` directory so it can be mounted
    # as a Docker volume and survive container rebuilds.
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
    DB_PATH: Path = Path(os.getenv("DB_PATH", str(DATA_DIR / "deals.db")))

    # ----- Scraper -------------------------------------------------------
    # The Platekompaniet 4K Ultra HD category landing page. The scraper walks
    # the pagination from here. Override if the retailer changes its URL scheme.
    SCRAPER_BASE_URL: str = os.getenv(
        "SCRAPER_BASE_URL",
        "https://www.platekompaniet.no/filmer-serier/4k-ultra-hd",
    )
    # Root used to turn relative product/image links into absolute URLs.
    SITE_ROOT: str = os.getenv("SITE_ROOT", "https://www.platekompaniet.no")

    # ----- Algolia (Platekompaniet's product search backend) -------------
    # Platekompaniet is a client-side SPA whose category/listing pages are
    # rendered from Algolia, not server-side HTML. We therefore query the same
    # public, search-only Algolia index the website's own frontend uses. These
    # credentials are embedded in the site's JavaScript bundle; override via env
    # if Platekompaniet ever rotates them.
    ALGOLIA_APP_ID: str = os.getenv("ALGOLIA_APP_ID", "H4ZQSN0RMC")
    ALGOLIA_API_KEY: str = os.getenv(
        "ALGOLIA_API_KEY",
        "NTA5ZGNjMjBlM2FmZTMwZWQxZTlkMTNiYzQxN2RiMTAwMmRm"
        "YWE4ZDczZWQwYWQ3NTk3ZTA5ZTlhNGM4ZDFjYXRhZ0ZpbHRlcnM9",
    )
    ALGOLIA_INDEX: str = os.getenv("ALGOLIA_INDEX", "plate_prod_default_products")
    # Facet filter that isolates strictly "4K Ultra HD" releases.
    ALGOLIA_4K_FILTER: str = os.getenv("ALGOLIA_4K_FILTER", "format_media:4K Ultra HD")
    # Algolia caps standard pagination at 1000 results; fetch in pages of this size.
    SCRAPE_HITS_PER_PAGE: int = _get_int("SCRAPE_HITS_PER_PAGE", 100)
    SCRAPE_MAX_ITEMS: int = _get_int("SCRAPE_MAX_ITEMS", 1000)
    # If true, only store items currently on offer/campaign (smaller DB).
    SCRAPE_ONLY_ON_OFFER: bool = _get_bool("SCRAPE_ONLY_ON_OFFER", False)

    # Full-catalogue mode: recursively bisect the product_id space so we can fetch
    # ALL ~6500 4K items, working around Algolia's 1000-results-per-query cap.
    # When False, falls back to the (faster) top-SCRAPE_MAX_ITEMS popular subset.
    SCRAPE_FULL_CATALOGUE: bool = _get_bool("SCRAPE_FULL_CATALOGUE", True)
    # The unique numeric attribute used to partition the result space.
    SCRAPE_PARTITION_ATTR: str = os.getenv("SCRAPE_PARTITION_ATTR", "product_id")
    # Initial bounds of that attribute (generous; empty sub-ranges prune instantly).
    SCRAPE_ID_MIN: int = _get_int("SCRAPE_ID_MIN", 0)
    SCRAPE_ID_MAX: int = _get_int("SCRAPE_ID_MAX", 100_000_000)
    # Safety budget on total Algolia requests per full crawl.
    SCRAPE_MAX_QUERIES: int = _get_int("SCRAPE_MAX_QUERIES", 500)

    # How often the background scraper runs, in hours.
    SCRAPE_INTERVAL_HOURS: float = _get_float("SCRAPE_INTERVAL_HOURS", 24.0)
    # Run a scrape immediately on startup (handy for a fresh install).
    SCRAPE_ON_STARTUP: bool = _get_bool("SCRAPE_ON_STARTUP", True)

    # Politeness knobs to avoid hammering the retailer / getting IP-blocked.
    SCRAPE_DELAY_SECONDS: float = _get_float("SCRAPE_DELAY_SECONDS", 1.5)
    SCRAPE_MAX_PAGES: int = _get_int("SCRAPE_MAX_PAGES", 40)
    SCRAPE_TIMEOUT_SECONDS: float = _get_float("SCRAPE_TIMEOUT_SECONDS", 30.0)
    SCRAPE_RETRIES: int = _get_int("SCRAPE_RETRIES", 3)
    USER_AGENT: str = os.getenv(
        "USER_AGENT",
        # A realistic desktop browser UA string to blend in with normal traffic.
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36",
    )

    # ----- Notifications -------------------------------------------------
    # Provide ONE of these to enable alerts. Discord takes precedence if both set.
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    # Only notify when a watched item drops by at least this fraction (0.0 = any).
    NOTIFY_MIN_DROP_PCT: float = _get_float("NOTIFY_MIN_DROP_PCT", 0.0)

    # ----- Web server ----------------------------------------------------
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = _get_int("PORT", 8000)
    # Items shown per page in the web UI.
    PAGE_SIZE: int = _get_int("PAGE_SIZE", 100)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.DISCORD_WEBHOOK_URL)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    @property
    def notifications_enabled(self) -> bool:
        return self.discord_enabled or self.telegram_enabled


settings = Settings()

# Ensure the data directory exists as early as possible.
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
