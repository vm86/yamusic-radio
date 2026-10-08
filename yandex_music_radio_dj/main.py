from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
import socket
from argparse import ArgumentParser, Namespace
from contextlib import suppress
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout

from yandex_music_radio_dj import dj
from yandex_music_radio_dj.ya_player import YandexMusicPlayer

faulthandler.enable()
log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path.home() / ".config/yandex-music-radio-dj.json"
DEFAULT_LOG = Path.home() / ".cache/yandex_music_radio_dj.log"


def init_logging(*, debug: bool, systemd_run: bool) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO if not debug else logging.DEBUG)

    logging.getLogger("yandex_music").setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(threadName)s: [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler: logging.Handler
    if debug or systemd_run:
        handler = logging.StreamHandler()
    else:
        handler = logging.FileHandler(DEFAULT_LOG)
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO if not debug else logging.DEBUG)
    root_logger.addHandler(handler)


def parse_args() -> Namespace:
    parser = ArgumentParser(
        description=(
            "DJ radio: download tracks from a Yandex Music station, "
            "beat-match them into one endless mix and stream it as MP3 "
            "over HTTP (needs ffmpeg)"
        )
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debugging output",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="Run foreground",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Settings file with the Yandex OAuth token",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address of the stream",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP port of the stream (/stream)",
    )
    parser.add_argument(
        "--tracks",
        type=int,
        default=12,
        help="Tracks to download and analyze before the radio starts",
    )
    parser.add_argument(
        "--station",
        default="user:onyourwave",
        help="Station the radio takes its tracks from",
    )
    parser.add_argument(
        "--effects",
        choices=("off", "light", "full"),
        default="light",
        help=(
            "Club effects on transitions: a riser and a boom on "
            "the drop, an echo-out when the beats can't be matched"
        ),
    )
    parser.add_argument(
        "--mood-arc",
        action="store_true",
        default=False,
        help=(
            "Switch the mood setting of the station along the energy arc of "
            "the set (calm, fun, active). The setting is kept on your "
            "Yandex account: it is changed while the radio runs and put "
            "back when it stops"
        ),
    )

    return parser.parse_args()


async def _run(options: Namespace, socket_notify: socket.socket | None) -> None:
    session = ClientSession(
        raise_for_status=False,
        timeout=ClientTimeout(sock_read=10, sock_connect=5),
    )
    player = YandexMusicPlayer(options.config, session)
    ready = asyncio.Event()
    serve_task = asyncio.create_task(
        dj.serve(
            player,
            session,
            host=options.host,
            port=options.port,
            station_id=options.station,
            pool_size=options.tracks,
            effects=options.effects,
            mood_arc=options.mood_arc,
            ready=ready,
        )
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        ready_wait = asyncio.create_task(ready.wait())
        await asyncio.wait(
            {ready_wait, serve_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if serve_task.done():
            ready_wait.cancel()
            serve_task.result()
        if socket_notify is not None:
            socket_notify.sendall(b"READY=1")

        stop_wait = asyncio.create_task(stop.wait())
        await asyncio.wait(
            {serve_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        stop_wait.cancel()
        if not serve_task.done():
            log.info("Shutting down...")
            serve_task.cancel()
        with suppress(asyncio.CancelledError):
            await serve_task
    finally:
        await player.aclose()
        await session.close()


def main() -> None:
    options = parse_args()
    notify_addr = os.getenv("NOTIFY_SOCKET")

    init_logging(debug=options.debug, systemd_run=notify_addr is not None)

    socket_notify = None
    if notify_addr is None:
        child_pid = os.fork()
    else:
        child_pid = None
        socket_notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if notify_addr[0] == "@":
            notify_addr = "\0" + notify_addr[1:]
        socket_notify.connect(notify_addr)

    if child_pid:
        if options.wait:
            os.waitpid(child_pid, 0)
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run(options, socket_notify))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
