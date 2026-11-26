"""A job's log on the site: what this helper logs while it works on a job is also sent to the site, so the person
can follow each step on their Fetchers page (POST /jobs/<id>/log, docs/api.md).

Only the lines of the thread doing the job, at the level the helper logs at (debug with SORCERESS_LOG_LEVEL=debug
or --verbose). Sending is best effort and never in the way of the job: lines wait in a bounded queue and a
background thread sends them, so a slow or failing site never stalls a download or an upload. The thread backs off
when the site asks it to (429, Retry-After) or fails, stops for the job on 401, 404 or 409, and gets a few seconds
at the end of the job to send what is left; anything it can't send is dropped. The key is never logged, so it is
never sent.
"""

from __future__ import annotations

import collections
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
QUEUE = 1000  # lines waiting at most; older ones are dropped beyond it
FINISH_S = 10.0  # how long the end of a job waits for the last lines to go
ERROR_WAIT_S = 30.0  # after a failed send (no Retry-After), before the next one
MAX_WAIT_S = 300.0
TEXT = 500


class SiteLog(logging.Handler):
    """Collects a job's log lines and sends them to the site from a background thread. Use as a context manager
    around a job."""

    def __init__(self, client: Client, job_id: str, *, send_every_s: float = SEND_EVERY_S,
                 finish_s: float = FINISH_S, clock=time.monotonic):
        super().__init__(level=logging.NOTSET)
        self.client = client
        self.job_id = job_id
        self.send_every_s = send_every_s
        self.finish_s = finish_s
        self.clock = clock
        self.thread_id = threading.get_ident()
        self.queue: collections.deque[dict[str, Any]] = collections.deque(maxlen=QUEUE)
        self._qlock = threading.Lock()
        self.wake = threading.Event()
        self.done = threading.Event()
        self.stopped = False  # the site won't take more for this job
        self.not_before = 0.0  # backing off until then (clock)
        self.sender: threading.Thread | None = None
        self.sent = 0
        self.dropped = 0

    def __enter__(self) -> "SiteLog":
        root = logging.getLogger()
        self.setLevel(root.getEffectiveLevel())
        self.sender = threading.Thread(target=self._run, name=f"joblog-{self.job_id[:8]}", daemon=True)
        self.sender.start()
        root.addHandler(self)
        return self

    def __exit__(self, *exc) -> None:
        logging.getLogger().removeHandler(self)
        self.done.set()
        self.wake.set()
        if self.sender is not None:
            self.sender.join(self.finish_s)  # a daemon thread: one still stuck is left behind, not waited for
        with self._qlock:
            self.dropped += len(self.queue)
            self.queue.clear()
            self.stopped = True

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id or self.stopped:
            return  # only the job's own thread: not the heartbeat, nor this handler's sender
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            line = {"at": round(record.created, 3), "level": LEVELS.get(record.levelno, "info"),
                    "step": getattr(record, "step", None) or _step_of(record.name), "text": text[:TEXT]}
        except Exception:  # pragma: no cover - a broken record never stops a job
            return
        with self._qlock:
            if len(self.queue) == QUEUE:
                self.dropped += 1
            self.queue.append(line)
            full = len(self.queue) >= PER_CALL
        if full:
            self.wake.set()

    # --- the sender thread ---------------------------------------------------

    def _run(self) -> None:
        while True:
            self.wake.wait(self.send_every_s)
            self.wake.clear()
            finishing = self.done.is_set()
            wait = self.not_before - self.clock()
            if wait > 0:
                if finishing:  # the site asked for a pause longer than the end of the job: what's left is dropped
                    return
                self.wake.wait(min(wait, MAX_WAIT_S))
                continue
            while not self.stopped:
                with self._qlock:
                    batch = [self.queue.popleft() for _ in range(min(PER_CALL, len(self.queue)))]
                if not batch:
                    break
                if not self._send(batch):
                    break
            if finishing or self.stopped:
                return

    def _send(self, batch: list[dict[str, Any]]) -> bool:
        """Send one batch. False: stop sending for now (backing off) or for good (self.stopped)."""
        try:
            resp = self.client.call("POST", job_path(self.job_id, "log"), {"lines": batch}, timeout=15)
        except Unauthorized:  # the key was revoked: the job's own calls find out too
            return self._stop(len(batch))
        except NetworkError:
            self.dropped += len(batch)
            self.not_before = self.clock() + ERROR_WAIT_S
            return False
        except Exception:  # pragma: no cover - never let the sender die noisily
            return self._stop(len(batch))
        if resp.status == 200:
            self.sent += len(batch)
            return True
        if resp.status in (404, 409):  # an older site without the route, or the job is no longer ours
            return self._stop(len(batch))
        self.dropped += len(batch)
        self.not_before = self.clock() + min(resp.retry_after or ERROR_WAIT_S, MAX_WAIT_S)
        return False

    def _stop(self, lost: int) -> bool:
        with self._qlock:
            self.dropped += lost + len(self.queue)
            self.queue.clear()
            self.stopped = True
        return False


def _step_of(logger_name: str) -> str | None:
    """A short step name from the module that logged the line."""
    tail = logger_name.rsplit(".", 1)[-1]
    return {"fetch": "fetch", "search": "search", "upload": "upload", "pipeline": "pipeline", "worker": "job",
            "builtin": "step"}.get(tail, tail[:40] if logger_name.startswith("tastr_sorceress") else None)
