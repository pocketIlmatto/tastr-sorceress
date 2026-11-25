"""Device-code pairing: show a code, the person confirms it on the site, we get a key."""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import __version__
from .api import Client, NetworkError
from .backoff import Backoff
from .config import Settings
from .keystore import KEY_PREFIX

log = logging.getLogger(__name__)

DEFAULT_CONFIRM_PATH = "/bank/sorceress"
NOT_ON_WAIT = 300.0  # the site answers 404 when the feature is switched off
RATE_LIMIT_WAIT = 600.0


def confirm_url(settings: Settings, confirm_path: object) -> str:
    path = confirm_path if isinstance(confirm_path, str) else ""
    # Only a path on the same site; never a link somewhere else.
    if not path.startswith("/") or path.startswith("//") or not path.isprintable():
        path = DEFAULT_CONFIRM_PATH
    return settings.url + path


def show_code(code: str, url: str, expires_s: float, out: Callable[[str], None]) -> None:
    minutes = max(1, round(expires_s / 60))
    bar = "=" * 64
    out(
        "\n".join(
            [
                bar,
                "  Pair this helper with your Tastr account:",
                f"    1. Open   {url}",
                f"    2. Enter  {code}",
                f"  The code works for about {minutes} minutes; a new one follows if it runs out.",
                bar,
            ]
        )
    )


def pair(
    client: Client,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = lambda s: print(s, flush=True),
    poll_interval: float = 3.0,
) -> str:
    """Pair with the site and return the new key. Loops until it succeeds."""
    backoff = Backoff()
    while True:
        try:
            resp = client.call(
                "POST", "/pair/start", {"name": settings.name, "version": __version__}, auth=False
            )
        except NetworkError as e:
            delay = backoff.next_delay()
            log.warning("Can't reach %s (%s); trying again in %.0f s", settings.url, e, delay)
            sleep(delay)
            continue
        if resp.status == 404:
            log.warning("The site answered 404: helpers are not switched on there yet. Trying again in %.0f s", NOT_ON_WAIT)
            sleep(NOT_ON_WAIT)
            continue
        if resp.status == 429:
            delay = resp.retry_after or RATE_LIMIT_WAIT
            log.warning("Too many pairing attempts from this address; waiting %.0f s", delay)
            sleep(delay)
            continue
        data = resp.data
        if resp.status != 200 or not all(isinstance(data.get(k), str) for k in ("pairing_id", "poll_secret", "code")):
            delay = backoff.next_delay()
            log.warning("Pairing could not start (%s); trying again in %.0f s", resp.describe(), delay)
            sleep(delay)
            continue
        backoff.reset()

        try:
            expires_s = float(data.get("expires_s") or 600)
        except (TypeError, ValueError):
            expires_s = 600.0
        url = confirm_url(settings, data.get("confirm_path"))
        show_code(data["code"], url, expires_s, out)

        key = _poll(client, data, expires_s, sleep=sleep, clock=clock, poll_interval=poll_interval)
        if key:
            out("Paired. This helper now fetches links for your account.")
            return key
        out("The pairing code ran out before it was confirmed; getting a new one.")
        sleep(poll_interval)


def _poll(client, data, expires_s, *, sleep, clock, poll_interval) -> str | None:
    body = {"pairing_id": data["pairing_id"], "poll_secret": data["poll_secret"]}
    deadline = clock() + expires_s + 30
    backoff = Backoff(base=poll_interval, cap=60.0)
    while clock() < deadline:
        try:
            resp = client.call("POST", "/pair/poll", body, auth=False)
        except NetworkError as e:
            delay = backoff.next_delay()
            log.warning("Can't reach the site while waiting for confirmation (%s)", e)
            sleep(delay)
            continue
        if resp.status == 200 and resp.data.get("state") == "paired":
            key = resp.data.get("key")
            if isinstance(key, str) and key.startswith(KEY_PREFIX) and key.isascii() and key.isprintable():
                log.info("Paired as helper %s", resp.data.get("helper_id", "?"))
                return key
            log.error("The site said paired but sent no usable key; starting over")
            return None
        if resp.status in (404, 410):
            return None
        if resp.status == 202:
            backoff.reset()
            sleep(poll_interval)
        elif resp.status == 429:
            sleep(max(poll_interval, resp.retry_after or 30.0))
        else:
            delay = backoff.next_delay()
            log.warning("Unexpected answer while waiting for confirmation (%s)", resp.describe())
            sleep(delay)
    return None
