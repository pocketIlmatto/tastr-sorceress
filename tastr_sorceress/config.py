"""Settings, read from the environment, and the optional config file that shapes the fetch pipeline."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A setting is missing or wrong."""


@dataclass(frozen=True)
class Settings:
    url: str  # the Tastr site, e.g. https://tastr.example.com (no trailing slash)
    name: str
    data_dir: str = "/data"
    tmp_dir: str = "/tmp"
    config_path: str = ""  # set by SORCERESS_CONFIG; empty means <data_dir>/config.toml, which may be missing
    log_level: str = ""  # set by SORCERESS_LOG_LEVEL; empty means the config file's, or info

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        log_level = (env.get("SORCERESS_LOG_LEVEL") or "").strip().lower()
        if log_level:
            check_log_level(log_level, "SORCERESS_LOG_LEVEL")
        return cls(
            url=check_site_url(env.get("TASTR_URL", "")),
            name=clean_name(env.get("SORCERESS_NAME") or socket.gethostname()),
            data_dir=env.get("SORCERESS_DATA") or "/data",
            tmp_dir=env.get("SORCERESS_TMP") or "/tmp",
            config_path=env.get("SORCERESS_CONFIG") or "",
            log_level=log_level,
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


# --- the config file ---------------------------------------------------------

LOG_LEVELS = ("debug", "info", "warning", "error")
BUILTIN_SOURCES = ("yt-dlp",)
BUILTIN_STEPS = ("copy", "convert")
CONVERT_FORMATS = ("flac", "mp3", "opus", "m4a")

TOP_KEYS = ("log_level", "sources", "steps", "copy", "convert", "network", "plugins")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
BITRATE_RE = re.compile(r"[1-9][0-9]{1,3}k")
LOCAL_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


@dataclass(frozen=True)
class FetcherConfig:
    """How this helper fetches: the sources to try, the steps to run, and their options. The defaults are what
    the helper does with no config file: yt-dlp alone, no steps."""

    log_level: str = "info"
    sources: tuple[str, ...] = ("yt-dlp",)
    steps: tuple[str, ...] = ()
    copy_folder: str | None = None
    convert_format: str | None = None
    convert_bitrate: str | None = None
    # (host, port) pairs a plugin may reach although they resolve to private addresses; port None means any.
    local_hosts: frozenset[tuple[str, str | None]] = frozenset()
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)

    def options_for(self, name: str) -> dict[str, Any]:
        """The options a source or step is started with."""
        if name == "copy":
            return {"folder": self.copy_folder}
        if name == "convert":
            return {"format": self.convert_format, "bitrate": self.convert_bitrate}
        return dict(self.plugins.get(name, {}))


def check_log_level(value: object, where: str) -> str:
    if not isinstance(value, str) or value.lower() not in LOG_LEVELS:
        raise ConfigError(f"{where} must be one of {', '.join(LOG_LEVELS)}, not {value!r}")
    return value.lower()


def config_file_path(settings: Settings) -> tuple[str, bool]:
    """The config file's path, and whether it was set on purpose (then it must exist)."""
    if settings.config_path:
        return settings.config_path, True
    return os.path.join(settings.data_dir, "config.toml"), False


def load_fetcher_config(settings: Settings) -> FetcherConfig:
    """Read and check the config file. No file at the default path means the defaults."""
    path, required = config_file_path(settings)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        if required:
            raise ConfigError(f"SORCERESS_CONFIG points to {path}, which does not exist") from None
        raw = {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path} can't be read: {e}") from None
    try:
        config = parse_fetcher_config(raw)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None
    if settings.log_level:
        config = replace(config, log_level=settings.log_level)  # the environment wins over the file
    return config


