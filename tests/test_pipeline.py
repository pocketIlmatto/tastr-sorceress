"""The fetch pipeline: plugins, sources in turn, steps in order, and the checks that always hold."""

import importlib.metadata
import logging
import os
import shutil
import socket
import subprocess
import sys
import types

import pytest

from tastr_sorceress import fetch as fetch_mod
from tastr_sorceress.builtin import ConvertStep, CopyStep, YtDlpSource, safe_filename
from tastr_sorceress.config import ConfigError, FetcherConfig, parse_fetcher_config
from tastr_sorceress.fetch import PRIVATE_REFUSED, public_addresses_only
from tastr_sorceress.keystore import KeyStore
from tastr_sorceress.pipeline import Pipeline, build_pipeline
from tastr_sorceress.plugins import SOURCE_GROUP, STEP_GROUP, Fetched, FetchFailed, Job, NotFound, clean_hints

LIMITS = {"max_seconds": 600, "max_bytes": 1000, "part_bytes": 5 << 20}
LOG = logging.getLogger("test")


def job(**kw):
    args = dict(id="job1", url="https://www.youtube.com/watch?v=abc", limits=dict(LIMITS),
                hints={"title": "A song", "artist": "Band"})
    args.update(kw)
    return Job(**args)


def write(folder, name, data=b"x" * 100):
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


class FileSource:
    """A source that writes `data` as audio.<ext>, or raises what it's told to."""

    def __init__(self, name, ext="mp3", data=b"x" * 100, raises=None, info=None):
        self.name, self.ext, self.data, self.raises = name, ext, data, raises
        self.info = info if info is not None else {"id": name, "title": "A song", "duration": 200}
        self.calls = []

    def fetch(self, job, workdir, log):
        self.calls.append((job, workdir))
        if self.raises:
            raise self.raises
        return Fetched(path=write(workdir, f"audio.{self.ext}", self.data), ext=self.ext, info=dict(self.info))


class RecordStep:
    def __init__(self, name, record, change=None):
        self.name, self.record, self.change = name, record, change

    def run(self, fetched, job, log):
        self.record.append(self.name)
        return self.change(fetched) if self.change else fetched


# --- sources and steps -------------------------------------------------------


def test_sources_tried_in_turn(tmp_path):
    first = FileSource("first", raises=NotFound("not in the library"))
    second = FileSource("second")
    third = FileSource("third")
    p = Pipeline(sources=[("first", first), ("second", second), ("third", third)])
    out = p.run(job(), str(tmp_path))
    assert len(first.calls) == 1 and len(second.calls) == 1 and third.calls == []
    assert out.info["source"] == "second" and out.info["steps"] == []
    assert out.size == 100 and out.ext == "mp3"
    assert first.calls[0][1] != second.calls[0][1]  # each source has its own folder in the job's folder
    assert all(os.path.dirname(c[1]) == str(tmp_path) for c in (first.calls[0], second.calls[0]))


def test_all_sources_not_found_is_unavailable(tmp_path):
    p = Pipeline(sources=[("a", FileSource("a", raises=NotFound())), ("b", FileSource("b", raises=NotFound()))])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "unavailable" and "a, b" in e.value.detail


def test_fetch_failed_stops_the_search(tmp_path):
    later = FileSource("later")
    p = Pipeline(sources=[("a", FileSource("a", raises=FetchFailed("bot_check", "prove it"))), ("later", later)])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "bot_check" and later.calls == []


@pytest.mark.parametrize(
    "raises,reason",
    [
        (RuntimeError("boom"), "error"),
        (FetchFailed("made_up", "x"), "error"),
        (FetchFailed("too_long", "x"), "too_long"),
    ],
)
def test_plugin_errors_become_api_reasons(tmp_path, raises, reason):
    p = Pipeline(sources=[("a", FileSource("a", raises=raises))])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == reason


