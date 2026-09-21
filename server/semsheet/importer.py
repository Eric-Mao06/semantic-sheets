"""CSV / TSV import.

Samples the file to propose delimiter and types, validates the full parse, preserves identifiers such as
leading-zero codes as text, records coercion failures, and assigns stable row IDs (`_row_id`) in file order.
The original bytes are kept next to the Parquet snapshot."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .config import settings

ROW_ID = "_row_id"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class ImportOptions:
    delimiter: str | None = None
    header: bool | None = None
    encoding: str = "utf-8"
    permissive: bool = False
    row_limit: int | None = None  # explicit truncation only; never silent
    quote: str = '"'
    null_strings: list[str] = field(default_factory=lambda: ["", "NA", "N/A", "null", "NULL", "None"])


@dataclass
class ImportReport:
    delimiter: str
    header: bool
    row_count: int
    rejected_rows: int
    rejects: list[dict[str, Any]]
    coercions: dict[str, dict[str, Any]]
    warnings: list[str]
    truncated_to: int | None


class ImportError_(Exception):
    def __init__(self, code: str, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.report = report or {}


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sanitize_column_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, raw in enumerate(names):
        n = (raw or "").strip().replace("\ufeff", "")
        if not n:
            n = f"column_{i + 1}"
        n = re.sub(r"\s+", " ", n)
        n = n.replace(".", "_")  # dots are reserved for semantic output columns
        base = n
        k = seen.get(base.lower(), 0)
        if k:
            n = f"{base}_{k + 1}"
        seen[base.lower()] = k + 1
        out.append(n)
    return out


def sniff(path: Path, options: ImportOptions) -> tuple[str, bool, list[str]]:
    """Return (delimiter, has_header, raw_header_names) using a bounded byte sample."""
    with duckdb.connect() as con:
        try:
            row = con.execute(f"SELECT Delimiter, HasHeader, Columns FROM sniff_csv({_sql_str(str(path))}, sample_size=2000)").fetchone()
        except duckdb.Error:
            row = None
    delimiter = options.delimiter
    header = options.header
    opener = open if path.suffix != ".gz" else __import__("gzip").open
    with opener(path, "rt", encoding=options.encoding, errors="replace") as fh:  # type: ignore[operator]
        sample = fh.read(64 * 1024)
    first_line = next((ln for ln in sample.splitlines() if ln.strip()), "")
    if row is not None:
        # DuckDB's sniffer can settle on a delimiter that never occurs (one column per line) when the file is
        # inconsistent; only trust it when the delimiter actually appears in the header line.
        if not delimiter and row[0] and (row[0] in first_line or len(first_line) == 0):
            delimiter = row[0]
        header = header if header is not None else bool(row[1])
    if not delimiter:
        # Prefer the candidate that splits the header (first line) into the most fields and appears on the
        # majority of sampled lines; fall back to the extension-based default.
        lines = [ln for ln in sample.splitlines() if ln.strip()][:200]
        best: tuple[int, int, str] | None = None
        for cand in (",", "\t", ";", "|"):
            if not lines or cand not in lines[0]:
                continue
            consistent = sum(1 for ln in lines[1:] if ln.count(cand) == lines[0].count(cand))
            score = (consistent, lines[0].count(cand), cand)
            if best is None or score[:2] > best[:2]:
                best = score
        delimiter = best[2] if best else ("\t" if path.suffix.lower() in (".tsv", ".tab") else ",")
    if header is None:
        header = True
    return delimiter, header, []


def header_names(path: Path, delimiter: str, header: bool, options: ImportOptions) -> list[str]:
    """Column names from the first record (or positional names when there is no header), read with the
    standard csv module so that the parse below can run with an explicit, sniff-free column list."""
    opener = open if path.suffix != ".gz" else __import__("gzip").open
    with opener(path, "rt", encoding=options.encoding, errors="replace", newline="") as fh:  # type: ignore[operator]
        reader = csv.reader(fh, delimiter=delimiter, quotechar=options.quote)
        try:
            first = next(reader)
        except StopIteration:
            return []
    if header:
        return [c if c is not None else "" for c in first]
    return [f"column_{i + 1}" for i in range(len(first))]


def _read_csv_sql(path: Path, delimiter: str, header: bool, options: ImportOptions, rejects: bool, columns: list[str] | None = None) -> str:
    nulls = "[" + ",".join(_sql_str(n) for n in options.null_strings) + "]"
    parts = [
        _sql_str(str(path)),
        f"delim={_sql_str(delimiter)}",
        f"header={'true' if header else 'false'}",
        "all_varchar=true",
        f"quote={_sql_str(options.quote)}",
        f"escape={_sql_str(options.quote)}",
        f"nullstr={nulls}",
        "sample_size=-1",
        "strict_mode=true",
    ]
    if columns:
        # Explicit column list: no dialect sniffing, so malformed rows are rejected and reported instead of
        # changing how the whole file is read.
        cols = ", ".join(f"{_sql_str(c)}: 'VARCHAR'" for c in columns)
        parts += ["auto_detect=false", f"columns={{{cols}}}"]
    if rejects:
        parts += ["ignore_errors=true", "store_rejects=true"]
    return f"read_csv({', '.join(parts)})"


def _detect_types(con: duckdb.DuckDBPyConnection, rel_name: str, columns: list[str]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Detect types from full data: only promote when 100% of non-null values cast. Leading-zero codes stay text."""
    types: dict[str, str] = {}
    coercions: dict[str, dict[str, Any]] = {}
    for col in columns:
        c = _q(col)
        stats = con.execute(
            f"""
            SELECT count({c}) AS non_null,
                   count({c}) FILTER (WHERE TRY_CAST({c} AS BIGINT) IS NOT NULL AND regexp_matches(trim({c}), '^[+-]?(0|[1-9][0-9]*)$')) AS ints,
                   count({c}) FILTER (WHERE TRY_CAST({c} AS DOUBLE) IS NOT NULL AND NOT regexp_matches({c}, '^[+-]?0[0-9]')) AS doubles,
                   count({c}) FILTER (WHERE TRY_CAST({c} AS DATE) IS NOT NULL) AS dates,
                   count({c}) FILTER (WHERE TRY_CAST({c} AS TIMESTAMP) IS NOT NULL) AS timestamps,
                   count({c}) FILTER (WHERE lower({c}) IN ('true','false','yes','no','t','f')) AS bools,
                   max(length({c})) AS max_len
            FROM {rel_name}
            """
        ).fetchone()
        non_null, ints, doubles, dates, timestamps, bools, max_len = stats
        if non_null == 0:
            types[col] = "text"
            continue
        if ints == non_null:
            types[col] = "integer"
        elif doubles == non_null:
            types[col] = "double"
        elif dates == non_null and (max_len or 0) <= 10:
            types[col] = "date"
        elif timestamps == non_null:
            types[col] = "timestamp"
        elif bools == non_null:
            types[col] = "boolean"
        else:
            types[col] = "text"
            best = max(("integer", ints), ("double", doubles), ("date", dates), ("timestamp", timestamps), key=lambda t: t[1])
            if best[1] >= 0.9 * non_null:
                coercions[col] = {
                    "kept_as": "text",
                    "candidate_type": best[0],
                    "non_conforming": int(non_null - best[1]),
                    "note": "column kept as text because some values did not parse as " + best[0],
                }
    return types, coercions


