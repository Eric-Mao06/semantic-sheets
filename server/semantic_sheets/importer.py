"""CSV/TSV import: sample to propose parse options, validate the full parse, assign stable row IDs.

Rules from the design:
- keep the original bytes; record coercion failures; never silently truncate long cells or rows;
- reject malformed rows with a downloadable error report, or import under an explicit permissive mode;
- preserve identifiers (leading zeros) as text; the sniffer runs over the whole file so column types hold.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .config import settings
from .errors import ValidationFailed
from .storage import sql_str

ROW_ID = "_row_id"

DUCK_TO_LOGICAL = {
    "VARCHAR": "text",
    "BIGINT": "integer",
    "INTEGER": "integer",
    "SMALLINT": "integer",
    "TINYINT": "integer",
    "HUGEINT": "integer",
    "UBIGINT": "integer",
    "UINTEGER": "integer",
    "DOUBLE": "number",
    "FLOAT": "number",
    "DECIMAL": "number",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "TIME": "text",
}


def logical_type(duck_type: str) -> str:
    base = duck_type.split("(")[0].upper()
    return DUCK_TO_LOGICAL.get(base, "text")


@dataclass
class ParseOptions:
    delimiter: str | None = None
    header: bool | None = None
    quote: str | None = None
    encoding: str | None = None
    all_text: bool = False
    permissive: bool = False
    max_rows: int | None = None  # explicit truncation requested by the caller
    compression: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ParseOptions":
        d = d or {}
        return cls(
            delimiter=d.get("delimiter"),
            header=d.get("header"),
            quote=d.get("quote"),
            encoding=d.get("encoding"),
            all_text=bool(d.get("all_text", False)),
            permissive=bool(d.get("permissive", False)),
            max_rows=d.get("max_rows"),
            compression=d.get("compression"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ImportResult:
    row_count: int
    schema: list[dict[str, Any]]
    quality: dict[str, Any]
    rejected_rows: int
    parse_options: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    checksum: str = ""
    elapsed_seconds: float = 0.0


def file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_csv_args(opts: ParseOptions, *, sniffed: dict[str, Any] | None) -> str:
    args: list[str] = []
    delim = opts.delimiter or (sniffed or {}).get("Delimiter")
    if delim:
        args.append(f"delim={sql_str(delim)}")
    if opts.quote:
        args.append(f"quote={sql_str(opts.quote)}")
    if opts.header is not None:
        args.append(f"header={'true' if opts.header else 'false'}")
    if opts.encoding:
        args.append(f"encoding={sql_str(opts.encoding)}")
    if opts.compression:
        args.append(f"compression={sql_str(opts.compression)}")
    if opts.all_text:
        args.append("all_varchar=true")
    args.append("sample_size=-1")
    args.append("null_padding=false")
    args.append("strict_mode=true")
    return ", ".join(args)


def sniff(path: Path, opts: ParseOptions) -> dict[str, Any]:
    con = duckdb.connect()
    try:
        extra = []
        if opts.compression:
            extra.append(f"compression={sql_str(opts.compression)}")
        if opts.delimiter:
            extra.append(f"delim={sql_str(opts.delimiter)}")
        if opts.header is not None:
            extra.append(f"header={'true' if opts.header else 'false'}")
        extra_sql = (", " + ", ".join(extra)) if extra else ""
        row = con.execute(f"SELECT * FROM sniff_csv({sql_str(path)}, sample_size=20000{extra_sql})").fetchone()
        cols = [d[0] for d in con.description]
        out = dict(zip(cols, row))
        if opts.delimiter is None and len(out.get("Columns") or []) <= 1:
            # A degenerate dialect (one column) usually means malformed rows confused the sniffer. Prefer the
            # common delimiter that yields the most columns so those rows surface as rejects instead.
            best_n, best_d = len(out.get("Columns") or []), None
            for d in (",", "\t", ";", "|"):
                try:
                    desc = con.execute(f"DESCRIBE SELECT * FROM read_csv({sql_str(path)}, delim={sql_str(d)}, header=true, "
                                       f"ignore_errors=true, sample_size=20000{extra_sql})").fetchall()
                except duckdb.Error:
                    continue
                if len(desc) > best_n:
                    best_n, best_d = len(desc), d
            if best_d is not None:
                out = {**out, "Delimiter": best_d, "Quote": '"', "Columns": [{"name": r[0], "type": r[1]} for r in desc]}
        return out
    finally:
        con.close()


def preview(path: Path, opts: ParseOptions, limit: int = 100) -> dict[str, Any]:
    sniffed = sniff(path, opts)
    con = duckdb.connect()
    try:
        args = _read_csv_args(opts, sniffed=sniffed).replace("sample_size=-1", "sample_size=20000")
        rel = con.execute(f"SELECT * FROM read_csv({sql_str(path)}, {args}, ignore_errors=true) LIMIT {int(limit)}")
        cols = [(d[0], logical_type(str(d[1]))) for d in rel.description]
        rows = [list(_jsonable(v) for v in r) for r in rel.fetchall()]
        return {
            "delimiter": sniffed.get("Delimiter"),
            "header": sniffed.get("HasHeader"),
            "columns": [{"name": n, "type": t} for n, t in cols],
            "rows": rows,
        }
    finally:
        con.close()


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def import_csv(source: Path, base_parquet: Path, error_report: Path, opts: ParseOptions) -> ImportResult:
    """Parse the whole file with the sniffed options, validate it, write base.parquet with stable row IDs."""
    cfg = settings()
    t0 = time.time()
    size = source.stat().st_size
    if size > cfg.max_file_bytes:
        raise ValidationFailed(
            f"File is {size} bytes; the limit is {cfg.max_file_bytes} bytes.", code="file_too_large", path="upload"
        )
    if size == 0:
        raise ValidationFailed("The file is empty.", code="empty_file", path="upload")

    sniffed = sniff(source, opts)
    args = _read_csv_args(opts, sniffed=sniffed)
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=true")
    con.execute("SET threads=4")
    warnings: list[str] = []
    try:
        # Full parse with reject tracking. DuckDB skips rows it cannot parse and records them.
        con.execute(
            f"CREATE TABLE parsed AS SELECT * FROM read_csv({sql_str(source)}, {args}, "
            f"ignore_errors=true, store_rejects=true, rejects_table='rejects', rejects_scan='rejects_scan')"
        )
        rejects = con.execute("SELECT count(*) FROM rejects").fetchone()[0]
        if rejects:
            rows = con.execute(
                "SELECT line, column_name, error_type, csv_line, error_message FROM rejects ORDER BY line LIMIT 10000"
            ).fetchall()
            error_report.parent.mkdir(parents=True, exist_ok=True)
            with open(error_report, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["line", "column", "error_type", "csv_line", "error_message"])
                for r in rows:
                    w.writerow([_jsonable(x) for x in r])
            if not opts.permissive:
                raise ValidationFailed(
                    f"{rejects} malformed row(s) were found. Download the error report, fix the file, "
                    "or import again with permissive=true to skip them.",
                    code="malformed_rows",
                    path="upload",
                    details={"rejected_rows": int(rejects), "error_report": True},
                )
            warnings.append(f"{rejects} malformed row(s) were skipped under permissive mode; see the error report.")

        cols = con.execute("DESCRIBE parsed").fetchall()
        if len(cols) > cfg.max_columns:
            raise ValidationFailed(f"{len(cols)} columns exceed the limit of {cfg.max_columns}.", code="too_many_columns")
        renames: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, *_ in cols:
            new = name
            if new == ROW_ID or new.startswith("_row_id"):
                new = name + "_source"
                warnings.append(f"Column {name!r} was renamed to {new!r}; {ROW_ID!r} is reserved.")
            if new in seen:
                i = 2
                while f"{new}_{i}" in seen:
                    i += 1
                new = f"{new}_{i}"
            seen.add(new)
            if new != name:
                renames.append((name, new))
        for old, new in renames:
            con.execute(f'ALTER TABLE parsed RENAME COLUMN "{_q(old)}" TO "{_q(new)}"')

        total = con.execute("SELECT count(*) FROM parsed").fetchone()[0]
        hard_cap = min(cfg.max_rows_per_file, cfg.hard_max_rows_per_file)
        if opts.max_rows is not None and total > opts.max_rows:
            con.execute(f"CREATE TABLE trimmed AS SELECT * FROM parsed LIMIT {int(opts.max_rows)}")
            con.execute("DROP TABLE parsed")
            con.execute("ALTER TABLE trimmed RENAME TO parsed")
            warnings.append(f"Imported the first {opts.max_rows} of {total} rows as requested (explicit max_rows).")
            total = opts.max_rows
        if total > hard_cap:
            raise ValidationFailed(
                f"The file has {total} rows; the current cap is {hard_cap}. Pass an explicit max_rows to "
                "import a prefix, or raise SS_MAX_ROWS_PER_FILE.",
                code="row_cap_exceeded",
                path="upload",
                details={"row_count": int(total), "cap": hard_cap},
            )
        if total == 0:
            raise ValidationFailed("No data rows were parsed.", code="no_rows", path="upload")

        tmp = base_parquet.with_suffix(".parquet.tmp")
        con.execute(
            f"COPY (SELECT (row_number() OVER ()) - 1 AS {ROW_ID}, * FROM parsed) TO {sql_str(tmp)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 65536)"
        )
        tmp.replace(base_parquet)
        schema, quality = profile(base_parquet)
        return ImportResult(
            row_count=int(total),
            schema=schema,
            quality=quality,
            rejected_rows=int(rejects),
            parse_options={
                **opts.to_dict(),
                "delimiter": opts.delimiter or sniffed.get("Delimiter"),
                "header": opts.header if opts.header is not None else sniffed.get("HasHeader"),
                "quote": opts.quote or sniffed.get("Quote"),
            },
            warnings=warnings,
            checksum=file_checksum(source),
            elapsed_seconds=round(time.time() - t0, 3),
        )
    finally:
        con.close()


def _q(name: str) -> str:
    return name.replace('"', '""')


def profile(base_parquet: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Column schema with null counts, approximate distinct counts, and bounded samples."""
    con = duckdb.connect()
    try:
        con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet({sql_str(base_parquet)})")
        cols = con.execute("DESCRIBE t").fetchall()
        total = con.execute("SELECT count(*) FROM t").fetchone()[0]
        schema: list[dict[str, Any]] = []
        aggs: list[str] = []
        names: list[str] = []
        for name, dtype, *_ in cols:
            if name == ROW_ID:
                continue
            names.append(name)
            lt = logical_type(str(dtype))
            q = f'"{_q(name)}"'
            aggs.append(f"count({q})")
            aggs.append(f"approx_count_distinct({q})")
            if lt == "text":
                aggs.append(f"max(length({q}))")
            else:
                aggs.append("NULL")
            schema.append({"name": name, "type": lt, "duck_type": str(dtype)})
        if aggs:
            stats = con.execute(f"SELECT {', '.join(aggs)} FROM t").fetchone()
            for i, col in enumerate(schema):
                non_null, distinct, max_len = stats[i * 3], stats[i * 3 + 1], stats[i * 3 + 2]
                col["null_count"] = int(total - (non_null or 0))
                col["distinct_estimate"] = int(distinct or 0)
                if max_len is not None:
                    col["max_length"] = int(max_len)
        sample = con.execute(f'SELECT * FROM t ORDER BY {ROW_ID} LIMIT 5').fetchall()
        colnames = [d[0] for d in con.description]
        for col in schema:
            idx = colnames.index(col["name"])
            vals = []
            for r in sample:
                v = _jsonable(r[idx])
                if isinstance(v, str) and len(v) > 80:
                    v = v[:80] + "…"
                vals.append(v)
            col["examples"] = vals
        quality = {
            "row_count": int(total),
            "column_count": len(schema),
            "columns_with_nulls": sum(1 for c in schema if c.get("null_count")),
            "text_columns": sum(1 for c in schema if c["type"] == "text"),
        }
        return schema, quality
    finally:
        con.close()