def test_steps_run_in_order(tmp_path):
    record = []

    def to_flac(f):
        new = os.path.join(os.path.dirname(f.path), "audio.flac")
        os.replace(f.path, new)
        return Fetched(path=new, ext="flac", info={**f.info, "artist": "Tagged"})

    p = Pipeline(sources=[("src", FileSource("src", ext="mkv"))],
                 steps=[("one", RecordStep("one", record)), ("two", RecordStep("two", record, to_flac)),
                        ("three", RecordStep("three", record))])
    out = p.run(job(), str(tmp_path))
    assert record == ["one", "two", "three"]
    assert out.ext == "flac"  # an odd type from the source is fine once a step fixes it
    assert out.info["artist"] == "Tagged"
    assert out.info["steps"] == ["one", "two", "three"] and out.info["source"] == "src"


def test_info_keeps_only_api_fields(tmp_path):
    src = FileSource("src", info={"title": "T", "secret": "x" * 10, "duration": 10})
    out = Pipeline(sources=[("src", src)]).run(job(), str(tmp_path))
    assert set(out.info) == {"id", "extractor", "title", "duration", "uploader", "artist", "webpage_url",
                             "upload_date", "source", "steps"}


def test_hints_are_cleaned():
    assert clean_hints({"title": " A\x00 song ", "artist": "", "duration_s": 201.5, "other": 1}) == \
        {"title": "A song", "duration_s": 201.5}
    assert clean_hints({"duration_s": True}) == {}
    assert clean_hints(None) == {}


# --- guardrails --------------------------------------------------------------


def test_oversize_after_a_step_is_refused(tmp_path):
    def grow(f):
        with open(f.path, "ab") as out:
            out.write(b"y" * 2000)
        return f

    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("grow", RecordStep("grow", [], grow))])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "too_big"


def test_oversize_from_a_source_is_refused(tmp_path):
    p = Pipeline(sources=[("src", FileSource("src", data=b"x" * 1001))])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "too_big"


@pytest.mark.parametrize("ext", ["exe", "mkv", "", "MP3X"])
def test_bad_ext_after_a_step_is_refused(tmp_path, ext):
    def rename(f):
        return Fetched(path=f.path, ext=ext, info=f.info)

    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("ren", RecordStep("ren", [], rename))])
    with pytest.raises(FetchFailed, match="not one the site takes") as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "error"


def test_too_long_after_a_step_is_refused(tmp_path):
    def stretch(f):
        return Fetched(path=f.path, ext=f.ext, info={**f.info, "duration": 601})

    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("s", RecordStep("s", [], stretch))])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "too_long"


def test_a_file_outside_the_job_folder_is_refused(tmp_path):
    secret = tmp_path / "data"
    secret.mkdir()
    key = write(str(secret), "key", b"sor_secret")
    work = tmp_path / "work"
    work.mkdir()

    def point_at_key(f):
        return Fetched(path=key, ext="mp3", info=f.info)

    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("evil", RecordStep("evil", [], point_at_key))])
    with pytest.raises(FetchFailed, match="outside the job's folder"):
        p.run(job(), str(work))


def test_a_link_out_of_the_job_folder_is_refused(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = write(str(tmp_path), "outside.mp3")

    def link(f):
        path = os.path.join(os.path.dirname(f.path), "link.mp3")
        os.symlink(outside, path)
        return Fetched(path=path, ext="mp3", info=f.info)

    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("l", RecordStep("l", [], link))])
    with pytest.raises(FetchFailed, match="outside the job's folder"):
        p.run(job(), str(work))


def test_a_plugin_cannot_raise_the_limits(tmp_path):
    def cheat(f):
        with open(f.path, "ab") as out:
            out.write(b"y" * 2000)
        return f

    class Cheat(RecordStep):
        def run(self, fetched, job, log):
            job.limits["max_bytes"] = 1 << 40
            return cheat(fetched)

    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("cheat", Cheat("cheat", []))])
    with pytest.raises(FetchFailed) as e:
        p.run(job(), str(tmp_path))
    assert e.value.reason == "too_big"


def test_a_step_must_return_fetched(tmp_path):
    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("bad", RecordStep("bad", [], lambda f: None))])
    with pytest.raises(FetchFailed, match="not a Fetched"):
        p.run(job(), str(tmp_path))


def test_job_never_carries_the_key():
    assert set(Job.__dataclass_fields__) == {"id", "url", "limits", "hints", "allow_hosts"}


# --- the worker --------------------------------------------------------------


