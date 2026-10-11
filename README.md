# yamusic-radio

Endless DJ radio from a music service. Takes tracks from a Yandex Music
station (or from any other service you add as a [backend](#adding-a-music-backend),
several at once), beat-matches them (bass swap, three-band EQ blend, filter
sweeps, optional effects) and streams one continuous mix over HTTP: MP3 at
320 kbit/s with ICY titles, and a lossless FLAC stream.

## Run

Needs Python >= 3.12 and `ffmpeg` with libmp3lame in `PATH`.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test,lint]"
.venv/bin/yamusic-dj --wait --port 8000 --backend yandex
.venv/bin/yamusic-dj --mood-arc --effects full --host 0.0.0.0
# listen: http://127.0.0.1:8000/stream (MP3) or /stream.flac (lossless)
```

**Music backends.** The tracks come from the backends that are on; with
several on, their tracks go into one pool and the radio picks the best fit
among them. A backend is on when it is asked for (`--backend ID`, repeatable,
or the page, or `PUT /api/auth/backend`), when it was on in the last run, or
when it is already signed in (has a saved token). Until one is on the radio
waits and the page offers them. Only `yandex` comes with the program. A backend
that fails is left alone for a while (30 s, doubling up to 10 min) and the
others carry on. `--station ID=STATION` (repeatable) picks the station of a
backend; `--station STATION` is for the first one.

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

The moods, the genres and the backends that were picked are kept in the cache
and picked again at the next start (not with `--no-cache`). A backend that
steers its station by the mood (Yandex does) has the mood setting of the
station on the account put back when the radio stops; the original setting is
kept in the cache for as long as the account has another one, so if the radio
is killed the next run puts it back first. For a backend that does not, the
radio finds the mood of each track by its sound (energy and key).

## Streams

Both streams carry the same mix and are always on (the radio runs two
encoders, one `ffmpeg` for each):

| URL | Format | Notes |
|---|---|---|
| `/stream` | MP3, 320 kbit/s (`MP3_BITRATE` in `dj.py`; `libmp3lame`) | With ICY titles (`Icy-MetaData: 1`). Plays in any player. |
| `/stream.flac` | FLAC, lossless, 16 bit | The mix is dithered to 16 bit (triangular dither, as for a CD). No titles: the stream carries no tags. About 110 kB a second, so mind the network. |

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
`/stream` is sent with `Access-Control-Allow-Origin: *`, so a page of another
origin can play it in an `<audio crossorigin>` and analyze the sound with Web
Audio.

## Key detection

The key is found by [Essentia](https://essentia.upf.edu/) (`pip install
".[key]"`; wheels exist for x86_64, the Docker image has it, and it needs
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

## Web page

`http://127.0.0.1:8000/` is a Material Design 3 page (works on a phone):
play/pause (it stops and starts this page's own listening; the radio stands while nobody listens, and the page shows that it is paused, and stops itself when the stream goes away), a switch between the MP3 and the FLAC stream (remembered) and volume, the current track with its progress and, under the cover, the spectrum of the mix (12 bars, in time with the sound), the previous and
the next tracks, mood chips, the genres (tabs *Play* and *Never play*), the
transition statistics, and the backends: which are on, the sign-in of each
(the code of a device flow, or the fields it asks for). The theme is light,
dark or the system's, set by the button in the top bar (each press: system,
light, dark) and remembered. The page is in Russian or English, by the system
language by default, and the button in the top bar changes it.

It is a Vue 3 + TypeScript app in `frontend/`. The build goes into
`yamusic_radio/web/` and **is committed**, so that the package, the RPM
and the image need no Node.js. After a change in `frontend/`:

```bash
cd frontend
npm ci
npm run build      # type check + build into ../yamusic_radio/web
npm test           # vitest
npm run dev        # Vite with a proxy to the radio on 127.0.0.1:8000
npm run dev:remote # the same with the API of another radio (RADIO_URL=https://radio.example.com)
```

## Spectrum

