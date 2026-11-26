import os
import stat
import threading

import pytest

from tastr_sorceress.api import Client, NetworkError
from tastr_sorceress.backoff import Backoff
from tastr_sorceress.cli import main
from tastr_sorceress.config import ConfigError, Settings
from tastr_sorceress.fetch import EXTRACTORS, map_error, url_allowed, ydl_options
from tastr_sorceress.keystore import KeyStore
from tastr_sorceress.upload import MIN_PART, UploadError, plan_parts

MIB = 1 << 20


def test_settings_from_env_defaults():
    s = Settings.from_env({"TASTR_URL": "https://tastr.example.com/"})
    assert s.url == "https://tastr.example.com"
    assert s.data_dir == "/data" and s.tmp_dir == "/tmp"
    assert s.name


@pytest.mark.parametrize("url", ["", "http://tastr.example.com", "ftp://x", "https://", "https://u:p@x.example",
                                 "https://x.example/?a=1"])
def test_settings_reject_bad_url(url):
    with pytest.raises(ConfigError):
        Settings.from_env({"TASTR_URL": url})


def test_settings_name_and_dirs():
    s = Settings.from_env({"TASTR_URL": "https://t.example", "SORCERESS_NAME": "Pi\nin the hall",
                           "SORCERESS_DATA": "/d", "SORCERESS_TMP": "/t"})
    assert (s.name, s.data_dir, s.tmp_dir) == ("Piin the hall", "/d", "/t")


def test_keystore_roundtrip(tmp_path):
    ks = KeyStore(str(tmp_path / "data"))
    assert ks.load() is None
    ks.save("sor_abc")
    assert ks.load() == "sor_abc"
    assert stat.S_IMODE(os.stat(ks.path).st_mode) == 0o600
    ks.delete()
    assert ks.load() is None
    ks.delete()  # no error when already gone


def test_keystore_ignores_garbage(tmp_path):
    ks = KeyStore(str(tmp_path))
    ks.path.write_text("not a key")
    assert ks.load() is None


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://www.youtube.com/watch?v=abc", True),
        ("https://youtube.com/watch?v=abc", True),
        ("https://youtu.be/abc", True),
        ("https://music.youtube.com/watch?v=abc", True),
        ("https://soundcloud.com/a/b", True),
        ("https://artist.bandcamp.com/track/x", True),
        ("https://www.mixcloud.com/a/b/", True),
        ("https://WWW.YouTube.com./watch?v=abc", True),
        ("http://www.youtube.com/watch?v=abc", False),
        ("https://youtube.com.evil.example/", False),
        ("https://evilyoutube.com/", False),
        ("https://user@youtube.com/", False),
        ("https://youtube.com:444/", False),
        ("https://you tube.com/", False),
        ("javascript:alert(1)", False),
        (None, False),
    ],
)
def test_url_allowed(url, ok):
    assert url_allowed(url) is ok


def test_url_allowed_needs_both_lists():
    assert url_allowed("https://soundcloud.com/x", ["soundcloud.com"])
    assert not url_allowed("https://soundcloud.com/x", ["youtube.com"])
    assert not url_allowed("https://example.com/x", ["example.com"])  # not on the helper's own list


@pytest.mark.parametrize(
    "size,part,expect",
    [
        (1, 32 * MIB, [(0, 1)]),
        (32 * MIB, 32 * MIB, [(0, 32 * MIB)]),
        (32 * MIB + 1, 32 * MIB, [(0, 32 * MIB), (32 * MIB, 1)]),
        (11 * MIB, 5 * MIB, [(0, 5 * MIB), (5 * MIB, 5 * MIB), (10 * MIB, MIB)]),
        (MIB, MIB, [(0, MIB)]),  # one small part is fine: it is the last
    ],
)
def test_plan_parts(size, part, expect):
    plan = plan_parts(size, part)
    assert plan == expect
    assert sum(n for _, n in plan) == size
    assert all(n >= MIN_PART for _, n in plan[:-1])
    assert all(n <= part for _, n in plan)


def test_plan_parts_refuses_small_parts():
    with pytest.raises(UploadError):
        plan_parts(2 * MIB, MIB)
    with pytest.raises(UploadError):
        plan_parts(0, 5 * MIB)


@pytest.mark.parametrize(
    "msg,reason",
    [
        ("Sign in to confirm you're not a bot", "bot_check"),
        ("Sign in to confirm you’re not a bot", "bot_check"),
        ("HTTP Error 429: Too Many Requests", "bot_check"),
        ("Sign in to confirm your age. This video may be inappropriate", "unavailable"),
        ("Video unavailable", "unavailable"),
        ("This video has been removed for violating YouTube's Terms of Service", "unavailable"),
        ("This live stream recording is not available.", "unavailable"),
        ("This live event will begin in a few moments.", "live"),
        ("Requested format is not available", "error"),
        ("No suitable extractor (Generic) found for URL https://x", "not_allowed"),
        ("\x1b[0;31mERROR:\x1b[0m Video unavailable", "unavailable"),
        ("Something else", "error"),
    ],
)
def test_map_error(msg, reason):
    assert map_error(msg) == reason


