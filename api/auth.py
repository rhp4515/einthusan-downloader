"""Static API-key authentication dependency."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.api_key
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "Missing or invalid X-Api-Key header"}},
        )