The radio mixes the sound itself, so it knows how loud each band is at every
moment; a page cannot get that from the stream it plays (it needs CORS and
it hears the sound ~3 s late). So the radio sends the bands over
`GET /api/spectrum`, each with its position in the mix, and the page shows
them when its player gets there.

To tie the two together, the page gives its connection a name: it opens the
stream as `/stream?l=NAME` (or `/stream.flac?l=NAME`) and the spectrum as
`/api/spectrum?l=NAME`. The radio notes where in the mix that stream starts
(a new listener gets the latest 3 s at once) and sends it first as the `start`
event. A page of another origin can do the same: both URLs are sent with
`Access-Control-Allow-Origin: *`. The analysis is in `yamusic_radio/spectrum.py`
(`BANDS`, `LOW_HZ`, `HIGH_HZ`, `FLOOR_DB`).

## REST API

Served next to the stream, no authentication.

| Request | Answer |
|---|---|
| `GET /api/queue?previous=2&next=15` | `previous` (oldest first), `now` (with `elapsed_seconds`, which does not run while the mix is paused), `next`, `moods`, the state of the next track button (`skip_pending`, `blending`, `can_skip`, as in `GET /api/next`) and the pause (`paused`, `pause_reason`, as in `POST /api/pause`). Both counts are capped at 15; the defaults are 2 and 15. |
| `POST /api/queue/refresh` | Choose the next tracks anew among the ones already loaded: the track already chosen and decoded goes back to the pool, tracks of moods and genres that are not selected are dropped (the genres picked since come into force here). Tracks of other genres are not downloaded; one without a genre is not played while genres are selected. Answers like `GET /api/queue`. |
| `GET /api/genres` | `available` (the genres of the tracks heard so far, the most common first), `selected` (picked) and `applied` (the queue is built from these). Empty is any genre. |
| `PUT /api/genres` `{"genres": ["house"]}` | Pick genres. Nothing is rebuilt: they come into force with the next `POST /api/queue/refresh`. `DELETE /api/genres` picks any. |
| `PUT /api/genres/blocked` `{"genres": ["rap"]}` | Genres never to play (they win over the picked ones); `blocked` and `blocked_applied` in `GET /api/genres`. Kept in the cache, in force with the next refresh like the picked ones. `DELETE /api/genres/blocked` clears them. |
| `GET /api/stats` | How the tracks were blended: `session` (this run) and `all_time` (from the cache, `null` without one), each with `total`, `beat_matched`, `by_type` (`bass swap`, `eq blend`, `filter sweep`, `crossfade`: what was really made, a blend too short for a filtered one is a crossfade), `by_effect` (`none`, `drop`: a riser and a boom, `echo`: an echo-out; `unknown` for blends made before the effects were counted) and `by_key` (`same`, `neighbour`, `boost`, `semitone`, `down`, `diagonal`, `clash`, `unknown`) and `by_quality` (the same, summed up: `good` is `same` and `neighbour`, `fair` is `boost`, `semitone`, `down` and `diagonal`, `poor` is `clash`). |
| `GET /api/stats/keys` | Every change of key there was, with how often: `session` and `all_time` (`null` without a cache), each a list of `{"from": "8A", "to": "9A", "relation", "quality", "count"}`, the most common first. A key that was not found is `null`. The page does not show it. |
| `POST /api/next` | The next track now: it needs the next track to be ready, and the blend starts on the first bar line of the playing track at least 2 s ahead, as smooth as any other (16 beats; the tempo glides, bass swap / EQ blend / sweep). Answers 202 with the queue (`skip_pending: true` until the blend has begun), 409 if nothing plays yet or a transition is already under way. The station is told the track was skipped. |
| `GET /api/next` | Where the next track button is: `skip_pending` (asked for, waiting for a good beat), `blending` (the blend is under way, until the old track is left behind) and `can_skip` (something plays and neither of the two). The page drives the button by this. |
| `POST /api/pause`, `POST /api/resume` | Pause the mix for everyone, and go on. The time of the playing track does not run meanwhile. Answer `{"paused": true, "pause_reason": "user"}`, or `{"paused": false, "pause_reason": null}` after a resume. |
| `GET /api/spectrum?l=NAME` | The spectrum of the mix as server-sent events (`text/event-stream`, CORS `*`): 12 bands (30 Hz to 16 kHz, log-spaced, a little tilted up towards the treble), about 30 times a second. First `event: start` with `{"position": SECONDS}`: where in the mix the player of the page starts (`null` if it did not connect within 5 s). Then each value is `data: {"t": 12.345, "b": [0..1, ...]}`: `t` is the position in the mix, `b` the levels of the bands (0 at -60 dBFS, 1 at full scale). `: keepalive` comments come every 15 s. Frames are sent a little ahead (the player hears the sound ~3 s after the mix made it): show each one when the player gets to `start + t`. |
| `GET /api/transitions` | The last 10 blends, the newest last: `from`, `to`, `kind`, `effect`, `beat_matched`, `from_bpm`, `to_bpm`, `glide` (how far the tempo glided, `0.05` is 5% faster), `seconds`, `loudness_gap_db`, `key_relation`, `over_db` (how far above the limiter's ceiling the blend peaked before it was limited) and `at`. To look at a transition that did not sound right. |
| `GET /api/auth` | `authorized` (a backend can give tracks), and while it cannot, what to do to sign in: the `url` to open and the `code` to enter there (both `null` once authorized, or until the service has given them) and `fields` (`{"name", "label", "secret"}`: values to type, `[]` for a device flow). `backends` lists every backend with the same keys plus `id`, `name`, `enabled` and `available` (signed in and not resting after failures). The server is up during authorization. |
| `POST /api/auth` `{"backend": "id", "values": {"login": "..."}}` | Sign in with the values of the `fields` of the backend (without `backend`, of the radio's own). Answers like `GET /api/auth`; 400 if the backend turns them down. |
| `PUT /api/auth/backend` `{"backend": "id"}` | Turn the backend on; it starts signing in if it needs to. Answers like `GET /api/auth`; 400 for an unknown id. |
| `DELETE /api/auth/backend/{id}` | Turn the backend off. The tracks it gave already stay in the pool. Answers like `GET /api/auth`; 404 for an unknown id. |
| `GET /api/moods` | `available` (`sad`, `calm`, `fun`, `active`), `selected`, the `station` mood now and `steered` (whether the backends steer their stations by the mood; if not, the radio judges the tracks by their sound). |
| `PUT /api/moods` `{"moods": ["calm", "fun"]}` | Play only these moods. |
| `DELETE /api/moods` | Any mood again. |

Each track has `track_id`, `title`, `artists`, `bpm`, `camelot` (Camelot Wheel code such as `8A`, `""` when the key is unknown), `key` (`A minor`), `loudness_db`,
`duration_seconds`, `mood`, `cover` (the URL of the cover on Yandex's server
with `%%` where the size goes, e.g. `.../%%` -> `.../200x200`; `""` when there
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
the backend what was heard. Yandex Music is one implementation
(`ya_player.py`); a Subsonic server, a radio or a folder of files is another.

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

2. **Register it** in `backend_specs()` in `yamusic_radio/main.py`, and add its
   id to `BACKEND_IDS` (the choices of `--backend` and `--station`):

   ```python
   BackendSpec(
       "subsonic",                       # the id
       "Subsonic",                       # the name on the page
       lambda: SubsonicBackend(options.subsonic_url, session),
       lambda: bool(options.subsonic_url),  # configured: on without being asked
   )
   ```

   The track ids are stored as `<id>:<track_id>` in the cache and the API, so
   that ids of different backends never meet; `plain_ids=True` leaves them as
   they are (only for a backend whose ids were already in the cache).

3. **Test it** the way `tests/test_dj.py` does: a small fake backend, no network.
   The page needs no change: the backends, their sign-in fields and codes come
   from `GET /api/auth`.

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . \
  && .venv/bin/mypy yamusic_radio && .venv/bin/pytest -q
```

## RPM and Docker

`yamusic-radio.spec` builds an RPM that holds one executable,
`/usr/bin/yamusic-dj` (frozen with PyInstaller: Python and all libraries are
inside), and a user unit `yamusic-dj.service`. The only dependency is
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
