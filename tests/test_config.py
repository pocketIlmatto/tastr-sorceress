"""The config file: parsing, defaults and the errors a person sees."""

import pytest

from tastr_sorceress.cli import main
from tastr_sorceress.config import (
    ConfigError,
    FetcherConfig,
    Settings,
    load_fetcher_config,
    parse_fetcher_config,
    parse_local_host,
)


def settings_for(tmp_path, **env):
    return Settings.from_env({"TASTR_URL": "https://tastr.example.com", "SORCERESS_DATA": str(tmp_path), **env})


def test_no_file_means_todays_behaviour(tmp_path):
    config = load_fetcher_config(settings_for(tmp_path))
    assert config == FetcherConfig()
    assert config.sources == ("yt-dlp",) and config.steps == () and config.log_level == "info"


def test_default_path_is_in_the_data_dir(tmp_path):
    (tmp_path / "config.toml").write_text('log_level = "debug"\n')
    assert load_fetcher_config(settings_for(tmp_path)).log_level == "debug"


def test_full_example(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text(
        """
log_level = "warning"
sources = ["my-source", "yt-dlp"]
steps = ["convert", "my-step", "copy"]

[copy]
folder = "/copies"

[convert]
format = "mp3"
bitrate = "256k"

[network]
local_hosts = ["lidarr.lan", "192.168.1.20:8686"]

[plugins.my-source]
url = "http://lidarr.lan:8686"
api_key_file = "/run/secrets/lidarr"

[plugins.my-step]
""")
    config = load_fetcher_config(settings_for(tmp_path, SORCERESS_CONFIG=str(path)))
    assert config.log_level == "warning"
    assert config.sources == ("my-source", "yt-dlp")
    assert config.steps == ("convert", "my-step", "copy")
    assert config.copy_folder == "/copies"
    assert (config.convert_format, config.convert_bitrate) == ("mp3", "256k")
    assert config.local_hosts == {("lidarr.lan", None), ("192.168.1.20", "8686")}
    assert config.options_for("my-source") == {"url": "http://lidarr.lan:8686", "api_key_file": "/run/secrets/lidarr"}
    assert config.options_for("my-step") == {}
    assert config.options_for("copy") == {"folder": "/copies"}
    assert config.options_for("convert") == {"format": "mp3", "bitrate": "256k"}


def test_env_log_level_wins(tmp_path):
    (tmp_path / "config.toml").write_text('log_level = "error"\n')
    assert load_fetcher_config(settings_for(tmp_path, SORCERESS_LOG_LEVEL="DEBUG")).log_level == "debug"
    with pytest.raises(ConfigError, match="SORCERESS_LOG_LEVEL"):
        settings_for(tmp_path, SORCERESS_LOG_LEVEL="loud")


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_fetcher_config(settings_for(tmp_path, SORCERESS_CONFIG=str(tmp_path / "nope.toml")))


def test_bad_toml_names_the_file(tmp_path):
    (tmp_path / "config.toml").write_text("sources = [yt-dlp\n")
    with pytest.raises(ConfigError, match="config.toml is not valid TOML"):
        load_fetcher_config(settings_for(tmp_path))


@pytest.mark.parametrize(
    "raw,message",
    [
        ({"source": ["yt-dlp"]}, "unknown setting 'source'"),
        ({"log_level": "loud"}, "log_level must be one of"),
        ({"sources": []}, "at least one source"),
        ({"sources": "yt-dlp"}, "must be a list"),
        ({"sources": ["yt-dlp", "yt-dlp"]}, "twice"),
        ({"sources": ["Bad Name"]}, "not a valid name"),
        ({"steps": [1]}, "must be a list"),
        ({"steps": ["copy"]}, "needs \\[copy\\] folder"),
        ({"steps": ["copy"], "copy": {"folder": "relative/dir"}}, "absolute path"),
        ({"copy": {"folder": "/copies"}}, "not in steps"),
        ({"steps": ["copy"], "copy": {"folder": "/c", "mode": 1}}, "unknown setting 'mode' in \\[copy\\]"),
        ({"steps": ["convert"]}, "needs \\[convert\\] format"),
        ({"steps": ["convert"], "convert": {"format": "wma"}}, "needs \\[convert\\] format"),
        ({"steps": ["convert"], "convert": {"format": "flac", "bitrate": "320k"}}, "lossless"),
        ({"steps": ["convert"], "convert": {"format": "mp3", "bitrate": "loud"}}, "192k"),
        ({"convert": {"format": "mp3"}}, "not in steps"),
        ({"network": {"local_hosts": "lidarr.lan"}}, "must be a list"),
        ({"network": {"local_hosts": ["*.lan"]}}, "not a host name"),
        ({"network": {"hosts": []}}, "unknown setting 'hosts' in \\[network\\]"),
        ({"plugins": {"other": {}}}, "not in sources or steps"),
        ({"plugins": {"copy": {}}, "steps": []}, "use \\[copy\\]"),
        ({"plugins": {"yt-dlp": {"x": 1}}}, "not for a built-in"),
        ({"sources": ["s"], "plugins": {"s": 3}}, "must be a table"),
        ({"network": 3}, "must be a table"),
    ],
)
def test_config_errors(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_fetcher_config(raw)


@pytest.mark.parametrize(
    "entry,expect",
    [
        ("lidarr.lan", ("lidarr.lan", None)),
        ("Lidarr.LAN.", ("lidarr.lan", None)),
        ("192.168.1.20:8686", ("192.168.1.20", "8686")),
        ("[fd00::1]:5030", ("fd00::1", "5030")),
        ("[fd00::1]", ("fd00::1", None)),
        ("slskd", ("slskd", None)),
    ],
)
def test_local_host_entries(entry, expect):
    assert parse_local_host(entry) == expect


@pytest.mark.parametrize("entry", ["", "http://lidarr.lan", "lidarr.lan/x", "user@lidarr.lan", "lidarr.lan:0",
                                   "lidarr.lan:99999", "fd00::1", "-bad.lan", "a b", "*.lan"])
def test_local_host_entries_refused(entry):
    with pytest.raises(ConfigError):
        parse_local_host(entry)


def test_cli_refuses_a_bad_config_at_start(monkeypatch, tmp_path, capsys):
    (tmp_path / "config.toml").write_text('sources = ["nobody-has-this"]\n')
    monkeypatch.setenv("TASTR_URL", "https://tastr.example.com")
    monkeypatch.setenv("SORCERESS_DATA", str(tmp_path))
    monkeypatch.delenv("SORCERESS_CONFIG", raising=False)
    assert main(["status"]) == 2
    assert "unknown source 'nobody-has-this'" in capsys.readouterr().err
    assert main(["unpair"]) == 0  # a broken config never stops unpairing


def test_cli_status_shows_the_pipeline(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TASTR_URL", "https://tastr.example.com")
    monkeypatch.setenv("SORCERESS_DATA", str(tmp_path))
    monkeypatch.delenv("SORCERESS_CONFIG", raising=False)
    assert main(["status"]) == 1  # not paired
    assert "fetch: sources yt-dlp; steps none" in capsys.readouterr().out


def test_readme_example_config_is_valid():
    import pathlib
    import re
    import tomllib

    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text()
    block = re.search(r"```toml\n(.*?)```", readme, re.S).group(1)
    config = parse_fetcher_config(tomllib.loads(block))
    assert config.steps == ("convert", "copy") and config.sources == ("yt-dlp",)