@pytest.fixture
def paired(site, settings):
    KeyStore(settings.data_dir).save("sor_good")
    site.valid_keys.add("sor_good")


def leftover(settings):
    return [n for n in os.listdir(settings.tmp_dir) if n.startswith("sorceress-job-")]


def test_source_and_steps_reach_done(site, settings, paired, make_worker, tmp_path):
    record = []
    first = FileSource("my-source", raises=NotFound())
    second = FileSource("yt-dlp", ext="webm")
    p = Pipeline(sources=[("my-source", first), ("yt-dlp", second)],
                 steps=[("a", RecordStep("a", record)), ("b", RecordStep("b", record))])
    site.add_job(hints={"title": "A song", "artist": "Band", "duration_s": 200, "junk": "x"})
    make_worker(pipeline=p).step()
    done = site.done["job1"]
    assert done["info"]["source"] == "yt-dlp"
    assert done["info"]["steps"] == ["a", "b"]
    assert done["ext"] == "webm" and done["bytes"] == 100
    seen = first.calls[0][0]
    assert seen.hints == {"title": "A song", "artist": "Band", "duration_s": 200}
    assert seen.url == "https://www.youtube.com/watch?v=abc"
    assert "sor_good" not in repr(seen)
    assert leftover(settings) == []


def test_allowlist_still_comes_first(site, settings, paired, make_worker):
    src = FileSource("any")
    site.add_job(url="https://evil.example/x", hints={"title": "A song"})
    make_worker(pipeline=Pipeline(sources=[("any", src)])).step()
    assert site.failed["job1"]["reason"] == "not_allowed"
    assert src.calls == []


def test_guardrail_failure_reported_and_cleaned(site, settings, paired, make_worker):
    p = Pipeline(sources=[("src", FileSource("src", ext="exe"))])
    site.add_job()
    make_worker(pipeline=p).step()
    assert site.failed["job1"]["reason"] == "error"
    assert "job1" not in site.parts
    assert leftover(settings) == []


# --- the plugin registry -----------------------------------------------------


class PluginSource:
    def __init__(self, options):
        self.name = "fake-src"
        self.options = options

    def fetch(self, job, workdir, log):
        return Fetched(path=write(workdir, "a.mp3"), ext="mp3", info={})


class PluginStep:
    def __init__(self, options):
        if options.get("fail"):
            raise ValueError("bad option")
        self.name = "fake-step"
        self.options = options

    def run(self, fetched, job, log):
        return fetched


class NoMethod:
    def __init__(self, options):
        pass


@pytest.fixture
def entry_points(monkeypatch):
    module = types.ModuleType("fake_sorceress_plugin")
    module.PluginSource, module.PluginStep, module.NoMethod = PluginSource, PluginStep, NoMethod
    monkeypatch.setitem(sys.modules, "fake_sorceress_plugin", module)
    eps = [
        importlib.metadata.EntryPoint("fake-src", "fake_sorceress_plugin:PluginSource", SOURCE_GROUP),
        importlib.metadata.EntryPoint("fake-step", "fake_sorceress_plugin:PluginStep", STEP_GROUP),
        importlib.metadata.EntryPoint("no-method", "fake_sorceress_plugin:NoMethod", STEP_GROUP),
        importlib.metadata.EntryPoint("twice", "fake_sorceress_plugin:PluginStep", STEP_GROUP),
        importlib.metadata.EntryPoint("twice", "fake_sorceress_plugin:PluginSource", STEP_GROUP),
        importlib.metadata.EntryPoint("copy", "fake_sorceress_plugin:PluginStep", STEP_GROUP),
        importlib.metadata.EntryPoint("broken", "fake_sorceress_plugin:Missing", SOURCE_GROUP),
    ]

    def fake(*, group=None, **kw):
        return [ep for ep in eps if group is None or ep.group == group]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    return eps


def test_entry_points_are_found_and_get_their_options(entry_points):
    config = parse_fetcher_config({"sources": ["fake-src", "yt-dlp"], "steps": ["fake-step"],
                                   "plugins": {"fake-src": {"library": "/music"}, "fake-step": {"level": 2}}})
    p = build_pipeline(config)
    assert [n for n, _ in p.sources] == ["fake-src", "yt-dlp"]
    assert isinstance(p.sources[0][1], PluginSource) and p.sources[0][1].options == {"library": "/music"}
    assert isinstance(p.sources[1][1], YtDlpSource)
    assert isinstance(p.steps[0][1], PluginStep) and p.steps[0][1].options == {"level": 2}


