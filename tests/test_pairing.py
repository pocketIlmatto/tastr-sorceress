import os
import stat

from tastr_sorceress import __version__
from tastr_sorceress.api import Client
from tastr_sorceress.keystore import KeyStore
from tastr_sorceress.pairing import confirm_url, pair


def test_pairing_shows_code_and_returns_key(site, settings, rec):
    site.poll_script.extend(["waiting", "waiting", "paired"])
    key = pair(Client(settings.url), settings, sleep=rec.sleep, out=rec.out, poll_interval=3.0)
    assert key == "sor_key1"
    shown = "\n".join(rec.lines)
    assert "HXKM-4TQR" in shown
    assert f"{site.url}/bank/sorceress" in shown
    start = [r for r in site.requests if r["path"].endswith("/pair/start")][0]
    assert start["json"]["name"] == "test helper"
    assert start["json"]["version"] == __version__
    assert start["headers"]["User-Agent"] == f"tastr-sorceress/{__version__}"
    assert "Authorization" not in start["headers"]
    polls = [r for r in site.requests if r["path"].endswith("/pair/poll")]
    assert len(polls) == 3
    assert polls[0]["json"] == {"pairing_id": "p1", "poll_secret": "s1"}
    assert rec.sleeps.count(3.0) == 2  # polled every 3 s while waiting


def test_expired_code_starts_over(site, settings, rec):
    site.poll_script.extend(["waiting", "expired", "paired"])
    key = pair(Client(settings.url), settings, sleep=rec.sleep, out=rec.out, poll_interval=0.0)
    assert key == "sor_key1"
    shown = "\n".join(rec.lines)
    assert "HXKM-4TQR" in shown and "ABCD-2345" in shown
    assert len([p for p in site.paths() if p.endswith("/pair/start")]) == 2


def test_rate_limited_start_waits(site, settings, rec):
    site.override["POST /pair/start"] = (429, {"error": "rate_limited"})

    calls = {"n": 0}

    def sleep(s):
        rec.sleeps.append(s)
        calls["n"] += 1
        if calls["n"] == 1:
            del site.override["POST /pair/start"]

    key = pair(Client(settings.url), settings, sleep=sleep, out=rec.out, poll_interval=0.0)
    assert key.startswith("sor_")
    assert rec.sleeps[0] >= 600


def test_confirm_url_stays_on_site(settings):
    assert confirm_url(settings, "/bank/sorceress") == settings.url + "/bank/sorceress"
    assert confirm_url(settings, "//evil.example/x") == settings.url + "/bank/sorceress"
    assert confirm_url(settings, "https://evil.example/") == settings.url + "/bank/sorceress"
    assert confirm_url(settings, None) == settings.url + "/bank/sorceress"


def test_worker_pairs_and_saves_key_0600(site, settings, make_worker):
    w = make_worker()
    w.step()
    path = KeyStore(settings.data_dir).path
    assert path.read_text().strip() == "sor_key1"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert os.listdir(settings.data_dir) == ["key"]
    assert site.heartbeats and site.heartbeats[0]["version"] == __version__
    assert site.heartbeats[0]["yt_dlp"]


def test_401_deletes_key_and_pairs_again(site, settings, make_worker):
    KeyStore(settings.data_dir).save("sor_revoked")
    w = make_worker()
    w.step()  # heartbeat with the old key gets 401
    assert KeyStore(settings.data_dir).load() is None
    w.step()  # pairs again
    assert KeyStore(settings.data_dir).load() == "sor_key1"
    auths = [r["headers"].get("Authorization") for r in site.requests if "/jobs/next" in r["path"]]
    assert auths[-1] == "Bearer sor_key1"


def test_401_on_jobs_next_re_pairs(site, settings, make_worker):
    w = make_worker()
    w.step()
    site.valid_keys.clear()  # the person removed the helper on the site
    w.step()
    assert KeyStore(settings.data_dir).load() is None
    w.step()
    assert KeyStore(settings.data_dir).load() == "sor_key2"
