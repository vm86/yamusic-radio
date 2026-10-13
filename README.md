# yamusic-radio

Endless DJ radio. Takes tracks from a Yandex Music station, from YouTube Music
and/or from a folder of audio files (or from any other source you add as a
[backend](#adding-a-music-backend)), several at once, beat-matches them (bass
swap, three-band EQ blend, filter sweeps, optional effects) and streams one
continuous mix over HTTP: MP3 at 320 kbit/s with ICY titles, and a lossless
stream (FLAC, or FLAC in Ogg for Safari). There is no web page: the radio has
the streams and a [REST API](#rest-api).

## Run

Needs Python >= 3.12 and `ffmpeg` with libmp3lame in `PATH`.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test,lint]"
.venv/bin/yamusic-dj --wait --port 8000 --backend yandex
.venv/bin/yamusic-dj --mood-arc --effects full --host 0.0.0.0
# listen: http://127.0.0.1:8000/stream (MP3), /stream.flac or /stream.ogg (lossless)
```

**Music backends.** The tracks come from the backends that are on; with
several on, their tracks go into one pool and the radio picks the best fit
among them. A backend is on when it is asked for (`--backend ID`, repeatable,
or the API, `PUT /api/auth/backend`) or when it was on in the last run. On
the very first run, when nothing is remembered yet, a backend that is already
signed in (has a saved token) is on too. After that a backend that was turned
off through the API stays off, signed in or not; what the command line turned on
is remembered. Until one is on the radio waits, and `GET /api/auth` lists the
backends. Three come with the program: `yandex`, `youtube` (YouTube Music) and `local` (a
folder of audio files). When a backend is turned off, or cannot be reached, its tracks that are
only linked (a folder's) leave the queue, and so does its decoded next track. A
backend that fails is left alone for a while (30 s, doubling up to 10 min) and
the others carry on. `--station ID=STATION` (repeatable) picks the station of a
backend; `--station STATION` is for the first one.

**YouTube Music.** `pip install ".[youtube]"` gives the libraries (the RPM and the
Docker image have them already):
[ytmusicapi](https://github.com/sigma67/ytmusicapi) finds the tracks and
[yt-dlp](https://github.com/yt-dlp/yt-dlp) downloads their audio (ytmusicapi does
not give audio). Turn the backend on with `--backend youtube`. It needs no
sign-in for the public stations, and plays AAC of about 130 kbit/s. yt-dlp
wants a JavaScript runtime for YouTube: install `deno` (or `node`, `bun`; the
image has `nodejs`, the RPM only suggests it), see the
[EJS notes](https://github.com/yt-dlp/yt-dlp/wiki/EJS). The station is
chosen with `--station youtube=STATION`; the default is `charts`:

| Station | Plays |
|---|---|
| `charts`, `charts:RU` | the tracks of the top chart playlist (the global one, or of a country by its code) |
| `radio:VIDEO_ID` | the radio of a song (it gives other songs each time it is asked) |
| `playlist:PLAYLIST_ID` | the tracks of a playlist |
| `search:deep house` | the songs found for the words |
| `liked` | your liked songs (needs `--youtube-auth`) |

Tracks longer than 15 minutes (mixes, sets) and shorter than 30 s are left out.
`--youtube-auth FILE` takes the headers of a browser session made by
`ytmusicapi browser` (see its
[docs](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html)); it
turns the backend on and gives `liked`. The backend has no moods (the radio
judges the tracks by their sound) and tells YouTube nothing about what was
played. Downloading from YouTube is against its terms of service: use it for
yourself, and at your own risk.

**Local folder.** `--local-dir ~/Music` turns the `local` backend on with that
folder; the API can choose and change the folder too (it is a field of the
backend's sign-in, `POST /api/auth`), and the choice is kept in the cache
settings, so a restart plays from it again (without `--no-cache`). It plays the audio files under the
folder (`mp3`, `flac`, `m4a`, `aac`, `ogg`, `opus`, `wav`, `aiff`, `wma`;
hidden files and folders are skipped) in random order, a few at a time, and
plays a track again only when all the others were heard. Files are linked, not
copied, so a library costs no disk. Title, artist and genre are read from the
tags with `ffmpeg`, only for the tracks that are asked for; without tags the
file name is the title and the folder the artist. The station is the whole
folder (`shuffle`), or a subfolder of it (`--station local=Jazz`). The list of
files is made again every 10 minutes. The backend has no moods: the radio
judges the tracks by their sound.

On the first run of the Yandex backend the URL and the code of the OAuth
device flow are in the log and at `GET /api/auth` (the server is already up,
so `curl -s localhost:8000/api/auth` works before anyone has logged in). The
token is stored, mode 0600, in `~/.config/yamusic-radio.json`
(`--config` to change). The log is `~/.cache/yamusic_radio.log`
(or `--debug` to stderr).

Track analyses and play times (never audio) are cached in
`~/.cache/yamusic-radio.db`, so a restart does not repeat the last
tracks or analyze known ones again. The genre of the track (from its album) is
kept in the same table; the API does not show it. `--cache PATH` moves it, `--no-cache`
turns it off.

The tracks that are loaded and wait for their turn are kept in
`~/.cache/yamusic-radio-tracks/` (`<cache name>-tracks`, next to the cache;
`--tracks-dir PATH` moves it, `--no-keep-tracks` turns it off, and so does
`--no-cache`), so a restart plays them instead of loading them again. A pool of
lossless tracks takes some hundreds of MB. The Docker image keeps them in
`/data/tracks`.

The moods, the genres and the backends that were picked are kept in the cache
and picked again at the next start (not with `--no-cache`). A backend that
steers its station by the mood (Yandex does) has the mood setting of the
station on the account put back when the radio stops; the original setting is
kept in the cache for as long as the account has another one, so if the radio
is killed the next run puts it back first. For a backend that does not, the
radio finds the mood of each track by its sound (energy and key).

## Streams

The streams carry the same mix and are always on (the radio runs an
`ffmpeg` encoder for each):

| URL | Format | Notes |
|---|---|---|
| `/stream` | MP3, 320 kbit/s (`MP3_BITRATE` in `dj.py`; `libmp3lame`) | With ICY titles (`Icy-MetaData: 1`). Plays in any player. |
| `/stream.flac` | FLAC, lossless, 16 bit | The mix is dithered to 16 bit (triangular dither, as for a CD). No titles: the stream carries no tags. About 110 kB a second, so mind the network. |
| `/stream.ogg` | The same FLAC in an Ogg container | For Safari and every browser on iOS: they play a live stream of FLAC in Ogg, not of bare FLAC. A client picks it for those browsers. No titles. |

A new listener gets the latest 3 s of the stream at once (from a frame
boundary; a FLAC listener gets the stream header first), so the player can
start at once instead of after the ~1.5 s that a browser waits for audio
arriving at the pace it plays; the price is that the listener is ~3 s behind
the encoder. `PREROLL_SECONDS` in `dj.py`.

The mix stands while nobody listens to the stream (the pause is then
`pause_reason: "no_listeners"`, and the time of the track stops too) and goes
on with the first listener; `POST /api/pause` holds it for everyone until
`POST /api/resume`.

The stream has no authentication: bind only to trusted networks (`--host`).
The streams are sent with `Access-Control-Allow-Origin: *`, so a client of
another origin can play them in an `<audio crossorigin>` and analyze the sound
with Web Audio.

## Key detection

The key is found by [Essentia](https://essentia.upf.edu/) (`pip install
".[key]"`; wheels exist for Linux x86_64 and macOS (arm64 and x86_64), the Docker image and the RPM have it, and it needs
`libatomic`) with its `edma` profile, made for electronic music. Where
Essentia has no answer, and when it is not installed, a simpler match of the
pitch-class profile with the Krumhansl-Kessler profiles gives the key; the log
says which one runs. Essentia is asked first, as the simpler match finds
dense, distorted music (metal) as flat as noise and gives up on it; but
Essentia names a key for any sound but silence, noise too. The simpler match
gives none for a track with no tonal centre. A cached analysis without a key is
made again when the track comes up (Essentia may find one now), and so is one
made by another detector.

Checked on 240 synthetic tracks (chords, bass, melody and drums in all 24 keys,
random tempo, tuning and timbre), the key of which is known:

| | exact | a neighbour on the wheel (still mixes) | wrong |
|---|---|---|---|
| Essentia, `edma` | 91% | 9% | 0% |
| plain profile match | 36% | 45% | 18% |

Real music is harder than this, so treat the numbers as a comparison of the
two, not as the accuracy to expect.

## Loudness

The loudness of a track is its integrated loudness in LUFS by EBU R128
(ITU-R BS.1770: K-weighted, gated), measured by Essentia over the audible part
of the track. It is `loudness_db` in the API, and the radio compares tracks
by it (a blend of tracks far apart in loudness is a filter sweep, the cost of
a pair counts the gap, the energy of a track counts the level). Without
Essentia, or for a piece shorter than the 400 ms gating block, it is the RMS of
the samples, plus 2 dB to be on the scale of LUFS (the two differ by 1.8 to 2.5
dB on the test signals; it is 3 dB for a sine). An analysis in the cache that
was made the other way is made again. The scale of the energy
(`ENERGY_LOUDNESS_RANGE_DB` in `dj_mix.py`) is -16 to -4 LUFS. Measuring takes
a copy of the track in memory, about 130 MB for six minutes.

## Harmonic mixing

The key of each track is detected (shown as a Camelot Wheel code) and the
next track is chosen with it in mind. Cost of a change, cheapest first: the
same key; one step round the wheel (`8A` to `7A` or `9A`) or the relative key
(`8A` to `8B`); two steps up (`8A` to `10A`, an energy boost); seven steps up
(`8A` to `3A`, a semitone up); two steps down; one step and the other letter
(`8A` to `9B`); anything else clashes. Tempo, loudness, the energy arc, the
moods and repeated artists count as before, and a track with an undetected key
costs nothing extra. The constants are `KEY_*` in `dj_mix.py`.

The key must not push the set off its energy arc: on random pools (30 sets of
30 tracks) the arc followed the target with a correlation of 0.57 before the
keys, 0.33 with the keys at full weight, and 0.70 with `KEY_COST_SCALE = 0.7`
and `ENERGY_ARC_COST = 8`, which are the values now. About half of the changes
stay on neighbouring keys (a fifth of them would by chance).

## Transitions

- **Tempo glide.** Tracks are played at their own tempo. If the tempos are
  within 15% of each other (half or double time counts), a blend goes over a
  whole number of beats (8 to 32) and the tempo of **both** pieces glides from
  the tempo of the outgoing track to that of the incoming one, growing by the
  same amount on each beat, so the beats stay together; once the blend is over
  the incoming track goes on at its own tempo (WSOLA time stretching, the
  pitch stays).
  The stretch moves each frame only a little where there is an attack (a
  kick stays on the beat) and a lot where there is none (tones stay clean).
- **Limiter.** Two loud tracks, a riser and a boom add up to more than 0 dBFS,
  which the MP3 encoder clips (crackle). Every blend goes through a look-ahead
  limiter (ceiling 0.97): the gain dips smoothly before a peak and comes back
  over ~0.1 s.
- **Bass swap** (beat-matched, loudness within 3 dB): the highs cross over,
  the bass changes hands on a beat in the middle.
- **Three-band EQ blend** (as above, and the blend is 10 s or longer, or the
  keys clash): the mids change hands first (20-50% of the blend), then the
  bass on a beat in the middle, while the highs cross over 15-85%.
- **Filter sweep**: everything else (no shared beat grid, or a big jump in
  loudness).

## No web page

The radio has no page of its own: it serves the streams and the REST API
only, and `GET /` answers 404 with `{"web": false, "stream": "/stream",
"api": "/api/queue"}`. A page, an app or a script does the rest through the
API: the backends and their sign-in (`/api/auth`), the queue and the next
track button (`/api/queue`, `/api/next`, `/api/pause`), the moods, the genres
and the language, the statistics and the spectrum. A page of another origin
can play the streams and read `/api/spectrum` (both are sent with
`Access-Control-Allow-Origin: *`); the rest of the API is for the same origin
or a script.

## Spectrum

The radio mixes the sound itself, so it knows how loud each band is at every
moment; a client cannot get that from the stream it plays (it needs CORS and
it hears the sound ~3 s late). So the radio sends the bands over
`GET /api/spectrum`, each with its position in the mix, and a client shows
them when its player gets there.

To tie the two together, the client gives its connection a name: it opens the
stream as `/stream?l=NAME` (or `/stream.flac?l=NAME`, `/stream.ogg?l=NAME`)
and the spectrum as `/api/spectrum?l=NAME`. The radio notes where in the mix that stream starts
(a new listener gets the latest 3 s at once) and sends it first as the `start`
event. A page of another origin can do the same: both URLs are sent with
`Access-Control-Allow-Origin: *`. The analysis is in `yamusic_radio/spectrum.py`
(`BANDS`, `LOW_HZ`, `HIGH_HZ`, `FLOOR_DB`).

## REST API

Served next to the stream, no authentication.

| Request | Answer |
|---|---|
| `GET /api/queue?previous=2&next=15` | `previous` (oldest first), `now` (with `elapsed_seconds`, which does not run while the mix is paused), `next`, `moods`, the state of the next track button (`skip_pending`, `blending`, `can_skip`, as in `GET /api/next`) and the pause (`paused`, `pause_reason`, as in `POST /api/pause`). Both counts are capped at 15; the defaults are 2 and 15. |
| `PUT /api/queue/order` `{"track_ids": ["a", "b"]}` | Put the tracks of the queue (the `next` ones, by `track_id`) in this order by hand. The tracks left out follow them, as the radio chooses. Answers like `GET /api/queue`; 404 for a track that is not in the queue, 409 while a transition is under way (the next track is fixed). The same goes for the three that follow. |
| `POST /api/queue/move` `{"track_id": "a", "position": 0}` | Move a track of the queue to a place in it (0 is the next one). Answers like `GET /api/queue`. |
| `DELETE /api/queue/tracks/{track_id}` | Take a track out of the queue: it is not played, nor loaded again (after a restart either). Answers like `GET /api/queue`. |
| `POST /api/queue/play` `{"track_id": "a"}` | Play a track of the queue next, as `POST /api/next` does: the blend starts on a good beat. Answers 202 with the queue. |
| `POST /api/queue/refresh` | Choose the next tracks anew among the ones already loaded: the track already chosen and decoded goes back to the pool, tracks of moods and genres that are not selected are dropped (the genres picked since come into force here). Tracks of other genres are not downloaded; one without a genre is not played while genres are selected. The order put by hand is forgotten. Answers like `GET /api/queue`. |
| `GET /api/genres` | `available` (the genres of the tracks heard so far, the most common first), `selected` (picked) and `applied` (the queue is built from these). Empty is any genre. |
| `PUT /api/genres` `{"genres": ["house"]}` | Pick genres. Nothing is rebuilt: they come into force with the next `POST /api/queue/refresh`. `DELETE /api/genres` picks any. |
| `PUT /api/genres/blocked` `{"genres": ["rap"]}` | Genres never to play (they win over the picked ones); `blocked` and `blocked_applied` in `GET /api/genres`. Kept in the cache, in force with the next refresh like the picked ones. `DELETE /api/genres/blocked` clears them. |
| `GET /api/stats` | How the tracks were blended: `session` (this run) and `all_time` (from the cache, `null` without one), each with `total`, `beat_matched`, `by_type` (`bass swap`, `eq blend`, `filter sweep`, `crossfade`: what was really made, a blend too short for a filtered one is a crossfade), `by_effect` (`none`, `drop`: a riser and a boom, `echo`: an echo-out; `unknown` for blends made before the effects were counted) and `by_key` (`same`, `neighbour`, `boost`, `semitone`, `down`, `diagonal`, `clash`, `unknown`) and `by_quality` (the same, summed up: `good` is `same` and `neighbour`, `fair` is `boost`, `semitone`, `down` and `diagonal`, `poor` is `clash`). |
| `GET /api/stats/keys` | Every change of key there was, with how often: `session` and `all_time` (`null` without a cache), each a list of `{"from": "8A", "to": "9A", "relation", "quality", "count"}`, the most common first. A key that was not found is `null`. |
| `POST /api/next` | The next track now: it needs the next track to be ready, and the blend starts on the first bar line of the playing track at least 2 s ahead, as smooth as any other (16 beats; the tempo glides, bass swap / EQ blend / sweep). Answers 202 with the queue (`skip_pending: true` until the blend has begun), 409 if nothing plays yet or a transition is already under way. The station is told the track was skipped. |
| `GET /api/next` | Where the next track button is: `skip_pending` (asked for, waiting for a good beat), `blending` (the blend is under way, until the old track is left behind) and `can_skip` (something plays and neither of the two). A client drives its button by this. |
| `POST /api/pause`, `POST /api/resume` | Pause the mix for everyone, and go on. The time of the playing track does not run meanwhile. Answer `{"paused": true, "pause_reason": "user"}`, or `{"paused": false, "pause_reason": null}` after a resume. |
| `GET /api/spectrum?l=NAME` | The spectrum of the mix as server-sent events (`text/event-stream`, CORS `*`): 12 bands (30 Hz to 16 kHz, log-spaced, a little tilted up towards the treble), about 30 times a second. First `event: start` with `{"position": SECONDS}`: where in the mix the player of the page starts (`null` if it did not connect within 5 s). Then each value is `data: {"t": 12.345, "b": [0..1, ...]}`: `t` is the position in the mix, `b` the levels of the bands (0 at -60 dBFS, 1 at full scale). `: keepalive` comments come every 15 s. Frames are sent a little ahead (the player hears the sound ~3 s after the mix made it): show each one when the player gets to `start + t`. |
| `GET /api/transitions` | The last 10 blends, the newest last: `from`, `to`, `kind`, `effect`, `beat_matched`, `from_bpm`, `to_bpm`, `glide` (how far the tempo glided, `0.05` is 5% faster), `seconds`, `loudness_gap_db`, `key_relation`, `over_db` (how far above the limiter's ceiling the blend peaked before it was limited) and `at`. To look at a transition that did not sound right. |
| `GET /api/auth` | `authorized` (a backend can give tracks), and while it cannot, what to do to sign in: the `url` to open and the `code` to enter there (both `null` once authorized, or until the service has given them) and `fields` (`{"name", "label", "secret"}`: values to type, `[]` for a device flow). `backends` lists every backend with the same keys plus `id`, `name`, `enabled`, `available` (signed in and not resting after failures), and for a backend with settings (`local`: its folder) `settings` (the values now) and `setting_fields` (what they are called), which `POST /api/auth` changes. The server is up during authorization. |
| `POST /api/auth` `{"backend": "id", "values": {"login": "..."}}` | Sign in with the values of the `fields` of the backend (without `backend`, of the radio's own). Answers like `GET /api/auth`; 400 if the backend turns them down. |
| `PUT /api/auth/backend` `{"backend": "id"}` | Turn the backend on; it starts signing in if it needs to. Answers like `GET /api/auth`; 400 for an unknown id. |
| `DELETE /api/auth/backend/{id}` | Turn the backend off. The tracks it gave already stay in the pool. Answers like `GET /api/auth`; 404 for an unknown id. |
| `GET /api/language` | The language of the music: `available` (`any`, `russian`, `not-russian`, `without-words`), `selected` (picked here, `null` if none), the `station` language now and `steered` (whether the backend can change it). The same object is `language` in `GET /api/queue`. |
| `PUT /api/language` `{"language": "russian"}` | Play music of this language. A setting of the station on the account, like the moods: changed while the radio runs and put back on exit or `DELETE`, and remembered in the cache. The tracks loaded for the old language are given up but the two needed next, and the station is asked anew. 400 for an unknown language, 409 if the backend cannot change it, 503 before it is authorized. |
| `DELETE /api/language` | The account's own language again. |
| `GET /api/moods` | `available` (`sad`, `calm`, `fun`, `active`), `selected`, the `station` mood now and `steered` (whether the backends steer their stations by the mood; if not, the radio judges the tracks by their sound). |
| `PUT /api/moods` `{"moods": ["calm", "fun"]}` | Play only these moods. |
| `DELETE /api/moods` | Any mood again. |

Each track has `track_id`, `title`, `artists`, `bpm`, `camelot` (Camelot Wheel code such as `8A`, `""` when the key is unknown), `key` (`A minor`), `loudness_db` (LUFS),
`duration_seconds`, `mood`, `cover` (the URL of the cover, for a Yandex track
on Yandex's server, with `%%` where the size goes, e.g. `.../%%` -> `.../200x200`; `""` when there
is none) and `decided`. The first of `next` is already
decoded (`decided: true`); the rest is a forecast from the pool made the way
the radio chooses, and the radio may change it, as it picks ~40 s before each
blend.

Choosing moods changes the mood setting of the station **on your Yandex
account** (of every backend that steers its station) while the radio runs (put back on exit or `DELETE`). The track that
is already decoded keeps its mood; tracks of other moods in the pool are
dropped and fetched anew.

```bash
curl -s localhost:8000/api/queue | jq .
curl -s -X PUT localhost:8000/api/moods -d '{"moods":["calm","fun"]}'
```

## Adding a music backend

The DJ only talks to a `MusicBackend` (`yamusic_radio/backend.py`): it asks
for batches of tracks from a "station", downloads them to files, and tells
the backend what was heard. Yandex Music (`ya_player.py`) and a folder of audio files
(`local.py`, a short one to read first) are two implementations, and
`youtube.py` is a third that finds tracks in one library and downloads them
with another; a Subsonic server or a radio is another to write.

1. **Write a class** with the methods of the `MusicBackend` protocol:

   | Member | What it does |
   |---|---|
   | `authorized`, `auth_prompt`, `wait_authorized()` | Whether it can give tracks; what the user has to do to sign in (`AuthPrompt`: a `url` and a `code` of a device flow, and/or `fields` to type, each an `AuthField(name, label, secret)`); wait until it can. A backend that needs nothing is always `authorized`. |
   | `start_auth()`, `authorize(values)` | Begin a sign-in that goes on by itself (a device flow); sign in with the typed values, raising `AuthError` if they are wrong or there is no such way. |
   | `default_station` | The station to play when none is asked for (`--station`). |
   | `station_batch(station, exclude_track_ids=...)` | The next `Track`s of the station, ready to download. May leave out the excluded ones, may repeat when it has nothing else. Raises `BackendError` when the service is down. |
   | `download(track, path)` | Write the audio to `path`; raise if it cannot. |
   | `feedback_track(track, Feedback.STARTED/FINISHED/SKIPPED, station, seconds)` | Tell the service what happened to the track (a no-op if it has no use for it). |
   | `supports_moods`, `station_settings()`, `set_station_settings()` | Whether the service can steer a station by the mood. If not, return `False` (the other two are never called) and the radio judges each track by its sound. |
   | `aclose()` | Stop what runs in the background. |

   A `Track` has a `track_id` unique within the backend, a `title`, `artists`,
   the `codec` (the file extension that ffmpeg is given: `mp3`, `flac`, ...)
   and optionally `bitrate_in_kbps`, `genre`, a `cover` URL (with `%%` where
   the size such as `400x400` goes) and a `mood` (one of `sad`, `calm`, `fun`,
   `active`) when the service knows it. Raise only `BackendError` for what may
   pass (the service is down): the other backends carry on. Anything else is a
   bug and is not caught.

   A backend with settings the API can change (the folder of `local`) also
   has `settings` (the values now) and `setting_fields` (what they are called,
   `AuthField`s), the `Configurable` protocol; the new values come to
   `authorize()`, which keeps them. They are shown by `GET /api/auth`.

2. **Register it** in `backend_specs()` in `yamusic_radio/main.py`, and add its
   id to `BACKEND_IDS` (the choices of `--backend` and `--station`):

   ```python
   BackendSpec(
       "subsonic",  # the id
       "Subsonic",  # the name in the API
       lambda: SubsonicBackend(options.subsonic_url, session),
       lambda: bool(options.subsonic_url),  # configured: on without being asked
   )
   ```

   `local` is registered the same way, with the folder from `--local-dir` or
   the settings.

   The track ids are stored as `<id>:<track_id>` in the cache and the API, so
   that ids of different backends never meet; `plain_ids=True` leaves them as
   they are (only for a backend whose ids were already in the cache).

3. **Test it** the way `tests/test_dj.py` does: a small fake backend, no network.
   Nothing else needs a change: the backends, their sign-in fields and codes come
   from `GET /api/auth`.

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . \
  && .venv/bin/mypy yamusic_radio && .venv/bin/pytest -q
```

## RPM and Docker

A tag `vX.Y.Z` pushed to GitHub builds the RPM (the version of the package is
the tag without the `v`) and publishes it as a release; commits and pull
requests only build it (`.github/workflows/rpm.yml`).

`yamusic-radio.spec` builds an RPM that holds one executable,
`/usr/bin/yamusic-dj` (frozen with PyInstaller: Python and all libraries are
inside, also those of the YouTube Music backend), and a user unit `yamusic-dj.service`. The only dependency is
`/usr/bin/ffmpeg` with libmp3lame (from RPM Fusion; `ffmpeg-free` has none).
The build needs network access, to fetch the libraries from PyPI.

```bash
git archive --prefix=yamusic-radio-0.1.0/ -o ~/rpmbuild/SOURCES/yamusic-radio-0.1.0.tar.gz HEAD
rpmbuild -bb yamusic-radio.spec
systemctl --user enable --now yamusic-dj
```

The `Dockerfile` builds an image on Fedora with ffmpeg from RPM Fusion; the
token and the cache live in `/data`, so mount a volume there:

```bash
docker build --build-arg VERSION=0.1.0 -t yamusic-radio .
docker run -p 8000:8000 -v radio-data:/data yamusic-radio
```

## License

The source code is LGPL-3.0-or-later (`COPYING.LESSER`, which adds to the GPL
text in `LICENSE`).

**Binaries that include Essentia are distributed under the AGPL-3.0.** The RPM
and the Docker image bundle [Essentia](https://essentia.upf.edu/) (AGPL-3.0)
for the key detection, so they are a combined work that you may use and pass
on only under the terms of the AGPL-3.0, among them the right of every user of
the radio over the network to get the source: this repository is that source.
Without Essentia (`pip install .`) the program is LGPL-3.0-or-later and finds
the key in the simpler way.

The other libraries in the binary are under permissive licenses (Apache-2.0,
MIT, BSD, PSF-2.0) and MPL-2.0 (certifi); yandex-music is LGPL-3.0.
