"""Shared outbound HTTP clients with TLS certificate verification enabled."""

from __future__ import annotations

import ssl

import httpx

from app.config import Settings, get_settings


def build_http_client(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
    transport: httpx.BaseTransport | None = None,
    verify: bool | ssl.SSLContext | None = None,
) -> httpx.Client:
    """A sync client for TMDb/OMDb/LLM calls. Tests pass an httpx.MockTransport."""
    s = settings or get_settings()
    seconds = timeout if timeout is not None else s.media_timeout_s
    kwargs: dict = {
        "timeout": httpx.Timeout(seconds),
        "verify": True if verify is None else verify,
        "follow_redirects": True,
        "trust_env": True,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)
