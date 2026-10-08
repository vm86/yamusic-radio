# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build

# There is no .git in the build context, so setuptools-scm can't tell the
# version; pass it in: docker build --build-arg VERSION=1.2.3 .
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml LICENSE COPYING.LESSER MANIFEST.in ./
COPY yandex_music_radio_dj ./yandex_music_radio_dj
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install .


FROM python:3.12-slim

# ffmpeg from Debian has libmp3lame, which the MP3 output needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /home/radio radio \
    && mkdir /data && chown radio /data

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER 10001
WORKDIR /home/radio
VOLUME /data
EXPOSE 8000

# The token and the cache live in /data: mount a volume there.
ENTRYPOINT ["yamusic-dj", "--foreground", "--host", "0.0.0.0", \
    "--config", "/data/config.json", "--cache", "/data/cache.db"]