def parse_fetcher_config(raw: dict[str, Any]) -> FetcherConfig:
    """Check every key of a parsed config file. Anything unknown or of the wrong type is an error, so a typo
    can't silently fall back to the defaults."""
    unknown = sorted(set(raw) - set(TOP_KEYS))
    if unknown:
        raise ConfigError(f"unknown setting {unknown[0]!r}; the settings are {', '.join(TOP_KEYS)}")
    log_level = check_log_level(raw.get("log_level", "info"), "log_level")
    sources = _names(raw.get("sources", ["yt-dlp"]), "sources")
    if not sources:
        raise ConfigError("sources must name at least one source, for example [\"yt-dlp\"]")
    steps = _names(raw.get("steps", []), "steps")
    copy = _table(raw, "copy", ("folder",))
    convert = _table(raw, "convert", ("format", "bitrate"))
    network = _table(raw, "network", ("local_hosts",))
    plugins = _plugin_tables(raw.get("plugins", {}), sources, steps)

    copy_folder = None
    if "copy" in steps:
        copy_folder = copy.get("folder")
        if not isinstance(copy_folder, str) or not os.path.isabs(copy_folder):
            raise ConfigError("the copy step needs [copy] folder, an absolute path such as \"/copies\"")
    elif copy:
        raise ConfigError("[copy] is set but \"copy\" is not in steps")

    convert_format = convert_bitrate = None
    if "convert" in steps:
        convert_format = convert.get("format")
        if convert_format not in CONVERT_FORMATS:
            raise ConfigError(f"the convert step needs [convert] format, one of {', '.join(CONVERT_FORMATS)}")
        convert_bitrate = convert.get("bitrate")
        if convert_bitrate is not None:
            if convert_format == "flac":
                raise ConfigError("[convert] bitrate has no meaning for flac, which is lossless; remove it")
            if not isinstance(convert_bitrate, str) or not BITRATE_RE.fullmatch(convert_bitrate):
                raise ConfigError("[convert] bitrate must look like \"192k\"")
    elif convert:
        raise ConfigError("[convert] is set but \"convert\" is not in steps")

    return FetcherConfig(
        log_level=log_level,
        sources=sources,
        steps=steps,
        copy_folder=copy_folder,
        convert_format=convert_format,
        convert_bitrate=convert_bitrate,
        local_hosts=_local_hosts(network.get("local_hosts", [])),
        plugins=plugins,
    )


def _names(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of names, for example [\"yt-dlp\"]")
    for name in value:
        if not NAME_RE.fullmatch(name):
            raise ConfigError(f"{where}: {name!r} is not a valid name (lower-case letters, digits, . _ -)")
    if len(set(value)) != len(value):
        raise ConfigError(f"{where} names something twice")
    return tuple(value)


def _table(raw: dict[str, Any], name: str, keys: tuple[str, ...]) -> dict[str, Any]:
    table = raw.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"{name} must be a table, [{name}]")
    unknown = sorted(set(table) - set(keys))
    if unknown:
        raise ConfigError(f"unknown setting {unknown[0]!r} in [{name}]; it takes {', '.join(keys)}")
    return table


def _plugin_tables(value: object, sources: tuple[str, ...], steps: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ConfigError("plugins must be a table of tables, [plugins.<name>]")
    out: dict[str, dict[str, Any]] = {}
    for name, options in value.items():
        if name in BUILTIN_SOURCES or name in BUILTIN_STEPS:
            hint = f"; use [{name}]" if name in BUILTIN_STEPS else ""
            raise ConfigError(f"[plugins.{name}] is not for a built-in{hint}")
        if name not in sources and name not in steps:
            raise ConfigError(f"[plugins.{name}] is set but {name!r} is not in sources or steps")
        if not isinstance(options, dict):
            raise ConfigError(f"plugins.{name} must be a table, [plugins.{name}]")
        out[name] = options
    return out


def _local_hosts(value: object) -> frozenset[tuple[str, str | None]]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError("[network] local_hosts must be a list of host names, for example [\"lidarr.lan\"]")
    return frozenset(parse_local_host(entry) for entry in value)


def parse_local_host(entry: str) -> tuple[str, str | None]:
    """"lidarr.lan", "192.168.1.20:8686" or "[fd00::1]:8686" -> (host, port or None). No wildcards."""
    bad = ConfigError(f"[network] local_hosts: {entry!r} is not a host name or address with an optional :port")
    text = entry.strip().lower()
    if not text or "/" in text or "@" in text or "*" in text:
        raise bad
    try:
        parts = urlsplit("//" + text)
        port = parts.port
    except ValueError:
        raise bad from None
    host = (parts.hostname or "").rstrip(".")
    if not host or (parts.netloc.startswith("[") is False and ":" in host):
        raise bad
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not LOCAL_HOST_RE.fullmatch(host):
            raise bad from None
    if port == 0:
        raise bad
    return host, None if port is None else str(port)
