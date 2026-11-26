"""A job's log on the site: what this helper logs while it works on a job is also sent to the site, so the person
can follow each step on their Fetchers page (POST /jobs/<id>/log, docs/api.md).

Only the lines of the thread doing the job, at the level the helper logs at (debug with SORCERESS_LOG_LEVEL=debug
or --verbose). Sending is best effort: a line that can't be sent is dropped, and nothing here ever stops a job.
The key is never logged, so it is never sent.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .api import Client, NetworkError, Unauthorized
from .upload import job_path

LEVELS = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning", logging.ERROR: "error",
          logging.CRITICAL: "error"}
PER_CALL = 50  # the most lines the site takes in one call
SEND_EVERY_S = 3.0
TEXT = 500


class SiteLog(logging.Handler):
    """Collects a job's log lines and sends them to the site in batches. Use as a context manager around a job."""

    def __init__(self, client: Client, job_id: str, *, clock=time.monotonic, send_every_s: float = SEND_EVERY_S):
        super().__init__(level=logging.NOTSET)
        self.client = client
        self.job_id = job_id
        self.clock = clock
        self.send_every_s = send_every_s
        self.thread = threading.get_ident()
        self.lines: list[dict[str, Any]] = []
        self.last_sent = clock()
        self.sending = False
        self.closed_by_site = False
        self.sent = 0
        self.dropped = 0

    def __enter__(self) -> "SiteLog":
        root = logging.getLogger()
        self.setLevel(root.getEffectiveLevel())
        root.addHandler(self)
        return self

    def __exit__(self, *exc) -> None:
        logging.getLogger().removeHandler(self)
        self.flush()

    def emit(self, record: logging.LogRecord) -> None:
        if self.sending or record.thread != self.thread or self.closed_by_site:
            return  # not this job's thread (the heartbeat), or the lines of sending itself
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            self.lines.append({"at": round(record.created, 3), "level": LEVELS.get(record.levelno, "info"),
                               "step": getattr(record, "step", None) or _step_of(record.name),
                               "text": text[:TEXT]})
        except Exception:  # pragma: no cover - a broken record never stops a job
            return
        if len(self.lines) >= PER_CALL or self.clock() - self.last_sent >= self.send_every_s:
            self.flush()

    def flush(self) -> None:
        while self.lines and not self.closed_by_site:
            batch, self.lines = self.lines[:PER_CALL], self.lines[PER_CALL:]
            self.sending = True
            try:
                resp = self.client.call("POST", job_path(self.job_id, "log"), {"lines": batch}, timeout=15)
            except NetworkError:
                self.dropped += len(batch)
                continue
            except Unauthorized:  # the key was revoked: the job's own calls find out too
                self.closed_by_site = True
                self.dropped += len(batch) + len(self.lines)
                self.lines = []
                return
            finally:
                self.sending = False
                self.last_sent = self.clock()
            if resp.status == 200:
                self.sent += len(batch)
            elif resp.status in (404, 409):  # an older site without the route, or the job is no longer ours
                self.closed_by_site = True
                self.dropped += len(batch) + len(self.lines)
                self.lines = []
            else:
                self.dropped += len(batch)


def _step_of(logger_name: str) -> str | None:
    """A short step name from the module that logged the line."""
    tail = logger_name.rsplit(".", 1)[-1]
    return {"fetch": "fetch", "search": "search", "upload": "upload", "pipeline": "pipeline", "worker": "job",
            "builtin": "step"}.get(tail, tail[:40] if logger_name.startswith("tastr_sorceress") else None)
