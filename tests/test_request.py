# ruff: noqa: S101
# mypy: ignore-errors
from __future__ import annotations

import pytest

from yandex_music_radio_dj.request import RateLimitError, retry_on_rate_limit


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
    assert call.calls == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_retry_on_rate_limit_gives_up_after_all_delays() -> None:
    call = _Flaky(failures=10)

    with pytest.raises(RateLimitError):
        await retry_on_rate_limit(call, delays=(0.0, 0.0))

    assert call.calls == 3  # noqa: PLR2004
