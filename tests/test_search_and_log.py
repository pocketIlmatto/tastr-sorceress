"""Search jobs (no link: find the song on YouTube by its hints) and the job log sent to the site."""

import functools
import logging

import pytest

from tastr_sorceress.fetch import fetch
from tastr_sorceress.keystore import KeyStore
from tastr_sorceress.plugins import FetchFailed, clean_hints
from tastr_sorceress.search import find_song, pick, query_for

LIMITS = {"max_seconds": 10800, "max_bytes": 1 << 30, "part_bytes": 5 << 20}


@pytest.fixture
def paired(site, settings):
    KeyStore(settings.data_dir).save("sor_good")
    site.valid_keys.add("sor_good")


def entry(vid, title, duration, **kw):
    return {"id": vid, "title": title, "duration": duration, "channel": "Lumen",
            "url": f"https://www.youtube.com/watch?v={vid}", **kw}


# ---- picking a search result ----

def test_the_result_closest_to_the_tracks_length_wins():
    entries = [entry("ext", "Lumen Park (Extended Mix)", 412), entry("radio", "Lumen Park", 246),
               entry("live", "Lumen Park live", 250, live_status="is_live")]
    link, got = pick(entries, {"title": "Lumen Park", "duration_s": 245.3}, LIMITS, None)
    assert link == "https://www.youtube.com/watch?v=radio" and got["title"] == "Lumen Park"


def test_no_result_near_the_length_fails_rather_than_saving_the_wrong_version():
    entries = [entry("ext", "Lumen Park (Extended Mix)", 412)]
    with pytest.raises(FetchFailed) as e:
        pick(entries, {"title": "Lumen Park", "duration_s": 245}, LIMITS, None)
    assert e.value.reason == "unavailable" and "4:05" in e.value.detail and "Extended Mix" in e.value.detail


def test_slack_grows_with_long_tracks():
    entries = [entry("a", "Long one", 3600 + 150)]  # 2.5 minutes off an hour: inside 5 %
    assert pick(entries, {"title": "Long one", "duration_s": 3600}, LIMITS, None)[0].endswith("v=a")


def test_without_a_length_the_first_result_with_the_title_wins():
    entries = [entry("a", "Some other song", 200), entry("b", "Lumen - Lumen Park (Official)", 240)]
    assert pick(entries, {"title": "Lumen Park"}, LIMITS, None)[0].endswith("v=b")
    assert pick(entries, {"title": "Nothing like it"}, LIMITS, None)[0].endswith("v=a")


def test_results_off_the_allowlist_or_too_long_are_skipped():
    entries = [{"id": "x", "title": "Lumen Park", "duration": 245, "url": "https://evil.example/watch?v=x"},
               entry("long", "Lumen Park", 20000), entry("ok", "Lumen Park", 245)]
    assert pick(entries, {"title": "Lumen Park", "duration_s": 245}, LIMITS, None)[0].endswith("v=ok")
    with pytest.raises(FetchFailed):
        pick(entries[:2], {"title": "Lumen Park"}, LIMITS, None)
    with pytest.raises(FetchFailed):  # the site's list for the job narrows it too
        pick(entries, {"title": "Lumen Park"}, LIMITS, ["soundcloud.com"])


def test_the_query_and_hints():
    assert query_for({"title": "Lumen Park", "artist": "Lumen", "words": "radio edit"}) == "Lumen Lumen Park radio edit"
    assert clean_hints({"title": " x ", "words": "live\x00", "note": "never", "duration_s": 3}) == \
        {"title": "x", "words": "live", "duration_s": 3}


def test_find_song_searches_youtube_only():
    seen = {}

    class YDL:
        def __init__(self, params):
            seen["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            seen["url"], seen["download"] = url, download
            return {"entries": [entry("radio", "Lumen Park", 246)]}

    link = find_song({"title": "Lumen Park", "artist": "Lumen", "duration_s": 245}, LIMITS, ydl_class=YDL)
    assert link == "https://www.youtube.com/watch?v=radio"
    assert seen["url"] == "ytsearch8:Lumen Lumen Park" and seen["download"] is False
    assert seen["params"]["allowed_extractors"] == [r"youtube(:.+)?"]
    with pytest.raises(FetchFailed):
        find_song({"artist": "Lumen"}, LIMITS, ydl_class=YDL)  # no title, no search


# ---- search jobs in the worker ----

def test_a_search_job_fetches_what_the_search_picked(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 1000
    asked = []

    def searcher(hints, limits, **kw):
        asked.append(hints)
        return "https://www.youtube.com/watch?v=abc"

    site.add_job(url=None, search=True, hints={"title": "Lumen Park", "artist": "Lumen", "words": "radio edit"})
    make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl), searcher=searcher).step()
    assert asked == [{"title": "Lumen Park", "artist": "Lumen", "words": "radio edit"}]
    assert site.done["job1"]["info"]["webpage_url"] == "https://www.youtube.com/watch?v=abc"


