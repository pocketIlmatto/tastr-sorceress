"""A small HTTP client for the Tastr helper API (docs/api.md).

It talks only to the one site it was given, never follows redirects, and never
sends the key anywhere else.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import IO, Any

from . import __version__

API_PREFIX = "/pub/sorceress/v1"
USER_AGENT = f"tastr-sorceress/{__version__}"


class NetworkError(Exception):
    """The site could not be reached, or the connection broke."""


class Unauthorized(Exception):
    """401: the key is unknown or was revoked."""


@dataclass
class Response:
    status: int
    data: dict[str, Any] = field(default_factory=dict)
    retry_after: float | None = None

    @property
    def error(self) -> str:
        return str(self.data.get("error") or "")

    def describe(self) -> str:
        text = f"HTTP {self.status}"
        if self.error:
            text += f" {self.error}"
        detail = self.data.get("detail")
        if detail:
            text += f": {str(detail)[:200]}"
        return text


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # urllib then reports the 3xx as an error response


class _Slice:
    """Reads at most `length` bytes of a file, from its current position, in small blocks."""

    def __init__(self, f: IO[bytes], length: int):
        self._f = f
        self._left = length

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        if n is None or n < 0 or n > self._left:
            n = self._left
        n = min(n, 1 << 20)
        chunk = self._f.read(n)
        self._left -= len(chunk)
        return chunk


class Client:
    def __init__(self, site_url: str, key: str | None = None, timeout: float = 60.0):
        self.base = site_url.rstrip("/") + API_PREFIX
        self.key = key
        self.timeout = timeout
        handlers: list[urllib.request.BaseHandler] = [_NoRedirects()]
        if site_url.startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        timeout: float | None = None,
        stream: IO[bytes] | None = None,
        length: int | None = None,
    ) -> Response:
        """Send one request. Returns the response for any HTTP status except 401 on an
        authenticated call, which raises Unauthorized. Network trouble raises NetworkError."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data: Any = None
        if stream is not None:
            assert length is not None
            data = _Slice(stream, length)
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(length)
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif method in ("POST", "PUT"):
            data = b""
        if auth:
            if not self.key:
                raise Unauthorized("no key")
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                status, raw, hdrs = resp.status, resp.read(1 << 20), resp.headers
        except urllib.error.HTTPError as e:
            status, hdrs = e.code, e.headers
            try:
                raw = e.read(1 << 20)
            except (OSError, ValueError):
                raw = b""
            finally:
                e.close()
        except (urllib.error.URLError, OSError, socket.timeout, ValueError) as e:
            raise NetworkError(str(getattr(e, "reason", e))) from e
        except Exception as e:  # http.client.HTTPException and friends
            raise NetworkError(f"{type(e).__name__}: {e}") from e
        if status == 401 and auth:
            raise Unauthorized("the site does not know this helper's key")
        return Response(status, _parse_json(raw), _parse_retry_after(hdrs))


def _parse_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_retry_after(headers) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 3600.0))
    except ValueError:
        return None
