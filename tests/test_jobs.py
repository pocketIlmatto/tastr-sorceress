import functools
import hashlib
import os

import pytest
from yt_dlp.utils import DownloadError

from tastr_sorceress.fetch import fetch
from tastr_sorceress.keystore import KeyStore

MIB = 1 << 20


@pytest.fixture
def paired(site, settings):
    KeyStore(settings.data_dir).save("sor_good")
    site.valid_keys.add("sor_good")


def worker_with(make_worker, ydl):
    return make_worker(fetcher=functools.partial(fetch, ydl_class=ydl))


def leftover(settings):
    return [n for n in os.listdir(settings.tmp_dir) if n.startswith("sorceress-job-")]


def test_happy_path(site, settings, paired, make_worker, fake_ydl):
    payload = os.urandom(12 * MIB + 123)
    fake_ydl.payload = payload
    site.add_job()
    w = worker_with(make_worker, fake_ydl)
    w.step()

    parts = site.parts["job1"]
    assert sorted(parts) == [1, 2, 3]
    assert [len(parts[n]) for n in (1, 2, 3)] == [5 * MIB, 5 * MIB, 2 * MIB + 123]
    assert b"".join(parts[n] for n in (1, 2, 3)) == payload
    done = site.done["job1"]
    assert done["parts"] == 3
    assert done["bytes"] == len(payload)
    assert done["sha256"] == hashlib.sha256(payload).hexdigest()
    assert done["ext"] == "webm"
    assert done["info"] == {"id": "abc", "extractor": "youtube", "title": "A song", "duration": 200.0,
                            "uploader": "Someone", "artist": "Band",
                            "webpage_url": "https://www.youtube.com/watch?v=abc", "upload_date": "20260101",
                            "source": "yt-dlp", "steps": []}
    assert leftover(settings) == []
    for r in site.requests:
        if "/parts/" in r["path"]:
            assert r["headers"]["Authorization"] == "Bearer sor_good"
            assert r["headers"]["Content-Length"] == str(len(r["raw"]))

    params = fake_ydl.instances[0].params
    assert params["format"] == "bestaudio/best"
    assert params["noplaylist"] is True
    assert params["postprocessors"] == []
    assert "generic" not in params["allowed_extractors"]


def test_small_file_single_part(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 1000
    fake_ydl.ext = "m4a"
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert list(site.parts["job1"]) == [1]
    assert site.done["job1"]["ext"] == "m4a"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=abc",
        "https://evil.example/watch?v=abc",
        "https://youtube.com.evil.example/watch",
        "https://notyoutube.com/watch?v=abc",
        "https://user:pw@www.youtube.com/watch?v=abc",
        "https://www.youtube.com:8443/watch?v=abc",
        "file:///etc/passwd",
        "",
    ],
)
def test_allowlist_refusal(site, settings, paired, make_worker, fake_ydl, url):
    site.add_job(url=url)
    worker_with(make_worker, fake_ydl).step()
    assert site.failed["job1"]["reason"] == "not_allowed"
    assert fake_ydl.instances == []  # yt-dlp never touched
    assert "job1" not in site.parts
    assert leftover(settings) == []


def test_server_allow_hosts_narrows(site, settings, paired, make_worker, fake_ydl):
    site.add_job(url="https://soundcloud.com/a/b", allow_hosts=["youtube.com"])
    worker_with(make_worker, fake_ydl).step()
    assert site.failed["job1"]["reason"] == "not_allowed"
    assert fake_ydl.instances == []


