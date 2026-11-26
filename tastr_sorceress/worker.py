"""The main loop: pair, then take one job at a time, fetch, upload, report."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable

from . import __version__
from .api import Client, NetworkError, Response, Unauthorized
from .backoff import Backoff
from .config import Settings
from .fetch import FetchFailed, clean_limits, fetch, url_allowed
from .keystore import KeyStore
from .pairing import pair
from .pipeline import Pipeline, build_pipeline
from .plugins import Job, clean_hints
from .upload import LeaseLost, UploadError, job_path, sha256_file, upload_parts

log = logging.getLogger(__name__)

JOB_DIR_PREFIX = "sorceress-job-"
HEARTBEAT_S = 300.0
LONG_POLL_S = 25
MIN_POLL_GAP = 5.0  # if the site answers "no job" at once, wait this long before asking again
NOT_ON_WAIT = 300.0
MAX_REPORT_WAIT = 600.0  # the longest Retry-After a done/failed report honours between tries


def yt_dlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as v

        return v
    except Exception:  # pragma: no cover - yt-dlp is a dependency
        return "unknown"


def _version_tuple(v: object) -> tuple[int, ...]:
    out = []
    for piece in str(v).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Client | None = None,
        keystore: KeyStore | None = None,
        pipeline: Pipeline | None = None,
        fetcher: Callable[..., Any] = fetch,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = lambda s: print(s, flush=True),
        poll_interval: float = 3.0,
        heartbeat_s: float = HEARTBEAT_S,
        long_poll_s: int = LONG_POLL_S,
        min_poll_gap: float = MIN_POLL_GAP,
        part_attempts: int = 4,
    ):
        self.settings = settings
        self.client = client or Client(settings.url)
        self.keystore = keystore or KeyStore(settings.data_dir)
        self.sleep = sleep
        self.clock = clock
        self.out = out
        self.poll_interval = poll_interval
        self.heartbeat_s = heartbeat_s
        self.long_poll_s = long_poll_s
        self.min_poll_gap = min_poll_gap
        self.part_attempts = part_attempts
        self.backoff = Backoff()
        self.lost_key = threading.Event()
        self.cache_dir = self.cache_dir_for(settings)
        # With no pipeline given, today's behaviour: yt-dlp alone (`fetcher` stands in for it in tests).
        self.pipeline = pipeline or build_pipeline(cache_dir=self.cache_dir, fetcher=fetcher)

    @staticmethod
    def cache_dir_for(settings: Settings) -> str:
        return os.path.join(settings.tmp_dir, "sorceress-cache")

    # --- key -------------------------------------------------------------

    def ensure_key(self) -> None:
        if self.lost_key.is_set():
            self.forget_key()
        if self.client.key:
            return
        key = self.keystore.load()
        if not key:
            key = pair(self.client, self.settings, sleep=self.sleep, clock=self.clock, out=self.out,
                       poll_interval=self.poll_interval)
            self.keystore.save(key)
        self.client.key = key
        self.heartbeat()  # says hello, and finds out at once if the key was revoked

    def forget_key(self) -> None:
        log.warning("The site no longer knows this helper (it was removed or unpaired). Pairing again.")
        self.keystore.delete()
        self.client.key = None
        self.lost_key.clear()

    # --- heartbeat -------------------------------------------------------

    def heartbeat(self) -> Response | None:
        try:
            resp = self.client.call("POST", "/heartbeat", {"version": __version__, "yt_dlp": yt_dlp_version()})
        except NetworkError as e:
            log.info("Heartbeat could not reach the site (%s)", e)
            return None
        if resp.status != 200:
            log.info("Heartbeat answered %s", resp.describe())
            return resp
        min_v, latest = resp.data.get("min_version"), resp.data.get("latest_version")
        if min_v and _version_tuple(__version__) < _version_tuple(min_v):
            log.warning("This helper (%s) is older than the site needs (%s): pull the new image.", __version__, min_v)
        elif latest and _version_tuple(__version__) < _version_tuple(latest):
            log.info("A newer helper (%s) is out; pull the new image when convenient.", latest)
        return resp

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if self.client.key and not self.lost_key.is_set():
                try:
                    self.heartbeat()
                except Unauthorized:
                    self.lost_key.set()
                except Exception:  # keep the thread alive whatever happens
                    log.exception("Heartbeat failed")
            stop.wait(self.heartbeat_s)

    # --- jobs ------------------------------------------------------------

    def next_job(self) -> dict[str, Any] | None:
        started = self.clock()
        try:
            resp = self.client.call("GET", f"/jobs/next?wait={self.long_poll_s}", timeout=self.long_poll_s + 30)
        except NetworkError as e:
            delay = self.backoff.next_delay()
            log.warning("Can't reach %s (%s); trying again in %.0f s", self.settings.url, e, delay)
            self.sleep(delay)
            return None
        if resp.status == 200 and isinstance(resp.data.get("job_id"), str) and resp.data["job_id"]:
            self.backoff.reset()
            return resp.data
        if resp.status == 204:
            self.backoff.reset()
            if self.clock() - started < 1.0:
                self.sleep(self.min_poll_gap)
            return None
        if resp.status == 404:
            log.warning("The site answered 404: helpers are switched off there. Asking again in %.0f s", NOT_ON_WAIT)
            self.sleep(NOT_ON_WAIT)
            return None
        if resp.status == 403:
            # An admin switched fetching off for this account. The key stays; it works again once switched back on.
            log.warning("The site answered 403: fetching is switched off for this account. Asking again in %.0f s",
                        NOT_ON_WAIT)
            self.sleep(NOT_ON_WAIT)
            return None
        if resp.status == 429:
            self.sleep(resp.retry_after or 60.0)
            return None
        delay = self.backoff.next_delay()
        log.warning("Asking for a job failed (%s); trying again in %.0f s", resp.describe(), delay)
        self.sleep(delay)
        return None

    def _post(self, path: str, body: dict[str, Any], attempts: int = 4) -> Response | None:
        backoff = Backoff(base=2.0, cap=60.0)
        for attempt in range(1, attempts + 1):
            wait = 0.0
            try:
                resp = self.client.call("POST", path, body)
            except NetworkError as e:
                problem = str(e)
            else:
                if resp.status < 500 and resp.status != 429:
                    return resp
                problem = resp.describe()
                wait = min(resp.retry_after or 0.0, MAX_REPORT_WAIT)
            if attempt < attempts:
                delay = max(backoff.next_delay(), wait)
                log.warning("Reporting to the site failed (%s); retrying in %.0f s", problem, delay)
                self.sleep(delay)
        log.error("Could not report to the site after %d tries", attempts)
        return None

    def report_failed(self, job_id: str, reason: str, detail: str) -> None:
        log.info("Job %s failed: %s (%s)", job_id, reason, detail)
        resp = self._post(job_path(job_id, "failed"), {"reason": reason, "detail": detail[:500]})
        if resp is not None and resp.status != 200:
            log.warning("The site did not take the failure report: %s", resp.describe())

    def handle(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        url = job.get("url")
        allow_hosts = job.get("allow_hosts")
        server_hosts = allow_hosts if isinstance(allow_hosts, list) else None
        # The allowlist comes before anything else: nothing is fetched or written for a refused link.
        if not url_allowed(url, server_hosts):
            log.warning("Refusing job %s: the link is not on the allowlist", job_id)
            self.report_failed(job_id, "not_allowed", "the link is not an https link to an allowed site")
            return
        limits = clean_limits(job.get("limits"))
        # What plugins see of the job. Never the key.
        plugin_job = Job(id=job_id, url=url, limits=dict(limits), hints=clean_hints(job.get("hints")),
                         allow_hosts=tuple(str(h) for h in server_hosts) if server_hosts is not None else None)
        log.info("Job %s: fetching %s", job_id, url)
        os.makedirs(self.settings.tmp_dir, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix=JOB_DIR_PREFIX, dir=self.settings.tmp_dir)
        try:
            fetched = self.pipeline.run(plugin_job, workdir)
            sha = sha256_file(fetched.path)
            parts = upload_parts(self.client, job_id, fetched.path, fetched.size, limits["part_bytes"],
                                 attempts=self.part_attempts, sleep=self.sleep)
            body = {"parts": parts, "bytes": fetched.size, "sha256": sha, "ext": fetched.ext, "info": fetched.info}
            resp = self._post(job_path(job_id, "done"), body)
            if resp is None:
                log.error("Job %s: could not report it done; the site will hand it out again", job_id)
            elif resp.status == 200:
                log.info("Job %s done: %d bytes in %d parts from %s (deposit %s)", job_id, fetched.size, parts,
                         fetched.info.get("source"), resp.data.get("deposit_id", "?"))
            elif resp.status == 409:
                log.warning("Job %s: the lease was lost before it was done (%s)", job_id, resp.describe())
            else:
                self.report_failed(job_id, "error", f"the site refused the finished upload ({resp.describe()})")
        except FetchFailed as e:
            self.report_failed(job_id, e.reason, e.detail)
        except LeaseLost as e:
            log.warning("Job %s: no longer ours (%s); dropping it", job_id, e)
        except UploadError as e:
            self.report_failed(job_id, "error", str(e))
        except Unauthorized:
            raise
        except Exception as e:
            log.exception("Job %s: unexpected error", job_id)
            self.report_failed(job_id, "error", f"{type(e).__name__}: {e}"[:500])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- loop ------------------------------------------------------------

    def step(self) -> None:
        """One turn of the loop: make sure we're paired, ask for a job, do it."""
        try:
            self.ensure_key()
            job = self.next_job()
            if self.lost_key.is_set():
                raise Unauthorized("heartbeat was refused")
            if job:
                self.handle(job)
        except Unauthorized:
            self.forget_key()

    def clean_tmp(self) -> None:
        try:
            names = os.listdir(self.settings.tmp_dir)
        except OSError:
            return
        for name in names:
            if name.startswith(JOB_DIR_PREFIX):
                shutil.rmtree(os.path.join(self.settings.tmp_dir, name), ignore_errors=True)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.clean_tmp()
        log.info("tastr-sorceress %s (yt-dlp %s) for %s as %r", __version__, yt_dlp_version(),
                 self.settings.url, self.settings.name)
        log.info("Fetching with %s", self.pipeline.describe())
        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop, args=(hb_stop,), name="heartbeat", daemon=True)
        hb.start()
        try:
            while not stop.is_set():
                self.step()
        finally:
            hb_stop.set()
            self.clean_tmp()
