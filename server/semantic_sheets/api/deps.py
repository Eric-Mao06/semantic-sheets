from __future__ import annotations

from fastapi import Depends, Header, Query

from ..db import Workspace
from ..services.workspaces import authenticate, require_scope


def current_workspace(authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None),
                      token: str | None = Query(default=None)) -> Workspace:
    """Bearer header normally; ?token= only for browser-initiated GETs (EventSource, downloads)."""
    key = None
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    elif x_api_key:
        key = x_api_key.strip()
    elif token:
        key = token.strip()
    return authenticate(key)


def scoped(scope: str):
    def dep(ws: Workspace = Depends(current_workspace)) -> Workspace:
        require_scope(ws, scope)
        return ws

    return dep
