"""Prepare demo/test sample CSVs from the raw downloads in data/raw.

Usage: python scripts/prepare_samples.py [--only key,key]
Outputs go to data/samples/. Large files are bounded on purpose: inference demos use 1,000-10,000 rows."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import random
import sys
import zipfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)
random.seed(7)


def bitext() -> None:
    con = duckdb.connect()
    con.execute(
        f"""COPY (SELECT flags, instruction, category, intent FROM read_csv('{RAW / 'bitext.csv'}', header=true, all_varchar=true)
                 USING SAMPLE reservoir(3000 ROWS) REPEATABLE (7)) TO '{OUT / 'bitext_support_3000.csv'}' (FORMAT CSV, HEADER TRUE)"""
    )
    con.execute(
        f"""COPY (SELECT flags, instruction, category, intent FROM read_csv('{RAW / 'bitext.csv'}', header=true, all_varchar=true))
                 TO '{OUT / 'bitext_support_full.csv'}' (FORMAT CSV, HEADER TRUE)"""
    )
    print("bitext: 3000-row sample + full 26,872 rows")


def banking77() -> None:
    (OUT / "banking77_test.csv").write_bytes((RAW / "banking77_test.csv").read_bytes())
    (OUT / "banking77_train.csv").write_bytes((RAW / "banking77_train.csv").read_bytes())
    print("banking77: test (3,080) and train (10,003)")


def cfpb(scan_rows: int = 400_000) -> None:
    """Stream the 5 GB CSV inside the zip; keep a bounded prefix. The 2026 export has no narrative column,
    so semantic operations use Issue / Sub-issue / Product text."""
    with zipfile.ZipFile(RAW / "cfpb_complaints.csv.zip") as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", newline=""))
            header = next(reader)
            rows = []
            for i, row in enumerate(reader):
                if i >= scan_rows:
                    break
                rows.append(row)
    print(f"cfpb: scanned {len(rows)} rows; columns={header}")
    narrative_idx = next((i for i, h in enumerate(header) if "narrative" in h.lower()), None)
    if narrative_idx is not None:
        rows = [r for r in rows if r[narrative_idx].strip()]
        print(f"cfpb: {len(rows)} rows with narratives")
    sample = random.sample(rows, min(5000, len(rows)))
    for fname, data in (("cfpb_complaints_5000.csv", sample), ("cfpb_complaints_100k.csv", rows[:100_000]), ("cfpb_complaints_300k.csv", rows[:300_000])):
        with open(OUT / fname, "w", newline="", encoding="utf-8") as out:
            w = csv.writer(out)
            w.writerow(header)
            w.writerows(data)
    print("cfpb: 5000-row sample, 100k and 300k scale files")


def airbnb() -> None:
    con = duckdb.connect()
    con.execute(f"CREATE TABLE listings AS SELECT id, name, neighbourhood_cleansed, neighbourhood_group_cleansed, room_type, price, number_of_reviews, review_scores_rating FROM read_csv('{RAW / 'airbnb_listings.csv.gz'}', header=true, all_varchar=true)")
    con.execute(f"CREATE TABLE reviews AS SELECT * FROM read_csv('{RAW / 'airbnb_reviews.csv.gz'}', header=true, all_varchar=true) WHERE comments IS NOT NULL AND length(trim(comments)) > 20 USING SAMPLE reservoir(5000 ROWS) REPEATABLE (7)")
    con.execute(
        f"""COPY (SELECT r.id AS review_id, r.listing_id, r.date, r.reviewer_name, r.comments,
                        l.name AS listing_name, l.neighbourhood_cleansed AS neighbourhood, l.neighbourhood_group_cleansed AS borough, l.room_type, l.price AS nightly_price, l.review_scores_rating
                 FROM reviews r LEFT JOIN listings l ON l.id = r.listing_id ORDER BY r.date)
            TO '{OUT / 'airbnb_reviews_5000.csv'}' (FORMAT CSV, HEADER TRUE)"""
    )
    con.execute(f"COPY (SELECT id AS listing_id, name, neighbourhood_cleansed AS neighbourhood, neighbourhood_group_cleansed AS borough, room_type, price AS nightly_price, number_of_reviews, review_scores_rating FROM listings) TO '{OUT / 'airbnb_listings.csv'}' (FORMAT CSV, HEADER TRUE)")
    print("airbnb: 5000 reviews joined to listing metadata + 30k listings")


def online_retail() -> None:
    import pandas as pd

    with zipfile.ZipFile(RAW / "online_retail_ii.zip") as zf:
        xlsx = zf.open(zf.namelist()[0])
        frames = []
        for sheet in ("Year 2009-2010", "Year 2010-2011"):
            print("  reading", sheet, "...")
            frames.append(pd.read_excel(xlsx, sheet_name=sheet, engine="openpyxl", dtype={"Invoice": str, "StockCode": str, "Customer ID": str}))
            xlsx.seek(0)
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.replace(" ", "_") for c in df.columns]
    df["Revenue"] = (df["Quantity"] * df["Price"]).round(2)
    df.to_csv(OUT / "online_retail_transactions.csv", index=False)
    print(f"online retail: {len(df)} transactions")
    con = duckdb.connect()
    con.register("t", df)
    con.execute(
        f"""COPY (SELECT StockCode AS stock_code, any_value(Description) AS description, round(sum(Revenue), 2) AS revenue, sum(Quantity) AS quantity, count(*) AS transactions,
                         count(DISTINCT Country) AS countries
                  FROM t WHERE Description IS NOT NULL AND Price > 0 AND Quantity > 0 AND NOT regexp_matches(StockCode, '^(POST|DOT|M|BANK|C2|ADJUST|TEST)')
                  GROUP BY StockCode ORDER BY revenue DESC) TO '{OUT / 'online_retail_products.csv'}' (FORMAT CSV, HEADER TRUE)"""
    )
    con.execute(
        f"""COPY (SELECT StockCode AS stock_code, Country AS country, round(sum(Revenue), 2) AS revenue, sum(Quantity) AS quantity, count(*) AS transactions
                  FROM t WHERE Description IS NOT NULL AND Price > 0 AND Quantity > 0 GROUP BY StockCode, Country ORDER BY revenue DESC) TO '{OUT / 'online_retail_product_country.csv'}' (FORMAT CSV, HEADER TRUE)"""
    )
    n = con.execute(f"SELECT count(*) FROM read_csv('{OUT / 'online_retail_products.csv'}')").fetchone()[0]
    m = con.execute(f"SELECT count(*) FROM read_csv('{OUT / 'online_retail_product_country.csv'}')").fetchone()[0]
    print(f"online retail: {n} distinct products, {m} product x country rows")


def wdc() -> None:
    """Left offers vs right catalog from the WDC gold standard pairs. The catalog contains the true match of every
    offer plus hard non-matches, so candidate recall and pair precision can both be measured."""
    with zipfile.ZipFile(RAW / "wdc_80pair.zip") as zf:
        raw = zf.read("wdcproducts80cc20rnd000un_gs.json.gz")
    pairs = [json.loads(l) for l in gzip.decompress(raw).decode("utf-8").splitlines() if l.strip()]
    right: dict[int, dict] = {}
    left: dict[int, dict] = {}
    truth: dict[int, int] = {}
    for p in pairs:
        r = right.setdefault(p["id_right"], {"catalog_id": p["id_right"], "brand": p.get("brand_right"), "title": p["title_right"], "description": (p.get("description_right") or "")[:300], "price": p.get("price_right"), "currency": p.get("priceCurrency_right"), "cluster_id": p["cluster_id_right"]})
        l = left.setdefault(p["id_left"], {"offer_id": p["id_left"], "brand": p.get("brand_left"), "title": p["title_left"], "description": (p.get("description_left") or "")[:300], "price": p.get("price_left"), "currency": p.get("priceCurrency_left"), "cluster_id": p["cluster_id_left"]})
        if p["label"] == 1:
            truth[p["id_left"]] = p["id_right"]
    # Keep offers that have a true match in the catalog and bound sizes
    left_rows = [l for l in left.values() if l["offer_id"] in truth]
    random.shuffle(left_rows)
    left_rows = left_rows[:400]
    for l in left_rows:
        l["true_catalog_id"] = truth[l["offer_id"]]
    right_rows = list(right.values())[:2000]
    needed = {l["true_catalog_id"] for l in left_rows}
    have = {r["catalog_id"] for r in right_rows}
    right_rows += [right[i] for i in needed - have]
    for fname, rows in (("wdc_offers_left.csv", left_rows), ("wdc_catalog_right.csv", right_rows)):
        with open(OUT / fname, "w", newline="", encoding="utf-8") as out:
            w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"wdc: {len(left_rows)} offers, {len(right_rows)} catalog entries")


STEPS = {"bitext": bitext, "banking77": banking77, "cfpb": cfpb, "airbnb": airbnb, "online_retail": online_retail, "wdc": wdc}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    keys = [k for k in args.only.split(",") if k] or list(STEPS)
    for k in keys:
        print(f"== {k}")
        try:
            STEPS[k]()
        except Exception as e:  # noqa: BLE001
            print(f"!! {k} failed: {e}", file=sys.stderr)
