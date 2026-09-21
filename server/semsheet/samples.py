"""Sample datasets offered on the landing screen. Files are prepared by scripts/prepare_samples.py."""
from __future__ import annotations

from pathlib import Path

from .config import settings

SAMPLES: list[dict] = [
    {
        "key": "support_requests",
        "title": "Customer support requests",
        "file": "bitext_support_3000.csv",
        "description": "3,000 synthetic customer support messages (Bitext). Intent labels are included for accuracy checks only.",
        "suggested": "Find customers trying to cancel an order because they cannot afford it.",
        "options": {},
    },
    {
        "key": "banking_queries",
        "title": "Banking queries (BANKING77)",
        "file": "banking77_test.csv",
        "description": "3,080 real banking customer queries across 77 intents.",
        "suggested": "Separate pending transfers, failed transfers, and transfers sent to the wrong account.",
        "options": {},
    },
    {
        "key": "cfpb_complaints",
        "title": "Consumer finance complaints",
        "file": "cfpb_complaints_5000.csv",
        "description": "5,000 CFPB complaints with narratives, company, product and dates.",
        "suggested": "Find complaints about charges continuing after cancellation, then rank by urgency.",
        "options": {},
    },
    {
        "key": "airbnb_reviews",
        "title": "Airbnb NYC reviews",
        "file": "airbnb_reviews_5000.csv",
        "description": "5,000 multilingual guest reviews joined to listing name, neighbourhood and nightly price.",
        "suggested": "Find reviews reporting unreliable Wi-Fi, group by property, and compare nightly prices.",
        "options": {},
    },
    {
        "key": "retail_products",
        "title": "Online retail products",
        "file": "online_retail_products.csv",
        "description": "Distinct products from Online Retail II with revenue and quantity aggregated per product and country.",
        "suggested": "Classify products into gift categories, then calculate revenue by category and country.",
        "options": {},
    },
    {
        "key": "wdc_offers_left",
        "title": "Product offers (left)",
        "file": "wdc_offers_left.csv",
        "description": "Product offers to match against the catalog sample (WDC product corpus).",
        "suggested": "Match offers to the catalog for the same exact product despite different titles.",
        "options": {},
    },
    {
        "key": "wdc_catalog_right",
        "title": "Product catalog (right)",
        "file": "wdc_catalog_right.csv",
        "description": "Catalog entries used as the right-hand table for semantic matching.",
        "suggested": "",
        "options": {},
    },
]


def sample_path(file: str) -> Path:
    return settings.data_dir / "samples" / file
