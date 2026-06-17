"""
Seed the database with sample 4K items.

Useful for trying the UI before tuning the live scraper selectors, or for local
development without hitting Platekompaniet at all.

    python -m app.seed
"""

from __future__ import annotations

from .database import db_session, init_db, upsert_product

SAMPLE = [
    {
        "product_id": "dune-part-two-4k",
        "title": "Dune: Part Two (4K Ultra HD)",
        "url": "https://www.platekompaniet.no/dune-part-two-4k",
        "image_url": "",
        "current_price": 229.0,
        "original_price": 399.0,
        "discount_pct": 42.6,
        "on_sale": True,
        "campaign_tags": ["Kjøp 2, få 30%"],
        "stock_status": "På lager",
    },
    {
        "product_id": "oppenheimer-4k",
        "title": "Oppenheimer (4K Ultra HD)",
        "url": "https://www.platekompaniet.no/oppenheimer-4k",
        "image_url": "",
        "current_price": 299.0,
        "original_price": 449.0,
        "discount_pct": 33.4,
        "on_sale": True,
        "campaign_tags": ["Kampanje"],
        "stock_status": "På lager",
    },
    {
        "product_id": "blade-runner-2049-4k",
        "title": "Blade Runner 2049 (4K Ultra HD)",
        "url": "https://www.platekompaniet.no/blade-runner-2049-4k",
        "image_url": "",
        "current_price": 179.0,
        "original_price": 249.0,
        "discount_pct": 28.1,
        "on_sale": True,
        "campaign_tags": ["Kjøp 2, få 30%"],
        "stock_status": "Få igjen",
    },
    {
        "product_id": "interstellar-4k",
        "title": "Interstellar (4K Ultra HD)",
        "url": "https://www.platekompaniet.no/interstellar-4k",
        "image_url": "",
        "current_price": 349.0,
        "original_price": None,
        "discount_pct": 0.0,
        "on_sale": False,
        "campaign_tags": [],
        "stock_status": "På lager",
    },
    {
        "product_id": "the-batman-4k",
        "title": "The Batman (4K Ultra HD)",
        "url": "https://www.platekompaniet.no/the-batman-4k",
        "image_url": "",
        "current_price": 199.0,
        "original_price": 329.0,
        "discount_pct": 39.5,
        "on_sale": True,
        "campaign_tags": ["Kampanje", "Nyhet"],
        "stock_status": "Utsolgt",
    },
]


def seed() -> None:
    init_db()
    with db_session() as conn:
        for item in SAMPLE:
            upsert_product(conn, item)
    print(f"Seeded {len(SAMPLE)} sample products.")


if __name__ == "__main__":
    seed()