@pytest.mark.parametrize(
    "raw,message",
    [
        ({"sources": ["nobody"]}, "unknown source 'nobody'; known sources: broken, fake-src, yt-dlp"),
        ({"steps": ["nobody"]}, "unknown step 'nobody'"),
        ({"steps": ["fake-src"]}, "unknown step 'fake-src'"),  # a source is not a step
        ({"steps": ["no-method"]}, "has no run\\(\\) method"),
        ({"steps": ["twice"]}, "more than one installed plugin"),
        ({"sources": ["broken"]}, "could not be loaded"),
        ({"steps": ["fake-step"], "plugins": {"fake-step": {"fail": True}}}, "failed to start: ValueError: bad"),
    ],
)
def test_registry_errors_at_start(entry_points, raw, message):
    with pytest.raises(ConfigError, match=message):
        build_pipeline(parse_fetcher_config(raw))


def test_builtin_names_cannot_be_taken(entry_points, tmp_path):
    config = parse_fetcher_config({"steps": ["copy"], "copy": {"folder": str(tmp_path)}})
    assert isinstance(build_pipeline(config).steps[0][1], CopyStep)


def test_default_pipeline_loads_no_entry_points(monkeypatch):
    def boom(**kw):
        raise AssertionError("entry points were read")

    monkeypatch.setattr(importlib.metadata, "entry_points", boom)
    p = build_pipeline(FetcherConfig())
    assert [n for n, _ in p.sources] == ["yt-dlp"] and p.steps == []


# --- copy --------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expect",
    [
        ("A song", "A song.mp3"),
        ("../../x", "x.mp3"),
        ("/etc/passwd", "etc_passwd.mp3"),
        ("..", "abc.mp3"),  # nothing left: the id
        ("Band - Song: Live/Remix?", "Band - Song_ Live_Remix.mp3"),
        ("\x00\n", "abc.mp3"),
        ("Café ☕", "Café.mp3"),
        ("Ünïcode — dash", "Ünïcode _ dash.mp3"),
    ],
)
def test_safe_filename(title, expect):
    assert safe_filename({"title": title, "id": "abc"}, "mp3") == expect


def test_safe_filename_falls_back_to_audio():
    assert safe_filename({"title": "../", "id": None}, "flac") == "audio.flac"
    assert len(safe_filename({"title": "x" * 500}, "flac")) == 150 + len(".flac")


def test_copy_stays_in_its_folder(tmp_path):
    folder = tmp_path / "copies"
    work = tmp_path / "work"
    work.mkdir()
    step = CopyStep({"folder": str(folder)})
    src = write(str(work), "audio.mp3", b"music")
    f = Fetched(path=src, ext="mp3", info={"title": "../../x", "id": "abc"})
    for _ in range(3):
        assert step.run(f, job(), LOG) is f
    assert sorted(os.listdir(folder)) == ["x (2).mp3", "x (3).mp3", "x.mp3"]
    assert (folder / "x.mp3").read_bytes() == b"music"
    assert sorted(os.listdir(tmp_path)) == ["copies", "work"]  # nothing written beside the folder


def test_copy_never_follows_a_link_in_its_folder(tmp_path):
    folder = tmp_path / "copies"
    folder.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    os.symlink(target, folder / "song.mp3")
    work = tmp_path / "work"
    work.mkdir()
    f = Fetched(path=write(str(work), "a.mp3", b"new"), ext="mp3", info={"title": "song"})
    CopyStep({"folder": str(folder)}).run(f, job(), LOG)
    assert target.read_bytes() == b"keep"
    assert (folder / "song (2).mp3").read_bytes() == b"new"


