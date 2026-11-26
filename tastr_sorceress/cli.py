"""The `tastr-sorceress` command: run (default), status, unpair."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from . import __version__
from .api import Client, NetworkError, Unauthorized
from .config import ConfigError, Settings, load_fetcher_config
from .keystore import KeyStore
from .pipeline import Pipeline, build_pipeline
from .worker import Worker, yt_dlp_version


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def _start_pipeline(settings: Settings, verbose: bool) -> Pipeline:
    """Read the config file, set the log level it asks for, and start its sources and steps. Raises ConfigError."""
    config = load_fetcher_config(settings)
    if not verbose:
        logging.getLogger().setLevel(config.log_level.upper())
    return build_pipeline(config, cache_dir=Worker.cache_dir_for(settings))


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def cmd_run(settings: Settings, pipeline: Pipeline) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        Worker(settings, pipeline=pipeline).run()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopping.")
    return 0


def cmd_status(settings: Settings, pipeline: Pipeline) -> int:
    store = KeyStore(settings.data_dir)
    key = store.load()
    print(f"tastr-sorceress {__version__}, yt-dlp {yt_dlp_version()}")
    print(f"site:  {settings.url}")
    print(f"name:  {settings.name}")
    print(f"fetch: {pipeline.describe()}")
    if not key:
        print("state: not paired (run the helper and confirm the code it shows)")
        return 1
    client = Client(settings.url, key)
    try:
        worker = Worker(settings, client=client, keystore=store, pipeline=pipeline)
        resp = worker.heartbeat()
    except Unauthorized:
        print("state: the site no longer knows this helper's key; it will pair again when it runs")
        return 1
    except NetworkError as e:
        print(f"state: paired, but the site can't be reached ({e})")
        return 1
    if resp is None:
        print("state: paired, but the site can't be reached")
        return 1
    if resp.status != 200:
        print(f"state: paired, but the site answered {resp.describe()}")
        return 1
    print("state: paired, and the site accepts the key")
    return 0


def cmd_unpair(settings: Settings, pipeline: Pipeline | None) -> int:  # noqa: ARG001 - no pipeline needed
    KeyStore(settings.data_dir).delete()
    print("The key on this machine is deleted. Also remove the helper on "
          f"{settings.url}/bank/sorceress so the site forgets it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tastr-sorceress",
        description="Fetches audio from music links for your own Tastr account, from your home connection.",
    )
    parser.add_argument("--version", action="version", version=f"tastr-sorceress {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log more detail")
    parser.add_argument(
        "command", nargs="?", default="run", choices=["run", "status", "unpair"],
        help="run (default): pair if needed, then fetch jobs; status: show the pairing; unpair: delete the key",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        settings = Settings.from_env()
        # unpair works even with a broken config file, so a mistake there can't stop it.
        pipeline = None if args.command == "unpair" else _start_pipeline(settings, args.verbose)
    except ConfigError as e:
        print(f"tastr-sorceress: {e}", file=sys.stderr)
        return 2
    return {"run": cmd_run, "status": cmd_status, "unpair": cmd_unpair}[args.command](settings, pipeline)
