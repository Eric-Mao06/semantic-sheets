from pathlib import Path

import pytest

from semantic_sheets.errors import ValidationFailed
from semantic_sheets.importer import ParseOptions, import_csv, preview, sniff
from semantic_sheets.services import datasets


def test_import_preserves_identifiers_and_multiline(dataset):
    schema = {c["name"]: c for c in dataset.schema_json}
    assert schema["account_code"]["type"] == "text", "leading-zero codes must stay text"
    assert schema["amount"]["type"] == "number"
    assert schema["created"]["type"] == "date"
    assert dataset.row_count == 120
    assert dataset.status == "ready" and dataset.source_checksum


def test_rows_page_and_cell(workspace, dataset):
    page = datasets.rows_page(workspace.id, dataset.id, None, 0, 3, ["account_code", "text"], 100000)
    assert page["columns"] == ["_row_id", "account_code", "text"]
    assert page["rows"][0][1] == "0000"
    assert page["total_count"] == 120 and page["count_status"] == "complete"
    cell = datasets.cell(workspace.id, dataset.id, None, 7, "text")
    assert "\n" in cell["value"], "multiline cells are preserved, never truncated"


def test_malformed_rows_are_rejected_unless_permissive(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("a,b\n1,2\n3,4,5,6,7\n\"unterminated,x\n8,9\n")
    with pytest.raises(ValidationFailed) as e:
        import_csv(p, tmp_path / "base.parquet", tmp_path / "errors.csv", ParseOptions())
    assert e.value.code == "malformed_rows"
    assert (tmp_path / "errors.csv").exists()
    res = import_csv(p, tmp_path / "base.parquet", tmp_path / "errors.csv", ParseOptions(permissive=True))
    assert res.rejected_rows >= 1 and res.row_count >= 1 and res.warnings


def test_row_cap_requires_explicit_truncation(tmp_path, monkeypatch):
    from semantic_sheets import config

    monkeypatch.setattr(config.settings(), "max_rows_per_file", 10)
    p = tmp_path / "big.csv"
    p.write_text("x\n" + "\n".join(str(i) for i in range(50)) + "\n")
    with pytest.raises(ValidationFailed) as e:
        import_csv(p, tmp_path / "b.parquet", tmp_path / "e.csv", ParseOptions())
    assert e.value.code == "row_cap_exceeded"
    res = import_csv(p, tmp_path / "b.parquet", tmp_path / "e.csv", ParseOptions(max_rows=10))
    assert res.row_count == 10 and any("explicit" in w for w in res.warnings)


def test_sniff_tsv(tmp_path):
    p = tmp_path / "t.tsv"
    p.write_text("a\tb\n1\thello world\n2\tx\n")
    s = sniff(p, ParseOptions())
    assert s["Delimiter"] == "\t"
    pv = preview(p, ParseOptions())
    assert [c["name"] for c in pv["columns"]] == ["a", "b"] and len(pv["rows"]) == 2
