"""The plugin interface: what a source and a step look like, and how they are found.

A fetcher runs a pipeline for each job: it tries its sources in turn until one finds the audio, then runs its steps
in order on the file, then uploads the result. The default pipeline is one source, `yt-dlp`, and no steps.

Writing a plugin
----------------
A plugin is an installed Python package that names a factory in an entry point:

    [project.entry-points."tastr_sorceress.sources"]
    my-source = "my_package:MySource"

    [project.entry-points."tastr_sorceress.steps"]
    my-step = "my_package:MyStep"

The factory (usually a class) is called once at start with the `[plugins.<name>]` table from the config file, as a
plain dict (empty when there is none). It should check its options there and raise `ConfigError` (or any
exception) for bad ones, so a mistake shows up at start rather than on the first job.

A source has a `fetch(job, workdir, log)` method that writes the audio into `workdir` and returns a `Fetched`. It
raises `NotFound` to let the next source try, or `FetchFailed(reason, detail)` to fail the job with one of the
reasons in docs/api.md.

A step has a `run(fetched, job, log)` method that returns a `Fetched`: the same one, or a new one for a new file,
which must be written next to the old one (in the same folder).

Whatever a plugin does, the helper keeps its own rules (see `pipeline.py`): the link must pass the allowlist before
any source runs, the final file must stay within the job's limits and be a type the site takes, the job's folder is
deleted afterwards, and name lookups to private addresses are refused unless the config lists the host. Plugins
never see the helper's key. They do run in the helper's process with its permissions, so install only plugins you
trust.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Callable, Protocol, runtime_checkable

from .config import ConfigError

log = logging.getLogger(__name__)

SOURCE_GROUP = "tastr_sorceress.sources"
STEP_GROUP = "tastr_sorceress.steps"

# The reasons the site understands in a failure report (docs/api.md).
REASONS = ("bot_check", "unavailable", "too_long", "too_big", "not_allowed", "live", "playlist", "error")


class FetchFailed(Exception):
    """The job can't be done; `reason` is one of REASONS and is shown to the person on the site."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail[:500]


class NotFound(Exception):
    """A source didn't find this track. The next source in the list gets a try."""


@dataclass(frozen=True)
class Job:
    """What a plugin is told about a job. Never the helper's key.

    `url` has already passed the allowlist; for a search job (the site had no link) it is the link the helper's
    YouTube search picked. `limits` holds `max_seconds`, `max_bytes` and `part_bytes`. `hints` may hold `title`,
    `artist`, `words` (strings: the person's extra search words, such as "extended mix") and `duration_s` (a
    number), for sources that search by song; any of them can be missing. `allow_hosts` is the site's allowlist
    for this job."""

    id: str
    url: str
    limits: dict[str, int]
    hints: dict[str, Any] = field(default_factory=dict)
    allow_hosts: tuple[str, ...] | None = None


@dataclass
class Fetched:
    """An audio file in the job's folder. `ext` is its type without the dot (`webm`, `m4a`, `flac`, ...). `info`
    holds what is known about the track, with the fields of `info` in docs/api.md (`id`, `extractor`, `title`,
    `duration`, `uploader`, `artist`, `webpage_url`, `upload_date`); unknown fields are dropped before upload.
    `size` is filled in by the helper."""

    path: str
    ext: str
    info: dict[str, Any]
    size: int | None = None


@runtime_checkable
class Source(Protocol):
    name: str

    def fetch(self, job: Job, workdir: str, log: logging.Logger) -> Fetched: ...


@runtime_checkable
class Step(Protocol):
    name: str

    def run(self, fetched: Fetched, job: Job, log: logging.Logger) -> Fetched: ...


def clean_hints(raw: object) -> dict[str, Any]:
    """Keep only the hints the API defines, with the right types."""
    hints: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return hints
    for name in ("title", "artist", "words"):
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            hints[name] = "".join(ch for ch in value if ch.isprintable()).strip()[:1000]
    duration = raw.get("duration_s")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
        hints["duration_s"] = duration
    return hints


# --- finding plugins ---------------------------------------------------------


def _builtin_sources() -> dict[str, Callable[..., Any]]:
    from .builtin import YtDlpSource

    return {"yt-dlp": YtDlpSource}


def _builtin_steps() -> dict[str, Callable[..., Any]]:
    from .builtin import ConvertStep, CopyStep

    return {"copy": CopyStep, "convert": ConvertStep}


def installed(group: str) -> dict[str, list[metadata.EntryPoint]]:
    """Entry points in a group, by name. Nothing is imported here."""
    found: dict[str, list[metadata.EntryPoint]] = {}
    for ep in metadata.entry_points(group=group):
        found.setdefault(ep.name, []).append(ep)
    return found


def find_factory(kind: str, name: str) -> Callable[..., Any]:
    """The factory for a configured source or step name: a built-in, or one installed plugin's entry point."""
    builtins = _builtin_sources() if kind == "source" else _builtin_steps()
    if name in builtins:
        return builtins[name]  # built-in names can't be taken over by a plugin
    group = SOURCE_GROUP if kind == "source" else STEP_GROUP
    eps = installed(group).get(name, [])
    if not eps:
        known = sorted(set(builtins) | set(installed(group)))
        raise ConfigError(f"unknown {kind} {name!r}; known {kind}s: {', '.join(known)}")
    if len(eps) > 1:
        raise ConfigError(f"more than one installed plugin offers the {kind} {name!r}: "
                          + ", ".join(ep.value for ep in eps))
    try:
        return eps[0].load()
    except Exception as e:
        raise ConfigError(f"the {kind} {name!r} ({eps[0].value}) could not be loaded: {e}") from None


def make_plugin(kind: str, name: str, options: dict[str, Any]) -> Any:
    """Load and start one plugin, and check it has the method its kind needs."""
    factory = find_factory(kind, name)
    try:
        plugin = factory(dict(options))
    except ConfigError as e:
        raise ConfigError(f"{kind} {name!r}: {e}") from None
    except Exception as e:
        raise ConfigError(f"the {kind} {name!r} failed to start: {type(e).__name__}: {e}") from None
    method = "fetch" if kind == "source" else "run"
    if not callable(getattr(plugin, method, None)):
        raise ConfigError(f"the {kind} {name!r} has no {method}() method")
    return plugin
