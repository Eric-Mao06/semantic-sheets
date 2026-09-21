"""Filesystem layout for source files, immutable columnar data, job checkpoints, views and exports."""

from __future__ import annotations

import os
from pathlib import Path

from .config import settings


def data_dir() -> Path:
    return settings().data_dir


def upload_path(upload_id: str) -> Path:
    return data_dir() / "uploads" / f"{upload_id}.bin"


def dataset_dir(dataset_id: str) -> Path:
    p = data_dir() / "datasets" / dataset_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def dataset_base_parquet(dataset_id: str) -> Path:
    return dataset_dir(dataset_id) / "base.parquet"


def dataset_source_file(dataset_id: str) -> Path:
    return dataset_dir(dataset_id) / "source.bin"


def dataset_error_report(dataset_id: str) -> Path:
    return dataset_dir(dataset_id) / "import_errors.csv"


def job_dir(job_id: str) -> Path:
    p = data_dir() / "jobs" / job_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def step_dir(job_id: str, step_id: str) -> Path:
    p = job_dir(job_id) / step_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def step_input_parquet(job_id: str, step_id: str) -> Path:
    return step_dir(job_id, step_id) / "input.parquet"


def step_chunk_parquet(job_id: str, step_id: str, chunk_index: int) -> Path:
    return step_dir(job_id, step_id) / f"chunk_{chunk_index:06d}.parquet"


def step_chunk_glob(job_id: str, step_id: str) -> str:
    return str(step_dir(job_id, step_id) / "chunk_*.parquet")


def step_chunk_files(job_id: str, step_id: str) -> list[Path]:
    return sorted(step_dir(job_id, step_id).glob("chunk_*.parquet"))


def step_final_parquet(job_id: str, step_id: str) -> Path:
    return step_dir(job_id, step_id) / "final.parquet"


def view_path(view_id: str) -> Path:
    return data_dir() / "views" / f"{view_id}.parquet"


def export_path(export_id: str, fmt: str) -> Path:
    return data_dir() / "exports" / f"{export_id}.{fmt}"


def export_manifest_path(export_id: str) -> Path:
    return data_dir() / "exports" / f"{export_id}.manifest.json"


def atomic_replace(tmp: Path, final: Path) -> None:
    os.replace(tmp, final)


def sql_str(path: Path | str) -> str:
    """Quote a filesystem path for use inside DuckDB SQL."""
    return "'" + str(path).replace("'", "''") + "'"
