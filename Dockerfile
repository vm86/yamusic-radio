# syntax=docker/dockerfile:1
ARG FEDORA_VERSION=44

FROM registry.fedoraproject.org/fedora:${FEDORA_VERSION} AS build

# There is no .git in the build context, so setuptools-scm can't tell the
# version; pass it in: docker build --build-arg VERSION=1.2.3 .
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN dnf install -y --setopt=install_weak_deps=False python3 python3-pip \
    && dnf clean all

WORKDIR /src
COPY pyproject.toml LICENSE COPYING.LESSER MANIFEST.in ./
COPY yamusic_radio ./yamusic_radio
# Essentia finds the keys; it has wheels for x86_64 only, elsewhere the
# program falls back to a simpler (and worse) key detection.
RUN python3 -m venv /opt/venv \
    && (/opt/venv/bin/pip install ".[key]" || /opt/venv/bin/pip install .)


FROM registry.fedoraproject.org/fedora:${FEDORA_VERSION}

# ffmpeg-free has no libmp3lame, which the MP3 output needs: take ffmpeg
# from RPM Fusion.
RUN dnf install -y --setopt=install_weak_deps=False \
        "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" \
    && dnf install -y --setopt=install_weak_deps=False python3 ffmpeg libatomic \
    && dnf clean all \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame \
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
