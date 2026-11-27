"""Finding a song on YouTube by its title and artist, for a job that has no link (docs/api.md, "search" jobs).

The search is yt-dlp's YouTube search (`ytsearch`), with only YouTube's extractors loaded. It reads the result list
without fetching anything, logs each result, and picks one:

- its link passes the allowlist (both this helper's and the site's), and it isn't live or longer than the limit;
- with the track's length known (`duration_s`), the result closest to it, within LENGTH_SLACK_S or LENGTH_SLACK
  of it, whichever is larger; none that close fails the job, so the wrong version is never saved;
- without a length, the first result whose title has the song's title in it, else the first result.

The worker then fetches the picked link like any other, after the same allowlist check.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from .fetch import _YdlLogger, clean_detail, map_error, public_addresses_only, url_allowed
from .plugins import FetchFailed

log = logging.getLogger(__name__)

RESULTS = 8
LENGTH_SLACK_S = 10.0
LENGTH_SLACK = 0.05
SEARCH_EXTRACTORS = [r"youtube(:.+)?"]  # the search ("youtube:search") and nothing else
_WORD = re.compile(r"[\w']+")


def query_for(hints: dict[str, Any]) -> str:
    """artist, title and the person's extra words, in that order."""
    return " ".join(str(hints[k]).strip() for k in ("artist", "title", "words") if hints.get(k))[:300]


def clock(seconds: float | None) -> str:
    if not isinstance(seconds, (int, float)):
        return "?:??"
    s = int(round(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _link(entry: dict[str, Any]) -> str | None:
    url = entry.get("webpage_url") or entry.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        return url
    vid = entry.get("id")
    if isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def _words(text: object) -> set[str]:
    return {w.casefold() for w in _WORD.findall(str(text or ""))}


def pick(entries: Iterable[dict[str, Any]], hints: dict[str, Any], limits: dict[str, int],
         server_hosts: Iterable[str] | None) -> tuple[str, dict[str, Any]]:
    """The link to fetch and its entry. Raises FetchFailed("unavailable") when nothing fits."""
    target = hints.get("duration_s")
    usable: list[tuple[str, dict[str, Any]]] = []
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        link, title, length = _link(entry), entry.get("title"), entry.get("duration")
        why = None
        if link is None or not url_allowed(link, server_hosts):
            why = "not on the allowlist"
        elif entry.get("live_status") in ("is_live", "is_upcoming") or entry.get("is_live"):
            why = "live"
        elif isinstance(length, (int, float)) and length > limits["max_seconds"]:
            why = "longer than the limit"
        log.info("Result %d: %s (%s) by %s, %s%s", n, title or "?", clock(length),
                 entry.get("channel") or entry.get("uploader") or "?", link or "no link", f": skipped, {why}" if why else "")
        if why is None:
            usable.append((link, entry))
    if not usable:
        raise FetchFailed("unavailable", "the search found nothing to fetch")
    if isinstance(target, (int, float)) and target > 0:
        timed = [(abs(e["duration"] - target), link, e) for link, e in usable
                 if isinstance(e.get("duration"), (int, float))]
        slack = max(LENGTH_SLACK_S, target * LENGTH_SLACK)
        if not timed or min(timed, key=lambda t: t[0])[0] > slack:
            nearest = min(timed, key=lambda t: t[0]) if timed else None
            raise FetchFailed("unavailable", f"no result is close to the track's length of {clock(target)}"
                              + (f" (nearest: {nearest[2].get('title')!r}, {clock(nearest[2]['duration'])})"
                                 if nearest else "") + "; add words such as \"extended mix\", or a link")
        _, link, entry = min(timed, key=lambda t: t[0])
        log.info("Picked %s: %s, %s, closest to the track's %s", link, entry.get("title"),
                 clock(entry.get("duration")), clock(target))
        return link, entry
    wanted = _words(hints.get("title"))
    for link, entry in usable:
        if wanted and wanted <= _words(entry.get("title")):
            log.info("Picked %s: %s, the first result with the title in it", link, entry.get("title"))
            return link, entry
    link, entry = usable[0]
    log.info("Picked %s: %s, the first result (no length to compare)", link, entry.get("title"))
    return link, entry


def find_song(hints: dict[str, Any], limits: dict[str, int], *, server_hosts: Iterable[str] | None = None,
              cache_dir: str | None = None, ydl_class=None, results: int = RESULTS) -> str:
    """Search YouTube for the song in `hints` and return the link to fetch. Raises FetchFailed."""
    query = query_for(hints)
    if not hints.get("title") or not query:
        raise FetchFailed("unavailable", "a search needs at least the song's title")
    from yt_dlp.utils import DownloadError

    if ydl_class is None:
        from yt_dlp import YoutubeDL as ydl_class
    params = {"extract_flat": "in_playlist", "allowed_extractors": list(SEARCH_EXTRACTORS), "quiet": True,
              "noprogress": True, "logger": _YdlLogger(), "cachedir": cache_dir or False, "socket_timeout": 30,
              "playlistend": results}
    log.info("Searching YouTube for %r%s", query,
             f", for a track {clock(hints['duration_s'])} long" if hints.get("duration_s") else "")
    with public_addresses_only(), ydl_class(params) as ydl:
        try:
            found = ydl.extract_info(f"ytsearch{results}:{query}", download=False)
        except DownloadError as e:
            raise FetchFailed(map_error(str(e)), clean_detail(str(e))) from None
    entries = list((found or {}).get("entries") or [])
    log.info("The search gave %d result%s", len(entries), "" if len(entries) == 1 else "s")
    link, _ = pick(entries, hints, limits, server_hosts)
    return link
