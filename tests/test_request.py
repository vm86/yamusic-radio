from __future__ import annotations

import pytest
from aiohttp import ClientSession, ServerDisconnectedError
from yandex_music.exceptions import NetworkError

from yamusic_radio.request import (
    ClientRequest,
    RateLimitError,
    retry_on_rate_limit,
    where,
)


class _Flaky:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimitError("Concurrency limit exceeded")
        return "ok"


@pytest.mark.asyncio
async def test_retry_on_rate_limit_succeeds_after_backoff() -> None:
    call = _Flaky(failures=2)

    assert await retry_on_rate_limit(call, delays=(0.0, 0.0, 0.0)) == "ok"
    assert call.calls == 3


@pytest.mark.asyncio
async def test_retry_on_rate_limit_gives_up_after_all_delays() -> None:
    call = _Flaky(failures=10)

    with pytest.raises(RateLimitError):
        await retry_on_rate_limit(call, delays=(0.0, 0.0))

    assert call.calls == 3


def test_where_drops_the_query() -> None:
    url = "https://api.music.yandex.net/get-file-info?sign=secret&ts=1"

    assert where(url) == "https://api.music.yandex.net/get-file-info"


@pytest.mark.asyncio
async def test_a_failed_request_says_which_address_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disconnect(*_args: object, **_kwargs: object) -> None:
        raise ServerDisconnectedError

    async with ClientSession() as session:
        monkeypatch.setattr(session, "request", disconnect)
        request = ClientRequest(session)

        with pytest.raises(NetworkError, match=r"GET https://x\.example/a/b:"):
            await request.get("https://x.example/a/b?sign=secret")