_CAST = {
    "integer": "BIGINT",
    "double": "DOUBLE",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "boolean": "BOOLEAN",
    "text": "VARCHAR",
}


def import_file(source_path: Path, dest_parquet: Path, options: ImportOptions | None = None) -> tuple[list[dict[str, Any]], ImportReport]:
    """Validate and convert a delimited file to a typed Parquet snapshot. Returns (schema, report)."""
    options = options or ImportOptions()
    if source_path.stat().st_size > settings.max_file_bytes:
        raise ImportError_("file_too_large", f"File exceeds {settings.max_file_bytes} bytes")
    if source_path.stat().st_size == 0:
        raise ImportError_("empty_file", "The file is empty")
    delimiter, header, _ = sniff(source_path, options)
    warnings: list[str] = []

    with duckdb.connect() as con:
        con.execute("SET preserve_insertion_order=true")
        con.execute("SET threads=4")
        # Full parse with rejects captured so we can report malformed rows.
        try:
            names = header_names(source_path, delimiter, header, options)
            if not names:
                raise ImportError_("empty_file", "The file has no header row")
            if len(set(n.lower() for n in names)) != len(names) or any(not n.strip() for n in names):
                names = sanitize_column_names(names)
            con.execute(f"CREATE TEMP TABLE raw AS SELECT * FROM {_read_csv_sql(source_path, delimiter, header, options, rejects=True, columns=names)}")
        except duckdb.Error as e:
            raise ImportError_("parse_failed", f"Could not parse file: {e}")
        rejects_rows: list[dict[str, Any]] = []
        try:
            rejects_rows = [
                {"line": r[0], "column": r[1], "error_type": r[2], "error": r[3]}
                for r in con.execute("SELECT line, column_name, error_type, error_message FROM reject_errors LIMIT 200").fetchall()
            ]
            rejected_total = con.execute("SELECT count(*) FROM reject_errors").fetchone()[0]
        except duckdb.Error:
            rejected_total = 0
        if rejected_total and not options.permissive:
            raise ImportError_(
                "malformed_rows",
                f"{rejected_total} malformed row(s). Fix the file or import in permissive mode.",
                {"rejected_rows": int(rejected_total), "rejects": rejects_rows, "delimiter": delimiter},
            )

        raw_columns = [d[0] for d in con.execute("SELECT * FROM raw LIMIT 0").description]
        columns = sanitize_column_names(raw_columns)
        if columns != raw_columns:
            warnings.append("column names were normalised (dots replaced, duplicates suffixed, blanks named)")
            for old, new in zip(raw_columns, columns):
                if old != new:
                    con.execute(f"ALTER TABLE raw RENAME COLUMN {_q(old)} TO {_q(new)}")
        if ROW_ID in columns:
            raise ImportError_("reserved_column", f"Column name {ROW_ID} is reserved")

        total = con.execute("SELECT count(*) FROM raw").fetchone()[0]
        if total == 0:
            raise ImportError_("empty_file", "The file has a header but no data rows")
        truncated_to = None
        if total > settings.max_import_rows:
            if options.row_limit is None or options.row_limit > settings.max_import_rows:
                raise ImportError_(
                    "too_many_rows",
                    f"File has {total} rows; the current cap is {settings.max_import_rows}. Set row_limit to import a bounded prefix explicitly.",
                    {"row_count": int(total), "cap": settings.max_import_rows},
                )
        if options.row_limit is not None and total > options.row_limit:
            truncated_to = options.row_limit
            warnings.append(f"imported the first {options.row_limit} of {total} rows (explicit row_limit)")
            con.execute(f"CREATE TEMP TABLE raw2 AS SELECT * FROM raw LIMIT {int(options.row_limit)}")
            con.execute("DROP TABLE raw")
            con.execute("ALTER TABLE raw2 RENAME TO raw")
            total = options.row_limit

        # Trim surrounding whitespace before type detection; keep internal content untouched.
        types, coercions = _detect_types(con, "raw", columns)
        select_parts = [f"CAST(row_number() OVER () - 1 AS BIGINT) AS {_q(ROW_ID)}"]
        for col in columns:
            t = types[col]
            c = _q(col)
            if t == "boolean":
                select_parts.append(f"CASE WHEN lower({c}) IN ('true','yes','t') THEN true WHEN lower({c}) IN ('false','no','f') THEN false END AS {c}")
            elif t == "text":
                select_parts.append(f"{c}")
            else:
                select_parts.append(f"TRY_CAST({c} AS {_CAST[t]}) AS {c}")
        dest_parquet.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT {', '.join(select_parts)} FROM raw) TO {_sql_str(str(dest_parquet))} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000)")

        schema: list[dict[str, Any]] = [{"name": ROW_ID, "type": "integer", "role": "row_id"}]
        for col in columns:
            c = _q(col)
            st = con.execute(
                f"SELECT count(*) - count({c}) AS nulls, approx_count_distinct({c}) AS distinct_est, max(length({c})) AS max_len FROM raw"
            ).fetchone()
            samples = [r[0] for r in con.execute(f"SELECT {c} FROM raw WHERE {c} IS NOT NULL LIMIT 3").fetchall()]
            schema.append(
                {
                    "name": col,
                    "type": types[col],
                    "role": "source",
                    "null_count": int(st[0]),
                    "distinct_estimate": int(st[1]),
                    "max_length": int(st[2] or 0),
                    "samples": [str(s)[:80] for s in samples],
                }
            )

    report = ImportReport(
        delimiter=delimiter,
        header=header,
        row_count=int(total),
        rejected_rows=int(rejected_total),
        rejects=rejects_rows,
        coercions=coercions,
        warnings=warnings,
        truncated_to=truncated_to,
    )
    return schema, report


