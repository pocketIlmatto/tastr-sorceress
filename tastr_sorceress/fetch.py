"""Checking links and fetching their audio with yt-dlp."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# The same list the site enforces (docs/api.md). A link must match it here AND the
# list the site sends with the job.
ALLOW_HOSTS = (
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "soundcloud.com",
    "bandcamp.com",
    "mixcloud.com",
)

# yt-dlp extractor names (regexes, matched in full). The generic extractor, which
# would fetch from any web page, is never loaded.
EXTRACTORS = [r"youtube(:.+)?", r"soundcloud(:.+)?", r"bandcamp(:.+)?", r"mixcloud(:.+)?"]

DEFAULT_LIMITS = {"max_seconds": 10800, "max_bytes": 1 << 30, "part_bytes": 32 << 20}

INFO_FIELDS = ("id", "extractor", "title", "duration", "uploader", "artist", "webpage_url", "upload_date")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_EXT = re.compile(r"^[a-z0-9]{1,10}$")


class FetchFailed(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail[:500]


@dataclass
class Fetched:
    path: str
    size: int
    ext: str
    info: dict[str, Any]


def _match(host: str, entries: Iterable[str]) -> bool:
    for entry in entries:
        entry = str(entry).strip().lower().rstrip(".")
        if entry and (host == entry or host.endswith("." + entry)):
            return True
    return False


def url_allowed(url: object, server_hosts: Iterable[str] | None = None) -> bool:
    """https only, no login or odd port, host equal to or under an allowed entry,
    on both this helper's list and the site's list for the job."""
    if not isinstance(url, str) or len(url) > 2048 or not url.isprintable() or " " in url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return False
    if port not in (None, 443):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not _match(host, ALLOW_HOSTS):
        return False
    if server_hosts is not None and not _match(host, server_hosts):
        return False
    return True


def clean_limits(raw: object) -> dict[str, int]:
    limits = dict(DEFAULT_LIMITS)
    if isinstance(raw, dict):
        for name in limits:
            value = raw.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                limits[name] = int(value)
    return limits


def map_error(message: str) -> str:
    """Turn a yt-dlp error message into one of the reasons in docs/api.md."""
    low = _ANSI.sub("", message).lower().replace("’", "'")
    if "not a bot" in low or ("sign in to confirm" in low and "age" not in low) or "http error 429" in low:
        return "bot_check"
    if "no suitable extractor" in low or "unsupported url" in low:
        return "not_allowed"
    if "larger than max-filesize" in low:
        return "too_big"
    if any(s in low for s in ("live event will begin", "premieres in", "premiere will begin", "currently live")):
        return "live"
    if "requested format is not available" in low:
        return "error"
    if any(
        s in low
        for s in (
            "unavailable",
            "not available",
            "removed",
            "private video",
            "video is private",
            "deleted",
            "does not exist",
            "terminated",
            "copyright",
            "http error 404",
            "http error 410",
            "no longer",
            "confirm your age",
            "age-restricted",
            "members-only",
            "join this channel",
            "geo restrict",
            "not found",
        )
    ):
        return "unavailable"
    return "error"


def clean_detail(message: str) -> str:
    text = _ANSI.sub("", message).strip()
    if text.startswith("ERROR: "):
        text = text[7:]
    return text[:500]


class _TooBigError(Exception):
    pass


def _too_big_class():
    """The exception the progress hook raises. A DownloadCancelled subclass, so
    yt-dlp passes it straight up instead of retrying."""
    from yt_dlp.utils import DownloadCancelled

    class TooBig(DownloadCancelled, _TooBigError):
        msg = "the download is larger than the size limit"

    return TooBig


class _YdlLogger:
    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp warning: %s", _ANSI.sub("", msg))

    def error(self, msg: str) -> None:
        log.info("yt-dlp error: %s", _ANSI.sub("", msg))


def ydl_options(workdir: str, cache_dir: str | None, max_bytes: int, hook) -> dict[str, Any]:
    return {
        "format": "bestaudio/best",
        "noplaylist": True,
        "extract_flat": "in_playlist",  # never resolve playlist entries; playlists are refused
        "allowed_extractors": list(EXTRACTORS),
        "outtmpl": {"default": os.path.join(workdir, "audio.%(ext)s")},
        "paths": {"home": workdir, "temp": workdir},
        "postprocessors": [],  # no ffmpeg: keep the native audio format
        "fixup": "never",
        "overwrites": True,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeinfojson": False,
        "skip_unavailable_fragments": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [hook],
        "cachedir": cache_dir or False,
        "quiet": True,
        "noprogress": True,
        "logger": _YdlLogger(),
    }


def check_info(info: Any, limits: dict[str, int], server_hosts: Iterable[str] | None) -> None:
    if not isinstance(info, dict):
        raise FetchFailed("error", "yt-dlp returned nothing for this link")
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        raise FetchFailed("playlist", "the link is a playlist; send a link to a single track")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise FetchFailed("live", "the link is a live stream")
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration > limits["max_seconds"]:
        raise FetchFailed("too_long", f"{duration:.0f} s is longer than the limit of {limits['max_seconds']} s")
    size = info.get("filesize")
    if isinstance(size, int) and size > limits["max_bytes"]:
        raise FetchFailed("too_big", f"{size} bytes is more than the limit of {limits['max_bytes']} bytes")
    page = info.get("webpage_url")
    if page is not None and not url_allowed(page, server_hosts):
        raise FetchFailed("not_allowed", "the link leads to a site that is not on the allowlist")


def pick_info(info: dict[str, Any]) -> dict[str, Any]:
    artist = info.get("artist")
    if not artist and isinstance(info.get("artists"), list):
        artist = ", ".join(str(a) for a in info["artists"])
    out: dict[str, Any] = {}
    for name in INFO_FIELDS:
        value = artist if name == "artist" else info.get(name)
        if isinstance(value, str):
            value = value[:1000]
        elif name == "duration" and isinstance(value, (int, float)) and not isinstance(value, bool):
            pass
        elif value is not None:
            value = str(value)[:1000]
        out[name] = value
    return out


def fetch(
    url: str,
    limits: dict[str, int],
    workdir: str,
    *,
    server_hosts: Iterable[str] | None = None,
    cache_dir: str | None = None,
    ydl_class=None,
) -> Fetched:
    """Fetch the audio of one link into `workdir`. Raises FetchFailed with a reason."""
    if not url_allowed(url, server_hosts):
        raise FetchFailed("not_allowed", "the link is not on the allowlist")
    from yt_dlp.utils import DownloadError

    if ydl_class is None:
        from yt_dlp import YoutubeDL as ydl_class

    TooBig = _too_big_class()
    max_bytes = limits["max_bytes"]
    state = {"too_big": False}

    def hook(d: dict[str, Any]) -> None:
        for key in ("downloaded_bytes", "total_bytes"):
            value = d.get(key)
            if isinstance(value, (int, float)) and value > max_bytes:
                state["too_big"] = True
                raise TooBig()

    with ydl_class(ydl_options(workdir, cache_dir, max_bytes, hook)) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            raise FetchFailed(map_error(str(e)), clean_detail(str(e))) from None
        check_info(info, limits, server_hosts)
        try:
            ydl.process_ie_result(info, download=True)
        except _TooBigError:
            state["too_big"] = True
        except DownloadError as e:
            if not state["too_big"]:
                raise FetchFailed(map_error(str(e)), clean_detail(str(e))) from None
    if state["too_big"]:
        raise FetchFailed("too_big", f"the download is larger than the limit of {max_bytes} bytes")

    files = [
        os.path.join(workdir, f)
        for f in os.listdir(workdir)
        if f.startswith("audio.") and not f.endswith((".part", ".ytdl", ".temp", ".tmp"))
    ]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise FetchFailed("error", "yt-dlp finished without writing a file")
    path = max(files, key=os.path.getsize)
    size = os.path.getsize(path)
    if size == 0:
        raise FetchFailed("error", "the download was empty")
    if size > max_bytes:
        raise FetchFailed("too_big", f"{size} bytes is more than the limit of {max_bytes} bytes")
    ext = path.rsplit(".", 1)[-1].lower()
    if not _EXT.match(ext):
        raise FetchFailed("error", "the download has an unexpected file type")
    return Fetched(path=path, size=size, ext=ext, info=pick_info(info))
