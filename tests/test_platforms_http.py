"""Tests for the shared polling HTTP helpers."""
import aiohttp
import pytest

from linkstart.platforms._http import create_polling_session, polling_get


async def test_session_connector_is_tuned_for_polling():
    session = create_polling_session()
    try:
        connector = session.connector
        # Private attrs: aiohttp exposes no public accessors for these.
        assert connector._keepalive_timeout == 60
        assert connector.use_dns_cache is True
        assert connector._cached_hosts._ttl == 300
    finally:
        await session.close()


async def test_session_applies_default_headers():
    session = create_polling_session(headers={"User-Agent": "test-agent"})
    try:
        assert session.headers["User-Agent"] == "test-agent"
    finally:
        await session.close()


async def test_session_without_headers_has_no_custom_user_agent():
    session = create_polling_session()
    try:
        # aiohttp injects its own UA at request time; the session-level
        # default headers must stay empty when none are passed.
        assert "User-Agent" not in session.headers
    finally:
        await session.close()


class _FakeResponse:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeGet:
    """Awaitable mimicking session.get(): raises or returns a response."""

    def __init__(self, outcome):
        self._outcome = outcome

    def __await__(self):
        async def _run():
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome
        return _run().__await__()


class _FakeSession:
    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return _FakeGet(self._outcomes.pop(0))


async def test_polling_get_retries_once_on_server_disconnect():
    response = _FakeResponse()
    session = _FakeSession([aiohttp.ServerDisconnectedError(), response])
    async with polling_get(session, "https://example.test/") as resp:
        assert resp is response
    assert session.calls == 2
    assert response.released is True


async def test_polling_get_gives_up_after_second_disconnect():
    session = _FakeSession(
        [aiohttp.ServerDisconnectedError(), aiohttp.ServerDisconnectedError()]
    )
    with pytest.raises(aiohttp.ServerDisconnectedError):
        async with polling_get(session, "https://example.test/"):
            pass
    assert session.calls == 2


async def test_polling_get_does_not_retry_other_client_errors():
    # ServerDisconnectedError subclasses ClientConnectionError; only the
    # exact stale-connection signature may trigger a retry.
    session = _FakeSession([aiohttp.ClientConnectionError("boom")])
    with pytest.raises(aiohttp.ClientConnectionError):
        async with polling_get(session, "https://example.test/"):
            pass
    assert session.calls == 1


async def test_polling_get_releases_response_when_body_raises():
    response = _FakeResponse()
    session = _FakeSession([response])
    with pytest.raises(RuntimeError):
        async with polling_get(session, "https://example.test/"):
            raise RuntimeError("body handling failed")
    assert response.released is True
    assert session.calls == 1
