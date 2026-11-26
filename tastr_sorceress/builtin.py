"""The built-in source (yt-dlp) and steps (copy, convert)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import unicodedata
from typing import Any, Callable

from .config import ConfigError
from .fetch import fetch
from .plugins import Fetched, FetchFailed, Job

# --- yt-dlp ------------------------------------------------------------------


class YtDlpSource:
    """Today's fetch, unchanged: the allowlist, the allowed extractors only, the size and length limits and the
    private-address guard all live in `fetch.fetch`."""

    name = "yt-dlp"

    def __init__(self, options: dict[str, Any] | None = None, *, fetcher: Callable[..., Fetched] = fetch,
                 cache_dir: str | None = None):
        if options:
            raise ConfigError("yt-dlp takes no options")
        self.fetcher = fetcher
        self.cache_dir = cache_dir

    def fetch(self, job: Job, workdir: str, log: logging.Logger) -> Fetched:
        hosts = list(job.allow_hosts) if job.allow_hosts is not None else None
        return self.fetcher(job.url, job.limits, workdir, server_hosts=hosts, cache_dir=self.cache_dir)


# --- copy --------------------------------------------------------------------

_UNSAFE = re.compile(r"[^\w .,()\[\]'&+-]+")
MAX_NAME = 150


def safe_filename(info: dict[str, Any], ext: str) -> str:
    """A file name made from the title (or the id), that can't name a folder or leave the copy folder: no path
    separators, no leading dots, no control characters, at most MAX_NAME characters before the extension."""
    for candidate in (info.get("title"), info.get("id"), "audio"):
        if not isinstance(candidate, str):
            continue
        text = unicodedata.normalize("NFC", candidate)
        text = "".join(ch for ch in text if ch.isprintable())
        text = _UNSAFE.sub("_", text).strip(" ._")
        text = text[:MAX_NAME].strip(" ._")
        if text:
            return f"{text}.{ext}"
    return f"audio.{ext}"  # pragma: no cover - "audio" always gives a name


class CopyStep:
    """Keeps a copy of the final file in `[copy] folder`. Nothing is written outside that folder, and an existing
    file is never overwritten: a second copy of the same title gets " (2)", " (3)", ... added."""

    name = "copy"

    def __init__(self, options: dict[str, Any]):
        folder = options.get("folder")
        if not isinstance(folder, str) or not os.path.isabs(folder):
            raise ConfigError("[copy] folder must be an absolute path")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            raise ConfigError(f"[copy] folder {folder} can't be created: {e}") from None
        if not os.access(folder, os.W_OK | os.X_OK):
            raise ConfigError(f"[copy] folder {folder} is not writable by this helper")
        self.folder = os.path.realpath(folder)

    def run(self, fetched: Fetched, job: Job, log: logging.Logger) -> Fetched:
        name = safe_filename(fetched.info, fetched.ext)
        stem, ext = name.rsplit(".", 1)
        for n in range(1, 1000):
            base = name if n == 1 else f"{stem} ({n}).{ext}"
            if os.path.basename(base) != base or base.startswith("."):
                raise FetchFailed("error", "copy: the file name would leave the copy folder")
            target = os.path.join(self.folder, base)
            try:
                # O_EXCL and O_NOFOLLOW: never overwrite a file or follow a link someone left in the folder.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as out, open(fetched.path, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
            log.info("Kept a copy as %s", target)
            return fetched
        raise FetchFailed("error", "copy: too many files with this name in the copy folder")


# --- convert -----------------------------------------------------------------

# format -> (ffmpeg audio codec, default bitrate or None for lossless)
CODECS = {
    "flac": ("flac", None),
    "mp3": ("libmp3lame", "192k"),
    "opus": ("libopus", "160k"),
    "m4a": ("aac", "192k"),
}
CONVERT_TIMEOUT_S = 3600


class ConvertStep:
    """Converts the file to `[convert] format` with ffmpeg, keeping the tags where the new format can hold them.
    ffmpeg must be on PATH (the default image doesn't have it); without it the helper refuses to start."""

    name = "convert"

    def __init__(self, options: dict[str, Any], *, which: Callable[[str], str | None] | None = None,
                 run: Callable[..., Any] | None = None):
        fmt = options.get("format")
        if fmt not in CODECS:
            raise ConfigError(f"[convert] format must be one of {', '.join(CODECS)}")
        ffmpeg = (which or shutil.which)("ffmpeg")
        if not ffmpeg:
            raise ConfigError("the convert step needs ffmpeg, and there is none on PATH. The default image has "
                              "no ffmpeg; build your own image with it (see the README) or remove \"convert\" "
                              "from steps")
        self.ffmpeg = ffmpeg
        self.format = fmt
        self.bitrate = options.get("bitrate") or CODECS[fmt][1]
        self._run = run or subprocess.run

    def argv(self, src: str, dst: str) -> list[str]:
        codec, _ = CODECS[self.format]
        args = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-protocol_whitelist", "file",  # local files only: a crafted input can't make ffmpeg fetch anything
            "-i", "file:" + src,
            "-map", "0:a:0", "-map_metadata", "0", "-vn",
            "-c:a", codec,
        ]
        if CODECS[self.format][1] is not None:
            args += ["-b:a", self.bitrate]
        return args + ["-y", "file:" + dst]

    def run(self, fetched: Fetched, job: Job, log: logging.Logger) -> Fetched:
        if fetched.ext == self.format:
            log.info("Already %s; not converting", self.format)
            return fetched
        folder = os.path.dirname(fetched.path)
        dst = os.path.join(folder, f"converted.{self.format}")
        argv = self.argv(fetched.path, dst)
        log.debug("Running %s", argv)
        try:
            done = self._run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=CONVERT_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise FetchFailed("error", f"convert: ffmpeg took longer than {CONVERT_TIMEOUT_S} s") from None
        if done.returncode != 0 or not os.path.isfile(dst):
            text = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise FetchFailed("error", "convert: ffmpeg failed" + (f": {text[-1]}" if text else ""))
        log.info("Converted %s to %s", fetched.ext, self.format)
        os.remove(fetched.path)  # only the newest file is kept around while the job runs
        return Fetched(path=dst, ext=self.format, info=dict(fetched.info))
