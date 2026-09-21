"""Structured errors shared by the API and the MCP adapter."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, path: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.path = path
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            d["path"] = self.path
        if self.details is not None:
            d["details"] = self.details
        return d


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class BudgetExceeded(AppError):
    status_code = 402
    code = "budget_exceeded"
