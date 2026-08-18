"""Shared HTTP helpers for platform polling.

Polling hits the same few hosts every 20-30 s, 24/7.  aiohttp's defaults
(keep-alive 15 s, DNS cache 10 s) both expire between polls, so every poll
used to open a fresh TCP+TLS connection and DNS lookup — a constant stream
of new NAT sessions through the home router (see the 2026-08-18 outage
post-mortem in docs/superpowers/specs/2026-08-18-polling-keepalive-design.md).
"""
import logging
from contextlib import asynccontextmanager

import aiohttp

log = logging.getLogger(__name__)

# Longer than every poll gap (20-30 s) so idle connections survive until the
# next poll; shorter than common server idle timeouts (75 s+) so we rarely
# reuse a connection the server has already abandoned.
KEEPALIVE_TIMEOUT_S: float = 60.0

# One DNS lookup per host per 5 minutes instead of per poll.
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
    """``session.get`` wrapper that retries ONCE on ServerDisconnectedError.

    With keep-alive enabled, a poll can race the server closing the idle
    connection; the request then fails with ServerDisconnectedError before
    anything was received.  GET is idempotent, so one immediate retry on a
    fresh connection is safe.  Only the request phase is retried — errors
    raised while the caller reads the body propagate unchanged.
    """
    try:
        resp = await session.get(url, **kwargs)
    except aiohttp.ServerDisconnectedError:
        log.debug("stale keep-alive connection for %s; retrying once", url)
        resp = await session.get(url, **kwargs)
    try:
        yield resp
    finally:
        resp.release()
