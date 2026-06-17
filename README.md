# 📀 4K Discovery

A self-hosted web app that tracks **4K Ultra HD Blu-ray deals** on the Norwegian
retailer [Platekompaniet](https://www.platekompaniet.no). It scrapes the 4K
category daily, stores price history, lets you favourite titles, and sends a
**Discord / Telegram notification** when a watched movie drops in price or enters
a campaign.

The entire stack — web UI, background scraper, and SQLite database — runs in a
**single container**, so it's trivial to host on a NAS, home server, or any box
with Docker.

---

## ✨ Features

- **🌊 Trawler view** — all current 4K deals, with sorting (biggest discount,
  price, title, recency) and filtering by campaign tag.
- **❤️ Watchlist** — favourite any title; the heart toggles instantly via an
  async API call (no page reload).
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

Use [`portainer-stack.yml`](portainer-stack.yml) instead of `docker-compose.yml`
(named volume + UI-overridable env vars):

1. Portainer → **Stacks → Add stack** → name it `fourk-discovery`
2. Build method: **Repository**
   - Repository URL: `https://github.com/LawlPie/4kdiscoveryapp`
   - Compose path: `portainer-stack.yml`
3. Add your `DISCORD_WEBHOOK_URL` (or Telegram pair) under **Environment variables**
4. **Deploy the stack** → open `http://<host>:8000`

The SQLite database lives in the managed `fourk_data` volume and survives stack
updates/redeploys.

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
| `SCRAPER_BASE_URL` | …/filmer-serier/4k-ultra-hd | 4K category page to crawl |
| `SITE_ROOT` | https://www.platekompaniet.no | Used to absolutise links |
| `SCRAPE_INTERVAL_HOURS` | `24` | How often the scraper runs |
| `SCRAPE_ON_STARTUP` | `true` | Run once immediately on boot |
| `SCRAPE_DELAY_SECONDS` | `1.5` | Politeness delay between pages |
| `SCRAPE_MAX_PAGES` | `40` | Pagination safety cap |
| `DISCORD_WEBHOOK_URL` | — | Enable Discord notifications |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Enable Telegram notifications |
| `NOTIFY_MIN_DROP_PCT` | `0.0` | Min fractional price drop to alert (e.g. `0.1` = 10%) |
| `DB_PATH` | `data/deals.db` | SQLite file location |

---

## 🌐 API

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/` | Trawler view (HTML) |
| `GET`  | `/watchlist` | Watchlist view (HTML) |
| `POST` | `/api/favorite/{product_id}` | Toggle favourite → `{is_favorited}` |
| `GET`  | `/api/products` | JSON list (`sort`, `campaign`, `on_sale`, `favorites`) |
| `GET`  | `/api/products/{id}/history` | Price history |
| `POST` | `/api/scrape` | Trigger a scrape now |
| `POST` | `/api/test-notification` | Send a test alert |
| `GET`  | `/health` | Health check |

---

## 🗄️ Database schema

- **`products`** — current state of each discovered 4K item (title, slug/id,
  current & original price, discount %, campaign tags, stock, timestamps).
- **`price_history`** — append-only `(product_id, price, original_price, date)`
  snapshots for trend tracking.
- **`watchlist`** — `(product_id, is_favorited)`.

---

## 🔧 Tuning the scraper

Retailer HTML changes. If a scrape returns 0 items, update the CSS selectors in
[`app/scraper.py`](app/scraper.py) → the `SELECTORS` dict. Each field accepts a
list of candidate selectors that are tried in order, so you can add new ones
without removing the existing fallbacks.

> **Be a good citizen.** This tool is for personal use. Keep the request delay
> reasonable and respect Platekompaniet's `robots.txt` and terms of service.

---

## 📁 Project layout

```
4KDiscoveryApp/
├── app/
│   ├── main.py            # FastAPI app + routes
│   ├── config.py          # env-driven settings
│   ├── database.py        # SQLite schema + queries
│   ├── scraper.py         # BeautifulSoup scraper + pagination
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