def test_copy_folder_checked_at_start(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(ConfigError, match="can't be created"):
        CopyStep({"folder": str(blocker / "sub")})


def test_copy_survives_the_job_folder(site, settings, paired, make_worker, tmp_path):
    folder = tmp_path / "copies"
    p = Pipeline(sources=[("src", FileSource("src"))], steps=[("copy", CopyStep({"folder": str(folder)}))])
    site.add_job()
    make_worker(pipeline=p).step()
    assert site.done["job1"]["info"]["steps"] == ["copy"]
    assert os.listdir(folder) == ["A song.mp3"]
    assert leftover(settings) == []


# --- convert -----------------------------------------------------------------


def test_convert_refused_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = parse_fetcher_config({"steps": ["convert"], "convert": {"format": "flac"}})
    with pytest.raises(ConfigError, match="needs ffmpeg"):
        build_pipeline(config)


@pytest.mark.parametrize(
    "fmt,bitrate,tail",
    [
        ("flac", None, ["-c:a", "flac"]),
        ("mp3", None, ["-c:a", "libmp3lame", "-b:a", "192k"]),
        ("opus", "128k", ["-c:a", "libopus", "-b:a", "128k"]),
        ("m4a", None, ["-c:a", "aac", "-b:a", "192k"]),
    ],
)
def test_convert_argv(fmt, bitrate, tail):
    step = ConvertStep({"format": fmt, "bitrate": bitrate}, which=lambda n: "/usr/bin/ffmpeg")
    argv = step.argv("/w/source-1/audio.webm", f"/w/source-1/converted.{fmt}")
    assert argv[0] == "/usr/bin/ffmpeg"
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-i") + 1] == "file:/w/source-1/audio.webm"
    assert argv[argv.index("-map_metadata") + 1] == "0"  # tags kept
    assert argv[-len(tail) - 2:] == tail + ["-y", f"file:/w/source-1/converted.{fmt}"]


def test_convert_runs_ffmpeg_without_a_shell(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        with open(argv[-1][len("file:"):], "wb") as f:
            f.write(b"flac data")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    step = ConvertStep({"format": "flac"}, which=lambda n: "/usr/bin/ffmpeg", run=run)
    src = write(str(tmp_path), "audio.webm")
    out = step.run(Fetched(path=src, ext="webm", info={"title": "T"}), job(), LOG)
    argv, kw = calls[0]
    assert isinstance(argv, list) and not kw.get("shell")
    assert kw["stdin"] == subprocess.DEVNULL and kw["timeout"] > 0
    assert out.ext == "flac" and out.path == str(tmp_path / "converted.flac") and out.info == {"title": "T"}
    assert not os.path.exists(src)


def test_convert_skips_a_file_already_in_the_format(tmp_path):
    step = ConvertStep({"format": "mp3"}, which=lambda n: "/usr/bin/ffmpeg", run=lambda *a, **k: 1 / 0)
    f = Fetched(path=write(str(tmp_path), "a.mp3"), ext="mp3", info={})
    assert step.run(f, job(), LOG) is f


def test_convert_failure_is_reported(tmp_path):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, b"", b"line one\nInvalid data found\n")

    step = ConvertStep({"format": "flac"}, which=lambda n: "/usr/bin/ffmpeg", run=run)
    with pytest.raises(FetchFailed, match="ffmpeg failed: Invalid data found") as e:
        step.run(Fetched(path=write(str(tmp_path), "a.webm"), ext="webm", info={}), job(), LOG)
    assert e.value.reason == "error"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH")
def test_convert_with_real_ffmpeg_keeps_tags(tmp_path):
    src = str(tmp_path / "audio.mp3")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1",
                    "-metadata", "title=Tagged song", "-c:a", "libmp3lame", src], check=True)
    out = ConvertStep({"format": "flac"}).run(Fetched(path=src, ext="mp3", info={}), job(), LOG)
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-i", out.path], capture_output=True, text=True)
    assert out.ext == "flac" and "Tagged song" in probe.stderr


# --- the private-address guard -----------------------------------------------


@pytest.fixture
def private_dns(monkeypatch):
    """Every name resolves to a private address, as names on a home network do."""
    def resolve(host, port, *a, **kw):
        addr = host if host[0].isdigit() else "192.168.1.50"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 0))]

    monkeypatch.setattr(fetch_mod, "_real_getaddrinfo", resolve)


