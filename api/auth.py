"""Static API-key authentication dependency."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

# Declared as a security scheme (not a bare Header) so it shows up under
# components.securitySchemes in the OpenAPI document — generated clients then
# wire the header up themselves instead of treating it as a manual parameter.
api_key_header = APIKeyHeader(
    name="X-Api-Key",
    auto_error=False,
    description="Static API key issued via the EINTHUSAN_API_KEY environment variable.",
)


def require_api_key(request: Request, x_api_key: str | None = Security(api_key_header)) -> None:
    expected = request.app.state.settings.api_key
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "Missing or invalid X-Api-Key header"}},
        )
