"""Uploading a fetched file to the site in parts, streamed from disk."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Callable
from urllib.parse import quote

from .api import Client, NetworkError
from .backoff import Backoff
from .fetch import FetchFailed

log = logging.getLogger(__name__)

MIN_PART = 5 << 20  # every part but the last must be at least 5 MiB


class LeaseLost(Exception):
    """409: the job is no longer ours (lease ran out, or another helper has it)."""


class UploadError(Exception):
    pass


def plan_parts(size: int, part_bytes: int) -> list[tuple[int, int]]:
    """(offset, length) for each part: full parts of part_bytes, then the rest."""
    if size <= 0:
        raise UploadError("nothing to upload")
    if part_bytes <= 0:
        raise UploadError("the site sent no usable part size")
    if size > part_bytes and part_bytes < MIN_PART:
        raise UploadError(f"the site's part size ({part_bytes} bytes) is below the 5 MiB minimum")
    return [(off, min(part_bytes, size - off)) for off in range(0, size, part_bytes)]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def job_path(job_id: str, *rest: str) -> str:
    return "/jobs/" + "/".join([quote(job_id, safe="")] + list(rest))


def upload_parts(
    client: Client,
    job_id: str,
    path: str,
    size: int,
    part_bytes: int,
    *,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Upload the file in parts; returns the number of parts."""
    parts = plan_parts(size, part_bytes)
    for n, (offset, length) in enumerate(parts, start=1):
        backoff = Backoff(base=2.0, cap=60.0)
        for attempt in range(1, attempts + 1):
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    resp = client.call(
                        "PUT", job_path(job_id, "parts", str(n)), stream=f, length=length, timeout=300
                    )
            except NetworkError as e:
                problem, wait = str(e), 0.0
            else:
                if resp.status == 200:
                    log.info("Uploaded part %d/%d (%d bytes)", n, len(parts), length)
                    break
                if resp.status == 409:
                    raise LeaseLost(resp.describe())
                if resp.status == 413:
                    raise FetchFailed("too_big", "the site refused the upload as too big")
                if resp.status != 429 and resp.status < 500:
                    raise UploadError(f"part {n} was refused ({resp.describe()})")
                problem, wait = resp.describe(), resp.retry_after or 0.0
            if attempt == attempts:
                raise UploadError(f"part {n} failed {attempts} times; last: {problem}")
            delay = max(backoff.next_delay(), wait)
            log.warning("Part %d failed (%s); retrying in %.0f s", n, problem, delay)
            sleep(delay)
    return len(parts)
