# syntax=docker/dockerfile:1
# python:3.12-slim, pinned by digest (multi-arch index: linux/amd64, linux/arm64 and more).
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

# The image workflow passes the yt-dlp version it checked on PyPI; empty means the newest.
ARG YT_DLP_VERSION=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY pyproject.toml README.md LICENSE NOTICE /src/
COPY tastr_sorceress /src/tastr_sorceress

# yt-dlp[default] brings yt-dlp-ejs (YouTube's JavaScript challenges); deno runs them.
# No ffmpeg: audio is kept in the format the site serves it in.
RUN set -eu; \
    case "$YT_DLP_VERSION" in \
      "") ytdlp="yt-dlp[default]" ;; \
      *[!0-9.]*) echo "bad YT_DLP_VERSION" >&2; exit 1 ;; \
      *) ytdlp="yt-dlp[default]==$YT_DLP_VERSION" ;; \
    esac; \
    pip install "$ytdlp" deno /src; \
    rm -rf /src; \
    deno --version; \
    useradd --system --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin sorceress; \
    mkdir -p /data; \
    chown 10001:10001 /data; \
    chmod 700 /data

# Everything that writes goes to /data (the key only) or /tmp (a tmpfs), so the
# root filesystem can be read-only.
ENV SORCERESS_DATA=/data \
    SORCERESS_TMP=/tmp \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    DENO_DIR=/tmp/.deno \
    DENO_NO_UPDATE_CHECK=1

USER 10001:10001
WORKDIR /tmp
VOLUME ["/data"]

LABEL org.opencontainers.image.title="tastr-sorceress" \
      org.opencontainers.image.description="Fetches audio from music links for your own Tastr account, from your home connection." \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["tastr-sorceress"]
CMD ["run"]