def bounded_sample(base_parquet: Path, columns: list[str], max_rows: int = 20, max_bytes: int = 8192) -> list[dict[str, Any]]:
    """At most `max_rows` rows and `max_bytes` of serialized JSON, spread across the table."""
    con = duckdb.connect()
    try:
        total = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(base_parquet)})").fetchone()[0]
        if total == 0:
            return []
        step = max(1, total // max_rows)
        sel = ", ".join(f'"{_q(c)}"' for c in columns) if columns else "*"
        rows = con.execute(
            f"SELECT {ROW_ID}, {sel} FROM read_parquet({sql_str(base_parquet)}) WHERE {ROW_ID} % {step} = 0 "
            f"ORDER BY {ROW_ID} LIMIT {int(max_rows)}"
        ).fetchall()
        names = [d[0] for d in con.description]
        out: list[dict[str, Any]] = []
        used = 0
        per_cell = max(64, max_bytes // max(1, max_rows * max(1, len(names))))
        for r in rows:
            d: dict[str, Any] = {}
            for n, v in zip(names, r):
                v = _jsonable(v)
                if isinstance(v, str) and len(v) > per_cell * 4:
                    v = v[: per_cell * 4] + "…"
                d[n] = v
            s = len(json.dumps(d, ensure_ascii=False))
            if used + s > max_bytes and out:
                break
            used += s
            out.append(d)
        return out
    finally:
        con.close()
