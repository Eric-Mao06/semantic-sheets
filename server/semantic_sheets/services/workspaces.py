"""Workspaces, API keys and scopes. Possession of an opaque object ID grants no access."""

from __future__ import annotations

import secrets

from sqlalchemy import select

from ..config import settings
from ..db import Workspace, hash_key, session
from ..errors import Forbidden, Unauthorized
from ..ids import new_id

ALL_SCOPES = ["read", "run", "import", "export", "delete"]


def ensure_default_workspace() -> tuple[Workspace, str | None]:
    """Create the dev workspace on first start. Returns (workspace, plaintext key or None if pre-existing)."""
    cfg = settings()
    with session() as s:
        ws = s.scalars(select(Workspace).order_by(Workspace.created_at)).first()
        if ws is not None:
            if cfg.dev_workspace_key and ws.api_key_hash != hash_key(cfg.dev_workspace_key):
                ws.api_key_hash = hash_key(cfg.dev_workspace_key)
                s.commit()
                return ws, cfg.dev_workspace_key
            return ws, None
        key = cfg.dev_workspace_key or ("ss_" + secrets.token_urlsafe(24))
        ws = Workspace(id=new_id("ws"), name="default", api_key_hash=hash_key(key), scopes=ALL_SCOPES,
                       budget_usd=cfg.workspace_budget_usd)
        s.add(ws)
        s.commit()
        return ws, key


def create_workspace(name: str, budget_usd: float | None = None, scopes: list[str] | None = None) -> tuple[Workspace, str]:
    key = "ss_" + secrets.token_urlsafe(24)
    with session() as s:
        ws = Workspace(id=new_id("ws"), name=name, api_key_hash=hash_key(key), scopes=scopes or ALL_SCOPES,
                       budget_usd=budget_usd if budget_usd is not None else settings().workspace_budget_usd)
        s.add(ws)
        s.commit()
        return ws, key


def authenticate(token: str | None) -> Workspace:
    if not token:
        raise Unauthorized("missing bearer token")
    with session() as s:
        ws = s.scalars(select(Workspace).where(Workspace.api_key_hash == hash_key(token))).first()
        if ws is None:
            raise Unauthorized("invalid API key")
        return ws


def require_scope(ws: Workspace, scope: str) -> None:
    if scope not in (ws.scopes or []):
        raise Forbidden(f"this workspace key lacks the {scope!r} scope", code="missing_scope")
