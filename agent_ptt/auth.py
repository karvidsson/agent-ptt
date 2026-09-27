"""Perimeter authentication — a single shared API key for the whole server.

This is Step 0 of docs/plans/org-auth.md: a static key that gates every
REST route and both WebSockets before a real identity layer exists.

The key is read from ``AGENT_PTT_API_KEY`` at request time. When the
variable is unset or empty, auth is disabled and the server behaves as it
always has, so existing local setups keep working unchanged.

Clients present the key in one of two headers (either is accepted):

    Authorization: Bearer <key>
    X-API-Key: <key>

WebSocket clients may fall back to an ``api_key`` query parameter because
browsers cannot set headers on ``new WebSocket(...)``. Query strings end up
in proxy and access logs, so prefer the headers wherever a client can send
them.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, WebSocket

API_KEY_ENV = "AGENT_PTT_API_KEY"
CONFIRM_DELETE_ALL_HEADER = "X-Confirm-Delete-All"
WS_CLOSE_UNAUTHORIZED = 4401


def configured_api_key() -> str | None:
    """The configured API key, or None when auth is disabled.

    Read on every call rather than at import so tests can monkeypatch the
    environment and operators can rely on the process environment.
    """
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def auth_enabled() -> bool:
    return configured_api_key() is not None


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _key_matches(candidate: str | None, expected: str) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate.encode(), expected.encode())


def _credentials_valid(
    authorization: str | None, x_api_key: str | None, query_key: str | None = None
) -> bool:
    """True when auth is disabled or any presented credential matches the key."""
    expected = configured_api_key()
    if expected is None:
        return True
    for candidate in (_bearer_token(authorization), x_api_key, query_key):
        if _key_matches(candidate, expected):
            return True
    return False


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency: reject the request unless a valid API key is presented.

    A no-op when ``AGENT_PTT_API_KEY`` is not set.
    """
    if not _credentials_valid(authorization, x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def websocket_authorized(websocket: WebSocket) -> bool:
    """Check a WebSocket handshake for a valid API key.

    Accepts the same headers as :func:`require_api_key` plus an ``api_key``
    query parameter for browser clients. Call this before ``accept()``.
    """
    # Legacy sockets have no organization ownership and must stay local-only.
    if os.environ.get("AGENT_PTT_HOSTED", "0") == "1":
        return False
    return _credentials_valid(
        websocket.headers.get("authorization"),
        websocket.headers.get("x-api-key"),
        websocket.query_params.get("api_key"),
    )


async def require_delete_all_confirmation(
    x_confirm_delete_all: str | None = Header(default=None, alias=CONFIRM_DELETE_ALL_HEADER),
) -> None:
    """FastAPI dependency: ``DELETE /channels`` needs an explicit confirmation header.

    Applies whether or not API-key auth is enabled — wiping every channel
    should never happen by accident.
    """
    if (x_confirm_delete_all or "").strip().lower() != "yes":
        raise HTTPException(
            status_code=403,
            detail=f"Deleting all channels requires the header {CONFIRM_DELETE_ALL_HEADER}: yes",
        )
