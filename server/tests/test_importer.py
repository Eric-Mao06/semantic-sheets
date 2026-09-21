from pathlib import Path

import duckdb
import pytest

from semsheet.importer import ImportError_, ImportOptions, import_file, preview_rows, sanitize_column_names, sniff


def test_sanitize_column_names_is_stable_and_unique():
    names = sanitize_column_names(["Date received", "Date received", "", "1abc", "ok_name", "_row_id"])
    assert len(set(names)) == len(names)
    assert all(n and "." not in n for n in names)
    assert "ok_name" in names and names[2] == "column_3"
    assert sanitize_column_names(["a.b", "a.b"]) == ["a_b", "a_b_2"]


def test_sniff_tsv_and_types(tmp_path: Path):
    p = tmp_path / "t.tsv"
    p.write_text("id\tamount\tprice\tnote\tflag\n1\t10\t1.5\thello\ttrue\n2\t20\t2.25\tworld\tfalse\n3\t\t3.0\t\"multi\nline\"\ttrue\n", encoding="utf-8")
    delimiter, header, _ = sniff(p, ImportOptions())
    assert delimiter == "\t" and header is True
    schema, report = import_file(p, tmp_path / "t.parquet")
    types = {c["name"]: c["type"] for c in schema}
    assert types["id"] == "integer" and types["amount"] == "integer" and types["price"] == "double" and types["flag"] == "boolean"
    assert types["note"] == "text"
    assert report.row_count == 3 and report.rejected_rows == 0
    rows = duckdb.sql(f"select _row_id, note from read_parquet('{tmp_path / 't.parquet'}') order by _row_id").fetchall()
    assert [r[0] for r in rows] == [0, 1, 2]
    assert "multi\nline" in rows[2][1]


def test_float_like_ints_are_not_integers(tmp_path: Path):
    p = tmp_path / "geo.csv"
    p.write_text("lat,lon,count\n40.7,-73.9,3\n40.8,-74.0,4\n", encoding="utf-8")
    schema, _ = import_file(p, tmp_path / "geo.parquet")
    types = {c["name"]: c["type"] for c in schema}
    assert types == {"lat": "double", "lon": "double", "count": "integer", "_row_id": "integer"}


def test_malformed_rows_are_reported_not_silently_dropped(tmp_path: Path):
    p = tmp_path / "bad.csv"
    p.write_text("a,b\n1,x\n2,y,extra\n3,z\n", encoding="utf-8")
    with pytest.raises(ImportError_) as e:
        import_file(p, tmp_path / "bad.parquet")
    assert e.value.code == "malformed_rows" and e.value.report["rejected_rows"] == 1
    schema, report = import_file(p, tmp_path / "bad2.parquet", ImportOptions(permissive=True))
    assert report.row_count == 2 and report.rejected_rows == 1 and report.rejects[0]["line"] == 3


def test_explicit_row_limit_is_recorded(tmp_path: Path):
    p = tmp_path / "many.csv"
    p.write_text("x\n" + "\n".join(str(i) for i in range(50)) + "\n", encoding="utf-8")
    _, report = import_file(p, tmp_path / "many.parquet", ImportOptions(row_limit=10))
    assert report.row_count == 10 and report.truncated_to == 10


def test_empty_file_rejected(tmp_path: Path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ImportError_):
        import_file(p, tmp_path / "empty.parquet")


def test_preview_is_bounded(tmp_path: Path):
    p = tmp_path / "prev.csv"
    p.write_text("a,b\n" + "\n".join(f"{i},{i * 2}" for i in range(500)) + "\n", encoding="utf-8")
    prev = preview_rows(p, limit=20)
    assert len(prev["rows"]) == 20 and prev["columns"] == ["a", "b"]
