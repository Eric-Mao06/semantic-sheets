"""Prepare demo sample datasets from the public test datasets listed in the design.

Downloads (or reuses) the source files, converts them to plain CSV, and writes bounded samples with a
metadata sidecar into <SS_DATA_DIR>/samples (default server/data/samples).

Usage: python scripts/prepare_samples.py [--source-dir DIR] [--out DIR] [--rows N]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import duckdb

SOURCES = {
    "bitext.csv": "https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset/resolve/main/Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv?download=true",
    "complaints.csv.zip": "https://files.consumerfinance.gov/ccdb/complaints.csv.zip",
    "reviews.csv.gz": "https://data.insideairbnb.com/united-states/ny/new-york-city/2026-06-14/data/reviews.csv.gz",
    "listings.csv.gz": "https://data.insideairbnb.com/united-states/ny/new-york-city/2026-06-14/data/listings.csv.gz",
    "banking77_train.csv": "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/train.csv",
    "banking77_test.csv": "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/test.csv",
    "wdc80pair.zip": "https://data.dws.informatik.uni-mannheim.de/largescaleproductcorpus/data/wdc-products/80pair.zip",
    "online_retail_ii.zip": "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip",
}


def fetch(src_dir: Path, name: str) -> Path:
    p = src_dir / name
    if p.exists() and p.stat().st_size > 0:
        return p
    print(f"downloading {name} …", file=sys.stderr)
    subprocess.run(["curl", "-sSL", "-o", str(p), SOURCES[name]], check=True)
    return p


def q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def write(con: duckdb.DuckDBPyConnection, sql: str, out: Path, meta: dict) -> None:
    con.execute(f"COPY ({sql}) TO {q(out)} (FORMAT CSV, HEADER TRUE)")
    n = con.execute(f"SELECT count(*) FROM read_csv({q(out)})").fetchone()[0]
    meta["rows"] = int(n)
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    print(f"  {out.name}: {n} rows", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default=os.environ.get("SS_SAMPLE_SOURCES", "data/sources"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("SS_DATA_DIR", "server/data"), "samples"))
    ap.add_argument("--rows", type=int, default=5000, help="rows per demo sample (inference demo range is 1,000-10,000)")
    ap.add_argument("--big-rows", type=int, default=100000, help="rows for the large scale fixtures")
    ap.add_argument("--skip-large", action="store_true", help="skip the 345 MB CFPB download")
    args = ap.parse_args()
    src = Path(args.source_dir); src.mkdir(parents=True, exist_ok=True)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=true")

    # 1. Bitext customer support (intent labels + responses kept in the file; exclude them from model inputs when evaluating)
    p = fetch(src, "bitext.csv")
    write(con, f"SELECT flags, instruction, category, intent, response FROM read_csv({q(p)}) USING SAMPLE reservoir({args.rows} ROWS) REPEATABLE (42)",
          out / "bitext_support.csv",
          {"title": "Bitext customer support (sample)", "source": "Bitext customer support LLM chatbot training dataset",
           "description": f"{args.rows} of 26,872 synthetic support requests with intent labels and responses. Test classification, semantic filtering and typo handling; the label columns are held out from model inputs.",
           "suggested_requests": ["Find customers trying to cancel an order because they cannot afford it",
                                  "Classify each request by intent using the same taxonomy as the intent column, then count agreement per category"]})
    write(con, f"SELECT flags, instruction, category, intent, response FROM read_csv({q(p)})", out / "bitext_support_full.csv",
          {"title": "Bitext customer support (full 26,872 rows)", "source": "Bitext", "description": "The full 26,872-row file for larger scans.",
           "suggested_requests": ["Find customers trying to cancel an order because they cannot afford it"]})

    # 2. BANKING77
    tr = fetch(src, "banking77_train.csv"); te = fetch(src, "banking77_test.csv")
    write(con, f"SELECT text, category FROM read_csv({q(te)})", out / "banking77_test.csv",
          {"title": "BANKING77 test queries", "source": "PolyAI BANKING77", "description": "3,080 banking queries across 77 labeled intents. Test classification accuracy and closely related meanings.",
           "suggested_requests": ["Separate pending transfers, failed transfers, and transfers sent to the wrong account"]})
    write(con, f"SELECT text, category FROM read_csv({q(tr)})", out / "banking77_train.csv",
          {"title": "BANKING77 training queries", "source": "PolyAI BANKING77", "description": "10,003 banking queries across 77 intents.",
           "suggested_requests": ["Separate pending transfers, failed transfers, and transfers sent to the wrong account"]})

    # 3. Inside Airbnb NYC reviews + listings
    rv = fetch(src, "reviews.csv.gz"); ls = fetch(src, "listings.csv.gz")
    write(con, f"SELECT listing_id, id AS review_id, date, reviewer_name, comments FROM read_csv({q(rv)}) WHERE comments IS NOT NULL AND length(comments) > 20 USING SAMPLE reservoir({args.rows} ROWS) REPEATABLE (7)",
          out / "airbnb_reviews.csv",
          {"title": "Inside Airbnb NYC reviews (sample)", "source": "Inside Airbnb, New York City, 14 June 2026 snapshot",
           "description": f"{args.rows} guest reviews (multilingual). Test semantic classification and joining results to listing metadata.",
           "suggested_requests": ["Find reviews reporting unreliable Wi-Fi, group by property, and compare nightly prices"]})
    write(con, f"SELECT id AS listing_id, name, host_id, neighbourhood_cleansed AS neighbourhood, room_type, price, number_of_reviews, review_scores_rating FROM read_csv({q(ls)}, ignore_errors=true)",
          out / "airbnb_listings.csv",
          {"title": "Inside Airbnb NYC listings", "source": "Inside Airbnb", "description": "Property listings with price and room type; join target for the reviews sample (listing_id).",
           "suggested_requests": ["Count listings by room type and average price"]})

    # 4. WDC product matching (JSONL inside zip -> CSV pairs; also a left/right offer table for semantic_match)
    z = fetch(src, "wdc80pair.zip")
    with zipfile.ZipFile(z) as zf:
        names = [n for n in zf.namelist() if n.endswith(".json.gz") or n.endswith(".jsonl") or n.endswith(".json")]
        test = [n for n in names if "test" in n.lower()]
        pick = test[0] if test else names[0]
        data = zf.read(pick)
        tmp = src / "wdc_pairs.json.gz" if pick.endswith(".gz") else src / "wdc_pairs.json"
        tmp.write_bytes(data)
    con.execute(f"CREATE OR REPLACE TABLE wdc AS SELECT * FROM read_json({q(tmp)}, format='newline_delimited', ignore_errors=true)")
    cols = [c[0] for c in con.execute("DESCRIBE wdc").fetchall()]
    def pickcol(*cands):
        for c in cands:
            if c in cols:
                return c
        return None
    tl, tr_ = pickcol("title_left"), pickcol("title_right")
    bl, br = pickcol("brand_left"), pickcol("brand_right")
    lab = pickcol("label")
    idl, idr = pickcol("id_left"), pickcol("id_right")
    write(con, f"SELECT {idl} AS id_left, {tl} AS title_left, {bl} AS brand_left, {idr} AS id_right, {tr_} AS title_right, {br} AS brand_right, {lab} AS label FROM wdc LIMIT {args.rows}",
          out / "wdc_product_pairs.csv",
          {"title": "WDC product matching pairs", "source": "Web Data Commons product corpus (80pair test split)",
           "description": "Product offer pairs with match labels and hard non-matches. Test semantic matching precision and recall (label held out).",
           "suggested_requests": ["Decide whether the left and right offers are the same exact product, distinguishing sizes and model variants"]})
    write(con, f"SELECT DISTINCT {idl} AS offer_id, {tl} AS title, {bl} AS brand FROM wdc WHERE {tl} IS NOT NULL LIMIT 3000", out / "wdc_offers_left.csv",
          {"title": "WDC offers (left)", "source": "Web Data Commons", "description": "Left-hand product offers for a semantic join against wdc_offers_right.",
           "suggested_requests": ["Match offers for the same exact product in the right table despite different titles"]})
    write(con, f"SELECT DISTINCT {idr} AS offer_id, {tr_} AS title, {br} AS brand FROM wdc WHERE {tr_} IS NOT NULL LIMIT 3000", out / "wdc_offers_right.csv",
          {"title": "WDC offers (right)", "source": "Web Data Commons", "description": "Right-hand product offers (matching reference table).", "suggested_requests": []})

    # 5. Online Retail II (XLSX inside zip -> CSV)
    z = fetch(src, "online_retail_ii.zip")
    with zipfile.ZipFile(z) as zf:
        xl = [n for n in zf.namelist() if n.lower().endswith(".xlsx")][0]
        xp = src / "online_retail_II.xlsx"
        if not xp.exists():
            xp.write_bytes(zf.read(xl))
    try:
        con.execute("INSTALL excel; LOAD excel;")
        con.execute(f"CREATE OR REPLACE TABLE retail AS SELECT * FROM read_xlsx({q(xp)}, sheet='Year 2010-2011', all_varchar=true) UNION ALL SELECT * FROM read_xlsx({q(xp)}, sheet='Year 2009-2010', all_varchar=true)")
    except Exception as e:  # noqa: BLE001
        print(f"  excel extension unavailable ({e}); converting with openpyxl", file=sys.stderr)
        import csv
        import openpyxl
        wb = openpyxl.load_workbook(xp, read_only=True)
        tmpcsv = src / "online_retail_II.csv"
        with open(tmpcsv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            first = True
            for ws in wb.worksheets:
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i == 0:
                        if first:
                            w.writerow(row); first = False
                        continue
                    w.writerow(row)
        con.execute(f"CREATE OR REPLACE TABLE retail AS SELECT * FROM read_csv({q(tmpcsv)}, all_varchar=true)")
    write(con, f"SELECT * FROM retail WHERE Description IS NOT NULL LIMIT {args.big_rows}", out / "online_retail_100k.csv",
          {"title": "Online Retail II (first 100,000 transactions)", "source": "UCI Online Retail II",
           "description": "Retail transactions with product descriptions, quantities, prices and dates. Test scrolling, sorting, aggregation and product categorization at 100k rows.",
           "suggested_requests": ["Classify products into gift categories, then calculate revenue by category and country"]})
    write(con, f"SELECT * FROM retail WHERE Description IS NOT NULL USING SAMPLE reservoir({args.rows} ROWS) REPEATABLE (3)", out / "online_retail_sample.csv",
          {"title": "Online Retail II (sample)", "source": "UCI Online Retail II", "description": f"{args.rows} transactions for the inference demo.",
           "suggested_requests": ["Classify products into gift categories, then calculate revenue by category and country"]})

    # 6. CFPB complaints (large; optional)
    if not args.skip_large:
        z = fetch(src, "complaints.csv.zip")
        with zipfile.ZipFile(z) as zf:
            inner = [n for n in zf.namelist() if n.endswith(".csv")][0]
            cp = src / "complaints.csv"
            if not cp.exists():
                with zf.open(inner) as fsrc, open(cp, "wb") as fdst:
                    while True:
                        b = fsrc.read(1 << 22)
                        if not b:
                            break
                        fdst.write(b)
        cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_csv({q(cp)}, ignore_errors=true, sample_size=2000)").fetchall()]
        if "Consumer complaint narrative" in cols:
            narrative = '"Consumer complaint narrative"'
            note = "Complaints with consumer narratives."
        else:
            # The 2026 public export dropped the narrative column; use the issue text so long-text tests still run.
            narrative = "concat_ws(' — ', Issue, \"Sub-issue\", \"Company public response\")"
            note = "The current public export has no narrative column; the text field is Issue + Sub-issue + company response."
        base = (f"SELECT \"Date received\" AS date_received, Product AS product, \"Sub-product\" AS sub_product, Issue AS issue, "
                f"{narrative} AS narrative, Company AS company, State AS state, \"Complaint ID\" AS complaint_id "
                f"FROM read_csv({q(cp)}, ignore_errors=true) WHERE {narrative} IS NOT NULL AND length({narrative}) > 40")
        write(con, f"{base} USING SAMPLE reservoir({args.rows} ROWS) REPEATABLE (11)", out / "cfpb_complaints.csv",
              {"title": "CFPB consumer complaints (sample)", "source": "Consumer Financial Protection Bureau complaint database",
               "description": f"{args.rows} complaints. Test long-text processing and urgency scoring. " + note,
               "suggested_requests": ["Find complaints about charges continuing after cancellation, then rank by urgency"]})
        write(con, f"{base} LIMIT {args.big_rows}", out / "cfpb_complaints_100k.csv",
              {"title": "CFPB consumer complaints (100,000 rows)", "source": "CFPB", "description": "100,000 complaints for background-scale runs. " + note,
               "suggested_requests": ["Find complaints about charges continuing after cancellation, then rank by urgency"]})
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
