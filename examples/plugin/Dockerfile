# syntax=docker/dockerfile:1
# Your own image: the official helper plus this plugin, and ffmpeg for the built-in convert step.
#   docker build -t my-sorceress .
# Then use `image: my-sorceress` in compose.yaml. Rebuild it whenever the official image updates
# (yt-dlp changes often), or pin the base by digest and update that on purpose.
FROM ghcr.io/pocketilmatto/tastr-sorceress:latest

USER root

# Only needed for the convert step; leave it out otherwise.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /src/plugin/
COPY sorceress_example /src/plugin/sorceress_example
RUN pip install /src/plugin && rm -rf /src

# Back to the official image's unprivileged user; its entrypoint and settings stay as they are.
USER 10001:10001