@pytest.mark.parametrize(
    "info,reason",
    [
        ({"duration": 10801}, "too_long"),
        ({"is_live": True}, "live"),
        ({"live_status": "is_upcoming"}, "live"),
        ({"_type": "playlist", "entries": []}, "playlist"),
        ({"filesize": (1 << 30) + 1}, "too_big"),
        ({"webpage_url": "https://evil.example/x"}, "not_allowed"),
    ],
)
def test_limits_checked_before_download(site, settings, paired, make_worker, fake_ydl, info, reason):
    fake_ydl.info = {**fake_ydl.info, **info}
    fake_ydl.payload = b"x" * 100
    site.add_job()
    w = worker_with(make_worker, fake_ydl)
    w.step()
    assert site.failed["job1"]["reason"] == reason
    assert "job1" not in site.parts and "job1" not in site.done
    assert leftover(settings) == []


def test_max_bytes_enforced_during_download(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * (3 * MIB)
    job = site.add_job()
    job["limits"]["max_bytes"] = 2 * MIB
    worker_with(make_worker, fake_ydl).step()
    assert site.failed["job1"]["reason"] == "too_big"
    assert "job1" not in site.parts
    assert leftover(settings) == []


@pytest.mark.parametrize(
    "message,reason",
    [
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser", "bot_check"),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "unavailable"),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "unavailable"),
        ("ERROR: [soundcloud] x: HTTP Error 404: Not Found", "unavailable"),
        ("ERROR: [youtube] abc: This live event will begin in 3 hours.", "live"),
        ("ERROR: Unsupported URL: https://www.youtube.com/foo", "not_allowed"),
        ("ERROR: something odd happened", "error"),
    ],
)
def test_failure_mapping(site, settings, paired, make_worker, fake_ydl, message, reason):
    fake_ydl.extract_error = DownloadError(message)
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    failed = site.failed["job1"]
    assert failed["reason"] == reason
    assert len(failed["detail"]) <= 500
    assert not failed["detail"].startswith("ERROR:")
    assert leftover(settings) == []


def test_download_error_cleans_up(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.download_error = DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert site.failed["job1"]["reason"] == "error"
    assert leftover(settings) == []


def test_long_detail_is_cut(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.extract_error = DownloadError("ERROR: " + "x" * 2000)
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert len(site.failed["job1"]["detail"]) == 500


def test_part_retry_with_backoff(site, settings, paired, make_worker, fake_ydl, rec):
    fake_ydl.payload = os.urandom(6 * MIB)
    site.part_script.extend([500, 503])
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert sorted(site.parts["job1"]) == [1, 2]
    assert site.done["job1"]["bytes"] == 6 * MIB
    waits = [s for s in rec.sleeps if s > 0]
    assert len(waits) == 2 and waits[1] > waits[0]


def test_part_gives_up_after_retries(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 100
    site.part_script.extend([500] * 10)
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert site.failed["job1"]["reason"] == "error"
    assert "job1" not in site.done
    assert leftover(settings) == []


def test_lease_lost_drops_job(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 100
    site.part_script.append(409)
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert "job1" not in site.done and "job1" not in site.failed
    assert leftover(settings) == []


def test_413_reports_too_big(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 100
    site.part_script.append(413)
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert site.failed["job1"]["reason"] == "too_big"


def test_one_job_at_a_time(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 100
    site.add_job("a")
    site.add_job("b")
    w = worker_with(make_worker, fake_ydl)
    w.step()
    assert list(site.done) == ["a"]
    w.step()
    assert list(site.done) == ["a", "b"]


def test_done_report_honours_retry_after(site, settings, paired, make_worker, fake_ydl, rec):
    fake_ydl.payload = b"x" * 100
    site.done_script.append((503, 120))
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert site.done["job1"]["bytes"] == 100
    assert max(rec.sleeps) >= 120


def test_done_report_caps_a_huge_retry_after(site, settings, paired, make_worker, fake_ydl, rec):
    from tastr_sorceress.worker import MAX_REPORT_WAIT

    fake_ydl.payload = b"x" * 100
    site.done_script.append((429, 99999))
    site.add_job()
    worker_with(make_worker, fake_ydl).step()
    assert site.done["job1"]["bytes"] == 100
    assert max(rec.sleeps) == MAX_REPORT_WAIT