def checksum_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def preview_rows(path: Path, options: ImportOptions | None = None, limit: int = 100) -> dict[str, Any]:
    """Bounded provisional preview used before the canonical import completes."""
    options = options or ImportOptions()
    delimiter, header, _ = sniff(path, options)
    with duckdb.connect() as con:
        names = header_names(path, delimiter, header, options)
        rel = con.execute(f"SELECT * FROM {_read_csv_sql(path, delimiter, header, options, rejects=True, columns=sanitize_column_names(names))} LIMIT {int(limit)}")
        cols = [d[0] for d in rel.description]
        rows = rel.fetchall()
    return {"delimiter": delimiter, "header": header, "columns": sanitize_column_names(cols), "rows": [list(r) for r in rows], "provisional": True}


def copy_source(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def report_to_dict(report: ImportReport) -> dict[str, Any]:
    return {
        "delimiter": report.delimiter,
        "header": report.header,
        "row_count": report.row_count,
        "rejected_rows": report.rejected_rows,
        "rejects": report.rejects,
        "coercions": report.coercions,
        "warnings": report.warnings,
        "truncated_to": report.truncated_to,
    }


def write_error_report(report: dict[str, Any], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["line", "column", "error_type", "error"])
    for r in report.get("rejects", []):
        w.writerow([r.get("line"), r.get("column"), r.get("error_type"), r.get("error")])
    dest.write_text(buf.getvalue(), encoding="utf-8")
    (dest.with_suffix(".json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
