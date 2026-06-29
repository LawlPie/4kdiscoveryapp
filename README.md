# 📀 4K Discovery

A self-hosted web app that tracks **4K Ultra HD Blu-ray deals** on the Norwegian
retailer [Platekompaniet](https://www.platekompaniet.no), and **compares prices
against [iMusic](https://imusic.co)**. It scrapes the 4K catalogues daily, stores
price history, lets you favourite titles, and sends a **Discord / Telegram
notification** when a watched movie drops in price or enters a campaign.

The entire stack — web UI, background scraper, and SQLite database — runs in a
**single container**, so it's trivial to host on a NAS, home server, or any box
with Docker.

---

## ✨ Features

- **🌊 Trawler view** — all current 4K deals, with title **search**,
  **pagination** (100/page), sorting (biggest discount, price, title, recency)
  and filtering by campaign tag.
- **🎬 Edition grouping** — variants of the same release (steelbook, imports,
  limited editions) are collapsed into one card showing the **cheapest** price;
  click through to a detail page listing every edition with its price,
  steelbook/limited flag, and stock.
- **⚖️ iMusic price comparison** — the same disc (matched by **EAN/barcode**) is
  compared against iMusic; cards flag "cheaper at iMusic" and each movie's detail
  page shows Platekompaniet vs iMusic per edition with the cheaper side
  highlighted. Both retailers price in NOK, so it's a direct comparison.
- **🎞️ Criterion collector view** — a separate poster-grid tab listing every
  Criterion Collection 4K (identified by Criterion's US barcode), with a
  **🇬🇧 UK-alternative** flag when a UK Criterion pressing or boutique label
  (Arrow, Second Sight, Powerhouse, …) released the same film. Click a poster for
  a per-film **comparison page** showing every edition across **both retailers**
  with region (from barcode) and price, cheapest highlighted. A corner button
  checks off what you've collected.
- **❤️ Watchlist** — favourite any title; the heart toggles instantly via an
  async API call (no page reload).
- **✓ Collection** — mark titles you already own; they're hidden from the
  Trawler/Watchlist and skipped by the scraper (favourite & owned are mutually
  exclusive).
- **📈 Price history** — every price change is logged to `price_history`.
- **🔔 Notifications** — rich Discord embeds or Telegram messages when a
  favourited item gets cheaper or a new campaign starts.
- **🕒 Scheduled scraper** — runs every 24 h (configurable) in-process via
  APScheduler; also triggerable on demand from the UI.
- **🐳 One-container deploy** — `Dockerfile` + `docker-compose.yml` included.

---

## 🚀 Quick start (Docker)

```bash
# 1. (optional) configure notifications
cp .env.example .env
#    edit .env and paste a DISCORD_WEBHOOK_URL or Telegram credentials

# 2. build & run
docker compose up -d --build

# 3. open the UI
open http://localhost:8000
```

The SQLite database is persisted to `./data/deals.db` on the host.

### Deploying with Portainer

Use [`portainer-stack.yml`](portainer-stack.yml) (named volume + UI-overridable
env vars). It **pulls a pre-built image** so Portainer never has to build on your
host — which avoids BuildKit errors common on NAS/self-hosted Docker
(`failed to list workers … http2 frame too large`).

**One-time:** GitHub Actions
([`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml))
builds and pushes `ghcr.io/lawlpie/4kdiscoveryapp:latest` on every push to
`main`. After the first run, make the GHCR package **Public** (GitHub profile →
**Packages** → `4kdiscoveryapp` → *Package settings* → *Change visibility*), or
add `ghcr.io` credentials under Portainer → **Registries**.

**Deploy:**

1. Portainer → **Stacks → Add stack** → name it `fourk-discovery`
2. **Web editor** → paste `portainer-stack.yml` (or use **Repository** deploy,
   Compose path `portainer-stack.yml`)
3. Add your `DISCORD_WEBHOOK_URL` (or Telegram pair) under **Environment variables**
4. **Deploy the stack** → open `http://<host>:8000`

The SQLite database lives in the managed `fourk_data` volume and survives stack
updates/redeploys. To update later, redeploy the stack to pull the newest
`:latest` image.

### Trying it without scraping

The live retailer markup changes over time. To explore the UI immediately with
sample data:

```bash
docker compose run --rm fourk-discovery python -m app.seed
```

---

## 🧑‍💻 Local development (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional: load demo data
python -m app.seed

# run the server (scheduler + scraper start automatically)
uvicorn app.main:app --reload --port 8000
```

Run a one-off scrape from the CLI:

```bash
python -m app.scraper
```

---

## ⚙️ Configuration

All settings are environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Description |
|---|---|---|
| `SCRAPE_INTERVAL_HOURS` | `24` | How often the scraper runs |
| `SCRAPE_ON_STARTUP` | `true` | Run once immediately on boot |
| `SCRAPE_FULL_CATALOGUE` | `true` | Fetch **all** ~6500 4K titles (bisects the id space to beat Algolia's 1000-cap). Set `false` for the faster top-popular subset |
| `SCRAPE_MAX_ITEMS` | `1000` | Cap for the top-popular mode only (ignored in full mode) |
| `SCRAPE_ONLY_ON_OFFER` | `false` | Store only items currently on offer/campaign |
| `SCRAPE_IMUSIC` | `true` | Also scrape iMusic's 4K catalogue for price comparison |
| `IMUSIC_IMPERSONATE` | `chrome` | Browser TLS fingerprint used to fetch iMusic |
| `SCRAPE_DELAY_SECONDS` | `1.5` | Politeness delay between API pages |
| `ALGOLIA_APP_ID` / `ALGOLIA_API_KEY` | (site defaults) | Platekompaniet's public search backend |
| `ALGOLIA_INDEX` | `plate_prod_default_products` | Algolia product index |
| `ALGOLIA_4K_FILTER` | `format_media:4K Ultra HD` | Facet filter isolating 4K releases |
| `DISCORD_WEBHOOK_URL` | — | Enable Discord notifications |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Enable Telegram notifications |
| `NOTIFY_MIN_DROP_PCT` | `0.0` | Min discount off the original to alert (e.g. `0.1` = 10%) |
| `NOTIFY_MAX_PER_RUN` | `25` | Cap on alerts sent per scrape (flood protection) |
| `DB_PATH` | `data/deals.db` | SQLite file location |

---

## 🌐 API

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/` | Trawler view (HTML) |
| `GET`  | `/watchlist` | Watchlist view (HTML) |
| `GET`  | `/movie/{product_id}` | Detail view — all editions of a release (HTML) |
| `GET`  | `/criterion` | Criterion 4K collector view (HTML) — `q`, `show`, `page` |
| `GET`  | `/criterion/{product_id}` | Per-film edition comparison across retailers (HTML) |
| `POST` | `/api/favorite/{product_id}` | Toggle favourite → `{is_favorited}` |
| `POST` | `/api/owned/{product_id}` | Toggle owned/collection → `{is_owned}` |
| `GET`  | `/owned` | Collection view (HTML) |
| `GET`  | `/api/products` | JSON list (`sort`, `campaign`, `on_sale`, `favorites`, `owned`, `exclude_owned`, `search`, `limit`, `offset`) |
| `GET`  | `/api/products/{id}/history` | Price history |
| `POST` | `/api/scrape` | Trigger a scrape now |
| `POST` | `/api/test-notification` | Send a test alert |
| `GET`  | `/health` | Health check |

---

## 🗄️ Database schema

- **`products`** — current state of each discovered 4K item (title, slug/id,
  current & original price, discount %, campaign tags, stock, timestamps,
  `product_family`/`edition`/`group_key` for edition grouping, plus
  `retailer`/`ean` for multi-store price comparison).
- **`price_history`** — append-only `(product_id, price, original_price, date)`
  snapshots for trend tracking.
- **`watchlist`** — `(product_id, is_favorited, is_owned)` — per-product user
  flags for the wishlist heart and the owned collection.

---

## 🔧 How the scraper works

Platekompaniet's website is a client-side SPA whose listing pages contain no
product HTML — the catalogue is rendered in the browser from **Algolia**, a
hosted search API. So instead of scraping HTML, the worker queries the same
public, search-only Algolia index the site's own frontend uses
([`app/scraper.py`](app/scraper.py)), filtered to `format_media:4K Ultra HD`.
This returns clean structured JSON (title, price, regular price, campaign name,
stock) — far more reliable than parsing markup.

**Full-catalogue coverage.** Algolia caps any single query at 1000 results, but
the 4K catalogue is ~6500 titles. In full mode (`SCRAPE_FULL_CATALOGUE=true`,
the default) the worker recursively **bisects the numeric `product_id` range**:
any sub-range with ≤1000 hits is fetched whole, larger ranges split in half, and
empty ranges prune instantly. This pulls the entire catalogue in ~30 small
queries (measured: all 6542 items in ~6s) — category-independent and complete.

**iMusic.** iMusic blocks ordinary HTTP clients (HTTP 418, a TLS-fingerprint
block), so [`app/imusic_scraper.py`](app/imusic_scraper.py) fetches with
`curl_cffi` (Chrome TLS impersonation) and parses the HTML listing. Each product
URL contains its EAN, which is matched against Platekompaniet's `ean` for a
same-disc price comparison. If iMusic ever changes its blocking and the scrape
returns 0 items, set `SCRAPE_IMUSIC=false` to disable it without affecting
Platekompaniet, or try a different `IMUSIC_IMPERSONATE` value.

If a run returns 0 items, Platekompaniet has likely rotated its Algolia
credentials or renamed the index/facet. Re-extract them from the site's JS
bundle and set `ALGOLIA_APP_ID`, `ALGOLIA_API_KEY`, `ALGOLIA_INDEX`, and
`ALGOLIA_4K_FILTER` via the environment.

> **Be a good citizen.** This tool is for personal use. Keep the interval and
> delay reasonable and respect Platekompaniet's terms of service.

---

## 📁 Project layout

```
4KDiscoveryApp/
├── app/
│   ├── main.py            # FastAPI app + routes
│   ├── config.py          # env-driven settings
│   ├── database.py        # SQLite schema + queries
│   ├── scraper.py         # Platekompaniet Algolia API client + pagination
│   ├── imusic_scraper.py  # iMusic scraper (curl_cffi + BeautifulSoup)
│   ├── scheduler.py       # APScheduler background worker
│   ├── notifications.py   # Discord / Telegram webhooks
│   ├── seed.py            # demo data
│   ├── templates/         # Jinja2 + Tailwind HTML
│   └── static/            # app.js, app.css
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```