def test_a_search_that_finds_nothing_fails_the_job(site, settings, paired, make_worker, fake_ydl):
    def searcher(hints, limits, **kw):
        raise FetchFailed("unavailable", "no result is close to the track's length")

    site.add_job(url=None, search=True, hints={"title": "Lumen Park"})
    make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl), searcher=searcher).step()
    assert site.failed["job1"]["reason"] == "unavailable" and fake_ydl.instances == []


def test_a_searchs_pick_still_passes_the_allowlist(site, settings, paired, make_worker, fake_ydl):
    site.add_job(url=None, search=True, hints={"title": "Lumen Park"})
    make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl),
                searcher=lambda hints, limits, **kw: "https://evil.example/x").step()
    assert site.failed["job1"]["reason"] == "not_allowed" and fake_ydl.instances == []


def test_a_job_without_a_link_and_not_a_search_is_refused(site, settings, paired, make_worker, fake_ydl):
    site.add_job(url=None)
    make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl)).step()
    assert site.failed["job1"]["reason"] == "not_allowed"


# ---- the job's log on the site ----

def test_each_step_is_sent_to_the_site(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * (3 << 20)
    site.add_job()
    make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl)).step()
    texts = [line["text"] for line in site.logs["job1"]]
    assert texts[0].startswith("Job job1 (track) taken")
    assert any(t.startswith("Found [youtube] A song by Band, 3:20 long") for t in texts)
    assert any(t.startswith("Downloaded 100%") for t in texts)
    assert any(t.startswith("Uploaded part 1/1") for t in texts)
    assert all(line["level"] != "debug" for line in site.logs["job1"])  # info unless the helper is verbose
    assert all("sor_good" not in t for t in texts)
    assert site.done["job1"]


def test_verbose_sends_debug_lines_too(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 1000
    site.add_job()
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl)).step()
    finally:
        root.setLevel(old)
    assert any(line["level"] == "debug" and line["text"].startswith("Job job1: sha256") for line in site.logs["job1"])


def test_a_site_without_the_log_route_still_gets_the_job(site, settings, paired, make_worker, fake_ydl):
    fake_ydl.payload = b"x" * 1000
    site.override["POST /jobs/job1/log"] = (404, {"error": "not_found"})
    site.add_job()
    make_worker(fetcher=functools.partial(fetch, ydl_class=fake_ydl)).step()
    assert site.done["job1"]
    assert len([p for p in site.paths("POST") if p.endswith("/log")]) == 1  # gave up after the first 404


class SlowClient:
    """Stands in for the site client: each call takes `delay` seconds and answers `answers` in turn (then 200)."""

    def __init__(self, delay=0.0, answers=()):
        import threading

        self.delay, self.answers, self.calls = delay, list(answers), []
        self.lock = threading.Lock()

    def call(self, method, path, body=None, **kw):
        import time

        from tastr_sorceress.api import Response

        with self.lock:
            self.calls.append(body)
        time.sleep(self.delay)
        status, wait = self.answers.pop(0) if self.answers else (200, None)
        return Response(status, {}, wait)


def test_a_slow_site_never_holds_up_the_job():
    import time

    from tastr_sorceress.joblog import SiteLog

    client = SlowClient(delay=2.0)
    log = logging.getLogger("tastr_sorceress.fetch")
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.INFO)
    try:
        started = time.monotonic()
        with SiteLog(client, "job1", send_every_s=0.01, finish_s=0.2):
            for i in range(120):  # more than two batches: the job thread never waits for a send
                log.info("line %d", i)
            in_job = time.monotonic() - started
        total = time.monotonic() - started
    finally:
        root.setLevel(old)
    assert in_job < 0.5 and total < 1.0


def test_the_log_backs_off_when_the_site_asks():
    import time

    from tastr_sorceress.joblog import SiteLog

    client = SlowClient(answers=[(429, 120.0)])
    log = logging.getLogger("tastr_sorceress.fetch")
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.INFO)
    try:
        with SiteLog(client, "job1", send_every_s=0.01, finish_s=0.5) as site_log:
            log.info("first")
            deadline = time.monotonic() + 2
            while not client.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            for i in range(60):
                log.info("more %d", i)
            time.sleep(0.2)
    finally:
        root.setLevel(old)
    assert len(client.calls) == 1  # nothing more sent during the pause the site asked for
    assert site_log.dropped == 61 and site_log.sent == 0
