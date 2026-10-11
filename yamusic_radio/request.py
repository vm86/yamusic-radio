# https://github.com/AlexxIT/YandexStation/blob/master/custom_components/yandex_station/core/yandex_session.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientSession
from yandex_music.exceptions import (
    BadRequestError,
    NetworkError,
    NotFoundError,
    TimedOutError,
    UnauthorizedError,
    YandexMusicError,
)
from yandex_music.utils.request_async import (
    USER_AGENT,
    Request,
)
from yarl import URL

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

log = logging.getLogger(__name__)

RATE_LIMIT_DELAYS = (0.5, 1.0, 2.0, 4.0)
HTTP_BAD_GATEWAY = 502


class RateLimitError(NetworkError):
    """HTTP 429: Yandex limits the number of concurrent requests."""


STATUS_ERRORS: dict[int, type[NetworkError | YandexMusicError]] = {
    400: BadRequestError,
    401: UnauthorizedError,
    403: UnauthorizedError,
    404: NotFoundError,
    409: NetworkError,
    413: NetworkError,
    429: RateLimitError,
}


def where(url: str) -> str:
    """Return the address without its query: it holds signatures."""
    return str(URL(url).with_query(None))


class Cooldown:
    """A pause the requests share: when Yandex limits one, all of them wait.

    Each request backing off on its own sends the next burst at the same
    moment, which is limited again, and every one of them logs it.
    """

    def __init__(self) -> None:
        self._until = 0.0

    async def wait(self) -> None:
        left = self._until - asyncio.get_running_loop().time()
        if left > 0:
            await asyncio.sleep(left)

    def start(self, delay: float) -> bool:
        """Pause for delay, unless a pause is on already; say if it began.

        A request that was limited while the pause was on was sent before
        it: it adds nothing, and nothing is said of it.
        """
        now = asyncio.get_running_loop().time()
        if self._until > now:
            return False
        self._until = now + delay
        return True


async def retry_on_rate_limit[T](
    call: Callable[[], Awaitable[T]],
    delays: tuple[float, ...] = RATE_LIMIT_DELAYS,
    cooldown: Cooldown | None = None,
) -> T:
    cooldown = cooldown or Cooldown()
    for delay in delays:
        await cooldown.wait()
        try:
            return await call()
        except RateLimitError:
            if cooldown.start(delay):
                log.warning("Rate limited, requests wait %.1fs", delay)
    await cooldown.wait()
    return await call()


class ClientRequest(Request):
    def __init__(self, client_session: ClientSession) -> None:
        super().__init__()
        self.__client_session = client_session

    async def _request_wrapper(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        proxy: str | None = None,
        data: object = None,
        json: object = None,
        **_unused: object,
    ) -> bytes:
        # (The timeout and the rest of what the library passes are not
        # used: the session has its own timeouts.)
        # api.music.yandex.net/rotor/station/*/feedback and .../settings3
        # only accept a JSON body; yandex_music sends form-data there
        # (data=dict), which makes the server reply 400 "condition is not met".
        if "/rotor/station/" in url and isinstance(data, dict):
            data, json = None, data

        try:
            async with self.__client_session.request(
                method,
                url,
                params=params,
                headers={**(headers or {}), "User-Agent": USER_AGENT},
                proxy=proxy,
                data=data,
                json=json,
            ) as _resp:
                resp = _resp
                content = await resp.content.read()
        except TimeoutError as e:
            log.warning("%s %s timed out", method, where(url))
            raise TimedOutError from e
        except ClientError as e:
            raise NetworkError(f"{method} {where(url)}: {e!r}") from e

        if resp.ok:
            return content

        message = "Unknown error"
        try:
            parse = self._parse(content)
            if parse:
                message = parse.get_error()
        except YandexMusicError:
            message = "Unknown HTTPError"

        if error := STATUS_ERRORS.get(resp.status):
            raise error(message)
        if resp.status == HTTP_BAD_GATEWAY:
            raise NetworkError("Bad Gateway")

        raise NetworkError(f"{message} ({resp.status}): {content!r}")
