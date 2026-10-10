# yamusic-radio

Endless DJ radio from a Yandex Music station. Downloads tracks, beat-matches
them (bass swap, three-band EQ blend, filter sweeps, optional effects) and streams one continuous
MP3 over HTTP with ICY titles.

## Run

Needs Python >= 3.12 and `ffmpeg` with libmp3lame in `PATH`.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test,lint]"
.venv/bin/yamusic-dj --wait --port 8000
.venv/bin/yamusic-dj --mood-arc --effects full --host 0.0.0.0
# listen: http://127.0.0.1:8000/stream
```

On the first run the URL and the code of the OAuth device flow are in the log
and at `GET /api/auth` (the server is already up, so
`curl -s localhost:8000/api/auth` works before anyone has logged in). The
token is stored, mode 0600, in `~/.config/yamusic-radio.json`
(`--config` to change). The log is `~/.cache/yamusic_radio.log`
(or `--debug` to stderr).

Track analyses and play times (never audio) are cached in
`~/.cache/yamusic-radio.db`, so a restart does not repeat the last
tracks or analyze known ones again. The genre of the track (from its album) is
kept in the same table; the API does not show it. `--cache PATH` moves it, `--no-cache`
turns it off.

The moods and the genres that were picked are kept in the cache and picked
again at the next start (not with `--no-cache`). The mood setting of the
station on the Yandex account is put back when the radio stops; the original
setting is kept in the cache for as long as the account has another one, so
if the radio is killed the next run puts it back first.

A new listener gets the latest 3 s of the stream at once (from a frame
boundary), so the player can start at once instead of after the ~1.5 s that a
browser waits for audio arriving at the pace it plays; the price is that the
listener is ~3 s behind the encoder. `PREROLL_SECONDS` in `dj.py`.

The stream has no authentication: bind only to trusted networks (`--host`).

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
play/pause and volume, the current track with its progress, the previous and
the next tracks, mood and genre chips, the transition statistics, and the
authorization code while the app has no token. The theme is light, dark or
the system's, set by the button in the top bar (each press: system, light, dark) and remembered.

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

## REST API

Served next to the stream, no authentication.

| Request | Answer |
|---|---|
| `GET /api/queue?previous=2&next=15` | `previous` (oldest first), `now`, `next` and `moods`. Both counts are capped at 15; the defaults are 2 and 15. |
| `POST /api/queue/refresh` | Choose the next tracks anew among the ones already loaded: the track already chosen and decoded goes back to the pool, tracks of moods and genres that are not selected are dropped (the genres picked since come into force here). Tracks of other genres are not downloaded; one without a genre is not played while genres are selected. Answers like `GET /api/queue`. |
| `GET /api/genres` | `available` (the genres of the tracks heard so far, the most common first), `selected` (picked) and `applied` (the queue is built from these). Empty is any genre. |
| `PUT /api/genres` `{"genres": ["house"]}` | Pick genres. Nothing is rebuilt: they come into force with the next `POST /api/queue/refresh`. `DELETE /api/genres` picks any. |
| `PUT /api/genres/blocked` `{"genres": ["rap"]}` | Genres never to play (they win over the picked ones); `blocked` and `blocked_applied` in `GET /api/genres`. Kept in the cache, in force with the next refresh like the picked ones. `DELETE /api/genres/blocked` clears them. |
| `GET /api/stats` | How the tracks were blended: `session` (this run) and `all_time` (from the cache, `null` without one), each with `total`, `beat_matched`, `by_type` (`bass swap`, `eq blend`, `filter sweep`, `crossfade`: what was really made, a blend too short for a filtered one is a crossfade), `by_effect` (`none`, `drop`: a riser and a boom, `echo`: an echo-out; `unknown` for blends made before the effects were counted) and `by_key` (`same`, `neighbour`, `boost`, `semitone`, `down`, `diagonal`, `clash`, `unknown`) and `by_quality` (the same, summed up: `good` is `same` and `neighbour`, `fair` is `boost`, `semitone`, `down` and `diagonal`, `poor` is `clash`). |
| `GET /api/stats/keys` | Every change of key there was, with how often: `session` and `all_time` (`null` without a cache), each a list of `{"from": "8A", "to": "9A", "relation", "quality", "count"}`, the most common first. A key that was not found is `null`. The page does not show it. |
| `POST /api/next` | The next track now: it needs the next track to be ready, and the blend starts on the first bar line of the playing track at least 2 s ahead, as smooth as any other (16 beats; the tempo glides, bass swap / EQ blend / sweep). Answers 202 with the queue (`skip_pending: true` until the blend has begun), 409 if nothing plays yet or a transition is already under way. The station is told the track was skipped. |
| `GET /api/transitions` | The last 10 blends, the newest last: `from`, `to`, `kind`, `effect`, `beat_matched`, `from_bpm`, `to_bpm`, `glide` (how far the tempo glided, `0.05` is 5% faster), `seconds`, `loudness_gap_db`, `key_relation`, `over_db` (how far above the limiter's ceiling the blend peaked before it was limited) and `at`. To look at a transition that did not sound right. |
| `GET /api/auth` | `authorized`, and while the app has no token, the `url` to open and the `code` to enter there (both `null` once authorized, or until Yandex has given them). The server is up during authorization. |
| `GET /api/moods` | `available` (`sad`, `calm`, `fun`, `active`), `selected`, and the `station` mood now. |
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
account** while the radio runs (put back on exit or `DELETE`). The track that
is already decoded keeps its mood; tracks of other moods in the pool are
dropped and fetched anew.

```bash
curl -s localhost:8000/api/queue | jq .
curl -s -X PUT localhost:8000/api/moods -d '{"moods":["calm","fun"]}'
```

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

LGPL-3.0-or-later.
