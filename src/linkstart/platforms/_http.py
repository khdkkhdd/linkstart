"""Shared HTTP helpers for platform polling — aiohttp defaults expire between
20-30 s polls, so every poll opened a fresh TCP+TLS+DNS round (2026-08-18 outage)."""
import logging
from contextlib import asynccontextmanager

import aiohttp

log = logging.getLogger(__name__)

# Longer than the poll gap (20-30 s), shorter than server idle timeouts (75 s+).
KEEPALIVE_TIMEOUT_S: float = 60.0
DNS_CACHE_TTL_S: int = 300


def create_polling_session(
    headers: dict[str, str] | None = None,
) -> aiohttp.ClientSession:
    """Return a ClientSession tuned so consecutive polls reuse connections."""
    connector = aiohttp.TCPConnector(
        keepalive_timeout=KEEPALIVE_TIMEOUT_S,
        ttl_dns_cache=DNS_CACHE_TTL_S,
    )
    return aiohttp.ClientSession(connector=connector, headers=headers)


@asynccontextmanager
async def polling_get(session: aiohttp.ClientSession, url: str, **kwargs):
    """``session.get`` retrying ONCE on ServerDisconnectedError — a keep-alive
    poll can race the server closing the idle connection, and GET is idempotent.
    Only the request phase retries; body-read errors propagate unchanged."""
    try:
        resp = await session.get(url, **kwargs)
    except aiohttp.ServerDisconnectedError:
        log.debug("stale keep-alive connection for %s; retrying once", url)
        resp = await session.get(url, **kwargs)
    try:
        yield resp
    finally:
        resp.release()
