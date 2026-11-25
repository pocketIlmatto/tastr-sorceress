"""Settings, read from the environment."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A setting is missing or wrong."""


@dataclass(frozen=True)
class Settings:
    url: str  # the Tastr site, e.g. https://tastr.example.com (no trailing slash)
    name: str
    data_dir: str = "/data"
    tmp_dir: str = "/tmp"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            url=check_site_url(env.get("TASTR_URL", "")),
            name=clean_name(env.get("SORCERESS_NAME") or socket.gethostname()),
            data_dir=env.get("SORCERESS_DATA") or "/data",
            tmp_dir=env.get("SORCERESS_TMP") or "/tmp",
        )


def check_site_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ConfigError("TASTR_URL is not set (for example https://tastr.example.com)")
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as e:
        raise ConfigError(f"TASTR_URL is not a valid URL: {e}") from None
    if parts.scheme != "https":
        raise ConfigError("TASTR_URL must start with https://")
    if not parts.hostname:
        raise ConfigError("TASTR_URL has no host name")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("TASTR_URL must be just the site's address, without a login, query or fragment")
    return f"https://{parts.netloc}{parts.path}".rstrip("/")


def clean_name(raw: str) -> str:
    name = "".join(ch for ch in raw if ch.isprintable()).strip()
    return name[:64] or "sorceress"
