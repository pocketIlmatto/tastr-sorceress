"""An example tastr-sorceress plugin.

- The source `folder` looks for the track in a folder of music you already have, by the artist and title the site
  sends as hints, so a song you own is used instead of fetching it. When it finds nothing it raises NotFound, and
  the helper tries the next source in your config.
- The step `fill-from-hints` fills in the title and artist the site will show when the source didn't know them.
  (A step that writes tags into the file itself would do the same with a tagging library.)

Config:

    sources = ["folder", "yt-dlp"]
    steps = ["fill-from-hints"]

    [plugins.folder]
    folder = "/music"   # mount your music here, read-only

Plugins get the job's link, limits and hints, never the helper's key, and every call runs under the helper's guard
against private network addresses.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from tastr_sorceress.config import ConfigError
from tastr_sorceress.pipeline import SITE_EXTS
from tastr_sorceress.plugins import Fetched, Job, NotFound


def _words(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.casefold()).split())


class FolderSource:
    name = "folder"

    def __init__(self, options: dict[str, Any]):
        folder = options.get("folder")
        if not isinstance(folder, str) or not os.path.isdir(folder):
            raise ConfigError("[plugins.folder] folder must be a folder that exists")
        self.folder = folder

    def fetch(self, job: Job, workdir: str, log: logging.Logger) -> Fetched:
        title, artist = job.hints.get("title"), job.hints.get("artist")
        if not title:
            raise NotFound("the site sent no title to search for")
        found = self._find(_words(title), _words(artist or ""), job.limits["max_bytes"])
        if found is None:
            raise NotFound(f"no file for {artist or '?'} - {title}")
        ext = found.rsplit(".", 1)[-1].lower()
        target = os.path.join(workdir, f"audio.{ext}")
        shutil.copyfile(found, target)  # the helper deletes the job's folder afterwards; the original stays
        log.info("Found %s", found)
        info = {"id": os.path.basename(found), "extractor": "folder", "title": title, "artist": artist}
        return Fetched(path=target, ext=ext, info=info)

    def _find(self, title: str, artist: str, max_bytes: int) -> str | None:
        for root, _dirs, files in os.walk(self.folder):
            for name in sorted(files):
                stem, _, ext = name.rpartition(".")
                words = _words(os.path.join(os.path.relpath(root, self.folder), stem))
                if ext.lower() in SITE_EXTS and title in words and artist in words:
                    path = os.path.join(root, name)
                    if os.path.isfile(path) and os.path.getsize(path) <= max_bytes:
                        return path
        return None


class FillFromHints:
    name = "fill-from-hints"

    def __init__(self, options: dict[str, Any]):
        if options:
            raise ConfigError("fill-from-hints takes no options")

    def run(self, fetched: Fetched, job: Job, log: logging.Logger) -> Fetched:
        info = dict(fetched.info)
        for field in ("title", "artist"):
            if not info.get(field) and job.hints.get(field):
                info[field] = job.hints[field]
                log.debug("Filled in %s from the site's hints", field)
        return Fetched(path=fetched.path, ext=fetched.ext, info=info)
