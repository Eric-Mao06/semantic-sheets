"""Run the API process: `python -m semsheet.main` (or uvicorn semsheet.api:app)."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run("semsheet.api:app", host=os.environ.get("SEMSHEET_HOST", "0.0.0.0"), port=int(os.environ.get("SEMSHEET_PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