def test_local_hosts_exempt_only_the_named_hosts(private_dns):
    allowed = {("lidarr.lan", None), ("192.168.1.20", "8686")}
    with public_addresses_only(allowed):
        assert socket.getaddrinfo("lidarr.lan", 8686)
        assert socket.getaddrinfo("LIDARR.lan.", 443)  # any port when none is given
        assert socket.getaddrinfo("192.168.1.20", 8686)
        assert socket.getaddrinfo("192.168.1.20", "8686")
        for host, port in [("192.168.1.20", 80), ("router.lan", 80), ("10.0.0.1", 8686), ("lidarr.lan.evil", 80),
                           ("sub.lidarr.lan", 80)]:
            with pytest.raises(socket.gaierror, match=PRIVATE_REFUSED):
                socket.getaddrinfo(host, port)
        with public_addresses_only():  # yt-dlp's own guard inside a plugin's: no exemptions
            with pytest.raises(socket.gaierror, match=PRIVATE_REFUSED):
                socket.getaddrinfo("lidarr.lan", 8686)
        assert socket.getaddrinfo("lidarr.lan", 8686)  # back to the outer guard
    assert socket.getaddrinfo("router.lan", 80)  # and no guard once it ends


def test_plugins_run_under_the_guard(private_dns, tmp_path):
    reached = []

    class LookUp(FileSource):
        def fetch(self, job, workdir, log):
            reached.append(socket.getaddrinfo("lidarr.lan", 8686))
            with pytest.raises(socket.gaierror, match=PRIVATE_REFUSED):
                socket.getaddrinfo("nas.lan", 445)
            return super().fetch(job, workdir, log)

    p = Pipeline(sources=[("l", LookUp("l"))], local_hosts=frozenset({("lidarr.lan", None)}))
    p.run(job(), str(tmp_path))
    assert reached


def test_yt_dlp_source_passes_the_job_through(tmp_path):
    seen = {}

    def fetcher(url, limits, workdir, *, server_hosts=None, cache_dir=None):
        seen.update(url=url, limits=limits, workdir=workdir, server_hosts=server_hosts, cache_dir=cache_dir)
        return Fetched(path=write(workdir, "audio.webm"), ext="webm", info={})

    src = YtDlpSource({}, fetcher=fetcher, cache_dir="/c")
    src.fetch(job(allow_hosts=("youtube.com",)), str(tmp_path), LOG)
    assert seen == {"url": "https://www.youtube.com/watch?v=abc", "limits": LIMITS, "workdir": str(tmp_path),
                    "server_hosts": ["youtube.com"], "cache_dir": "/c"}


# --- the example plugin in examples/ ------------------------------------------


@pytest.fixture
def example(monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(os.path.dirname(__file__), "..", "examples", "plugin"))
    import sorceress_example

    return sorceress_example


def test_example_plugin(example, tmp_path, site, settings, paired, make_worker):
    music = tmp_path / "music" / "Band"
    music.mkdir(parents=True)
    (music / "01 A Song.flac").write_bytes(b"f" * 50)
    (music / "notes.txt").write_text("A song")
    src = example.FolderSource({"folder": str(tmp_path / "music")})
    later = FileSource("yt-dlp", ext="webm")
    p = Pipeline(sources=[("folder", src), ("yt-dlp", later)],
                 steps=[("fill-from-hints", example.FillFromHints({}))])
    site.add_job("found", hints={"title": "A song", "artist": "Band"})
    site.add_job("missing", hints={"title": "Other song"})
    w = make_worker(pipeline=p)
    w.step()
    w.step()
    found, missing = site.done["found"], site.done["missing"]
    assert (found["ext"], found["bytes"], found["info"]["source"]) == ("flac", 50, "folder")
    assert (missing["info"]["source"], missing["info"]["title"]) == ("yt-dlp", "A song")
    assert missing["info"]["steps"] == ["fill-from-hints"]
    assert (music / "01 A Song.flac").exists()  # the original stays where it is


def test_example_plugin_checks_its_options(example, tmp_path):
    with pytest.raises(ConfigError):
        example.FolderSource({"folder": str(tmp_path / "nope")})
    with pytest.raises(ConfigError):
        example.FillFromHints({"x": 1})
