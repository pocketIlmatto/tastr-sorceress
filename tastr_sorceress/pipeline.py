"""The fetch pipeline: sources tried in turn, then steps in order, then the checks that always hold.

The checks here are what keep a custom pipeline safe for the site, whatever its plugins do:

- the job's link has passed the allowlist before any source runs (the worker checks it first);
- every plugin call runs under the private-address guard, except for the hosts in [network] local_hosts;
- every file a plugin hands back must be a regular file inside the job's folder (so no plugin can make the
  helper upload a file from elsewhere on the machine), no bigger than max_bytes;
- after the last step, the file is checked again: size against max_bytes, length against max_seconds when it is
  known, and the type against the types the site takes;
- the info sent to the site keeps only the fields of docs/api.md, plus `source` and `steps`.

The worker deletes the job's folder afterwards, whatever the outcome.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import FetcherConfig
from .fetch import fetch, pick_info, public_addresses_only
from .plugins import REASONS, Fetched, FetchFailed, Job, NotFound, make_plugin

log = logging.getLogger(__name__)

# The file types the site takes (docs/api.md).
SITE_EXTS = ("webm", "m4a", "mp4", "opus", "ogg", "mp3", "aac", "flac", "wav")


def plugin_log(kind: str, name: str) -> logging.Logger:
    return logging.getLogger(f"tastr_sorceress.{kind}.{name}")


@dataclass
class Pipeline:
    sources: list[tuple[str, Any]]
    steps: list[tuple[str, Any]] = field(default_factory=list)
    local_hosts: frozenset[tuple[str, str | None]] = frozenset()

    def describe(self) -> str:
        return f"sources {', '.join(n for n, _ in self.sources)}; steps {', '.join(n for n, _ in self.steps) or 'none'}"

    def run(self, job: Job, workdir: str) -> Fetched:
        """Fetch one job's audio into `workdir` and return the checked file, ready to upload."""
        limits = dict(job.limits)  # the checks use this copy, so a plugin that changes job.limits changes nothing
        fetched, source = self._fetch(job, workdir, limits)
        ran: list[str] = []
        for name, step in self.steps:
            log.debug("Job %s: running step %s", job.id, name)
            fetched = self._call("step", name, step.run, fetched, job, plugin_log("step", name))
            fetched = check_file(fetched, workdir, limits, f"the step {name!r}")
            ran.append(name)
        fetched = check_output(fetched, limits)
        info = pick_info(fetched.info)
        info["source"] = source
        info["steps"] = ran
        return Fetched(path=fetched.path, ext=fetched.ext, info=info, size=fetched.size)

    def _fetch(self, job: Job, workdir: str, limits: dict[str, int]) -> tuple[Fetched, str]:
        tried = []
        for n, (name, source) in enumerate(self.sources, 1):
            folder = os.path.join(workdir, f"source-{n}")
            os.mkdir(folder, 0o700)
            log.debug("Job %s: trying source %s", job.id, name)
            try:
                fetched = self._call("source", name, source.fetch, job, folder, plugin_log("source", name))
            except NotFound as e:
                log.info("Job %s: %s did not find it%s", job.id, name, f" ({e})" if str(e) else "")
                tried.append(name)
                continue
            return check_file(fetched, folder, limits, f"the source {name!r}"), name
        raise FetchFailed("unavailable", f"no source found this track (tried {', '.join(tried)})")

    def _call(self, kind: str, name: str, method: Callable[..., Any], *args: Any) -> Any:
        """Run one plugin call under the private-address guard, and turn anything unexpected into a failure."""
        try:
            with public_addresses_only(self.local_hosts):
                result = method(*args)
        except NotFound:
            if kind == "source":
                raise
            raise FetchFailed("error", f"the step {name!r} raised NotFound, which only sources may") from None
        except FetchFailed as e:
            if e.reason not in REASONS:
                raise FetchFailed("error", f"{name}: {e.reason}: {e.detail}") from None
            raise
        except Exception as e:
            log.exception("The %s %r failed", kind, name)
            raise FetchFailed("error", f"{kind} {name}: {type(e).__name__}: {e}") from None
        if not isinstance(result, Fetched):
            raise FetchFailed("error", f"the {kind} {name!r} returned {type(result).__name__}, not a Fetched")
        return result


def check_file(fetched: Fetched, folder: str, limits: dict[str, int], who: str) -> Fetched:
    """The file must be a regular file (not a link) inside `folder`, not empty and within max_bytes."""
    path = fetched.path
    if not isinstance(path, str) or not path:
        raise FetchFailed("error", f"{who} returned no file")
    real = os.path.realpath(path)
    root = os.path.realpath(folder)
    if os.path.commonpath([real, root]) != root or real == root:
        raise FetchFailed("error", f"{who} returned a file outside the job's folder")
    try:
        st = os.lstat(path)
    except OSError:
        raise FetchFailed("error", f"{who} returned a file that does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise FetchFailed("error", f"{who} returned something that is not a plain file")
    if st.st_size == 0:
        raise FetchFailed("error", f"{who} returned an empty file")
    if st.st_size > limits["max_bytes"]:
        raise FetchFailed("too_big", f"{st.st_size} bytes is more than the limit of {limits['max_bytes']} bytes")
    ext = fetched.ext.lower() if isinstance(fetched.ext, str) else ""
    info = fetched.info if isinstance(fetched.info, dict) else {}
    return Fetched(path=path, ext=ext, info=info, size=st.st_size)


def check_output(fetched: Fetched, limits: dict[str, int]) -> Fetched:
    """The last checks before upload: the type is one the site takes, and the length is within the limit."""
    if fetched.ext not in SITE_EXTS:
        raise FetchFailed("error", f"the file type {fetched.ext or '(none)'!r} is not one the site takes "
                                   f"({', '.join(SITE_EXTS)})")
    duration = fetched.info.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > limits["max_seconds"]:
        raise FetchFailed("too_long", f"{duration:.0f} s is longer than the limit of {limits['max_seconds']} s")
    return fetched


def build_pipeline(config: FetcherConfig | None = None, *, cache_dir: str | None = None,
                   fetcher: Callable[..., Fetched] = fetch) -> Pipeline:
    """Start every configured source and step. Any problem is a ConfigError, raised at start."""
    from .builtin import YtDlpSource

    config = config or FetcherConfig()
    sources = []
    with public_addresses_only(config.local_hosts):
        for name in config.sources:
            if name == "yt-dlp":
                plugin = YtDlpSource(config.options_for(name), fetcher=fetcher, cache_dir=cache_dir)
            else:
                plugin = make_plugin("source", name, config.options_for(name))
            sources.append((name, plugin))
        steps = [(name, make_plugin("step", name, config.options_for(name))) for name in config.steps]
    return Pipeline(sources=sources, steps=steps, local_hosts=config.local_hosts)