def test_real_yt_dlp_loads_only_allowed_extractors(tmp_path):
    """Builds a real YoutubeDL with our options (no network) and checks which extractors it has."""
    import yt_dlp
    from yt_dlp.utils import DownloadError

    with yt_dlp.YoutubeDL(ydl_options(str(tmp_path), None, 1 << 30, lambda d: None)) as ydl:
        names = {ie.IE_NAME.lower() for ie in ydl._ies.values()}
        assert "youtube" in names and "soundcloud" in names and "bandcamp" in names and "mixcloud" in names
        assert "generic" not in names
        assert all(any(__import__("re").fullmatch(p, n) for p in EXTRACTORS) for n in names)
        with pytest.raises(DownloadError) as e:
            ydl.extract_info("https://example.com/song.mp3", download=False)
        assert map_error(str(e.value)) == "not_allowed"


def test_backoff_grows_and_caps():
    b = Backoff(base=5, cap=300)
    delays = [b.next_delay() for _ in range(10)]
    assert delays[0] <= 5 and delays[1] > delays[0] * 1.4
    assert max(delays) <= 300
    b.reset()
    assert b.next_delay() <= 5


def test_client_does_not_follow_redirects(site):
    site.override["GET /jobs/next"] = (302, {})
    # http.server sends no Location; set one by using a raw handler instead
    c = Client(site.url, key="sor_x")
    resp = c.call("GET", "/jobs/next?wait=0", auth=False)
    assert resp.status == 302


def test_client_redirect_with_location_not_followed():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        c = Client(f"http://127.0.0.1:{srv.server_address[1]}", key="sor_x")
        resp = c.call("GET", "/jobs/next")
        assert resp.status == 307
        assert len(hits) == 1  # never went to /elsewhere
    finally:
        srv.shutdown()
        srv.server_close()


def test_client_network_error():
    c = Client("http://127.0.0.1:9", key="sor_x", timeout=2)
    with pytest.raises(NetworkError):
        c.call("GET", "/jobs/next")


def test_next_job_backs_off_on_5xx(site, settings, make_worker, rec):
    KeyStore(settings.data_dir).save("sor_good")
    site.valid_keys.add("sor_good")
    site.override["GET /jobs/next"] = (503, {"error": "busy"})
    w = make_worker()
    for _ in range(4):
        w.step()
    waits = [s for s in rec.sleeps if s > 0]
    assert len(waits) == 4
    assert waits == sorted(waits) and waits[-1] > waits[0] * 4


def test_next_job_waits_when_site_answers_at_once(site, settings, make_worker, rec):
    KeyStore(settings.data_dir).save("sor_good")
    site.valid_keys.add("sor_good")
    w = make_worker(min_poll_gap=5.0)
    w.step()
    assert rec.sleeps[-1] == 5.0
    assert "wait=0" in [p for p in site.paths("GET") if "/jobs/next" in p][0]


def test_cli_requires_url(monkeypatch, capsys):
    monkeypatch.delenv("TASTR_URL", raising=False)
    assert main(["status"]) == 2
    assert "TASTR_URL" in capsys.readouterr().err


def test_cli_unpair_deletes_key(monkeypatch, tmp_path, capsys):
    ks = KeyStore(str(tmp_path))
    ks.save("sor_abc")
    monkeypatch.setenv("TASTR_URL", "https://tastr.example.com")
    monkeypatch.setenv("SORCERESS_DATA", str(tmp_path))
    assert main(["unpair"]) == 0
    assert ks.load() is None
    assert "/bank/sorceress" in capsys.readouterr().out


def test_cli_status_unpaired(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TASTR_URL", "https://tastr.example.com")
    monkeypatch.setenv("SORCERESS_DATA", str(tmp_path))
    assert main(["status"]) == 1
    assert "not paired" in capsys.readouterr().out


def test_run_loop_stops(site, settings, make_worker):
    KeyStore(settings.data_dir).save("sor_good")
    site.valid_keys.add("sor_good")
    stop = threading.Event()
    calls = {"n": 0}

    def sleep(s):
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()

    stale = os.path.join(settings.tmp_dir, "sorceress-job-old")
    os.mkdir(stale)
    w = make_worker(sleep=sleep, heartbeat_s=0.05)
    w.run(stop)
    assert not os.path.exists(stale)
    assert site.heartbeats


def test_fetch_thread_cannot_resolve_private_addresses():
    import socket
    import threading

    from tastr_sorceress.fetch import PRIVATE_REFUSED, public_addresses_only

    with public_addresses_only():
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"):
            with pytest.raises(socket.gaierror, match=PRIVATE_REFUSED):
                socket.getaddrinfo(host, 443)
        seen = []
        other = threading.Thread(target=lambda: seen.append(socket.getaddrinfo("127.0.0.1", 443)))
        other.start()
        other.join()
        assert seen and seen[0]  # other threads (the heartbeat, the site's own calls) are not limited
    assert socket.getaddrinfo("127.0.0.1", 443)  # and the limit ends with the fetch


def test_private_address_refusal_maps_to_not_allowed():
    from tastr_sorceress.fetch import PRIVATE_REFUSED, map_error

    assert map_error(f"ERROR: Unable to download webpage: [Errno -2] {PRIVATE_REFUSED} (router.lan)") == "not_allowed"
