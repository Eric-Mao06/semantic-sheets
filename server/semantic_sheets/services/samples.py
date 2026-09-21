"""Sample datasets shipped with the demo (prepared CSVs in data/samples or the repo's samples folder)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import settings
from ..errors import NotFound
from . import datasets as ds_service


def sample_dirs() -> list[Path]:
    out = [settings().data_dir / "samples"]
    repo = Path(__file__).resolve().parents[3] / "samples"
    if repo.exists():
        out.append(repo)
    return out


def list_samples() -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for d in sample_dirs():
        if not d.exists():
            continue
        for p in sorted(d.glob("*.csv")):
            meta_path = p.with_suffix(".json")
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            seen.setdefault(p.stem, {"name": p.stem, "title": meta.get("title", p.stem), "description": meta.get("description", ""),
                                     "bytes": p.stat().st_size, "suggested_requests": meta.get("suggested_requests", []),
                                     "source": meta.get("source", "")})
    return list(seen.values())


def import_sample(workspace_id: str, name: str):
    for d in sample_dirs():
        p = d / f"{name}.csv"
        if p.exists():
            meta_path = p.with_suffix(".json")
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            return ds_service.import_local_file(workspace_id, p, meta.get("title", name))
    raise NotFound(f"sample {name!r} not found", code="sample_not_found")
