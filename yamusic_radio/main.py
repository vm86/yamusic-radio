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

from yamusic_radio import dj
from yamusic_radio.cache import DEFAULT_CACHE, TrackCache
from yamusic_radio.multi import BackendSpec, MultiBackend
from yamusic_radio.ya_player import YandexMusicPlayer, has_token

faulthandler.enable()
log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path.home() / ".config/yamusic-radio.json"
DEFAULT_LOG = Path.home() / ".cache/yamusic_radio.log"
# The ids of backend_specs(), for the command line.
BACKEND_IDS = ("yandex",)
# The cache setting that keeps the backends turned on (the older one,
# "backend", kept just the one).
SETTING_BACKENDS = "backends"
SETTING_BACKEND = "backend"


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
        DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
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
        "--foreground",
        action="store_true",
        default=False,
        help=(
            "Don't fork into the background and log to stderr (for "
            "containers; SIGTERM then puts the station settings back)"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Settings file with the Yandex OAuth token (Yandex backend)",
    )
    parser.add_argument(
        "--backend",
        action="append",
        choices=BACKEND_IDS,
        help=(
            "Turn a music backend on (repeat for several). The tracks of "
            "all that are on are mixed. The ones of the last run and the "
            "ones that are signed in are on too; the page turns them on "
            "and off"
        ),
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
        action="append",
        default=[],
        help=(
            "Station to take tracks from: ID=STATION for the backend ID "
            "(repeat for several), or just STATION for the first backend. "
            "Each backend has its own by default"
        ),
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

    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=(
            "Database of track analyses and play times (no audio is "
            "stored): a restart doesn't repeat tracks or analyze them again"
        ),
    )
    parser.add_argument(
        "--tracks-dir",
        type=Path,
        default=None,
        help=(
            "Where the tracks that are loaded and wait for their turn are "
            "kept, so that a restart plays them and doesn't download them "
            "again (default: next to the cache, as <cache name>-tracks; a "
            "pool of lossless tracks takes some hundreds of MB)"
        ),
    )
    parser.add_argument(
        "--no-keep-tracks",
        action="store_true",
        default=False,
        help="Keep no tracks between runs: they are loaded again at a start",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Don't keep anything on disk between runs",
    )

    return parser.parse_args()


def backend_specs(
    options: Namespace, session: ClientSession
) -> list[BackendSpec]:
    """List the music services the radio can take its tracks from.

    The one place to change to play from another service or from a local
    collection: add a `BackendSpec` that makes any `MusicBackend`.
    """
    return [
        BackendSpec(
            "yandex",
            "Yandex Music",
            lambda: YandexMusicPlayer(options.config, session),
            lambda: has_token(options.config),
        ),
    ]


def _last_backends(cache: TrackCache | None) -> list[str]:
    """List the backends that were on in the last run."""
    if cache is None:
        return []
    ids = cache.get_setting(SETTING_BACKENDS)
    if not isinstance(ids, list):
        ids = [cache.get_setting(SETTING_BACKEND)]
    return [i for i in ids if isinstance(i, str)]


def _stations(values: list[str], first: str) -> dict[str, str]:
    """Read the --station options: ID=STATION, or STATION for the first."""
    stations = {}
    for value in values:
        backend_id, found, station = value.partition("=")
        if found and backend_id in BACKEND_IDS:
            stations[backend_id] = station
        else:
            stations[first] = value
    return stations


def _tracks_dir(options: Namespace, cache: TrackCache | None) -> Path | None:
    """Return where to keep the waiting tracks, None to keep none."""
    if cache is None or options.no_keep_tracks:
        return None
    chosen: Path | None = options.tracks_dir
    cache_path: Path = options.cache
    return chosen or cache_path.with_name(f"{cache_path.stem}-tracks")


async def _run(options: Namespace, socket_notify: socket.socket | None) -> None:
    session = ClientSession(
        raise_for_status=False,
        timeout=ClientTimeout(sock_read=10, sock_connect=5),
    )
    cache = None if options.no_cache else TrackCache(options.cache)
    # What the last run was using, unless the command line says otherwise.
    specs = backend_specs(options, session)
    backend = MultiBackend(
        specs,
        enabled=[*(options.backend or ()), *_last_backends(cache)],
        stations=_stations(options.station, specs[0].id),
        on_change=(
            None
            if cache is None
            else lambda ids: cache.put_setting(SETTING_BACKENDS, ids)
        ),
    )
    ready = asyncio.Event()
    serve_task = asyncio.create_task(
        dj.serve(
            backend,
            host=options.host,
            port=options.port,
            pool_size=options.tracks,
            effects=options.effects,
            mood_arc=options.mood_arc,
            cache=cache,
            tracks_dir=_tracks_dir(options, cache),
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
        await backend.aclose()
        await session.close()
        if cache is not None:
            cache.close()


def main() -> None:
    options = parse_args()
    notify_addr = os.getenv("NOTIFY_SOCKET")

    init_logging(
        debug=options.debug,
        systemd_run=notify_addr is not None or options.foreground,
    )

    socket_notify = None
    child_pid: int | None
    if notify_addr is None:
        child_pid = None if options.foreground else os.fork()
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
