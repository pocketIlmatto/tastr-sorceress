"""A fake Tastr site (http.server in a thread) and a fake yt-dlp. No real network."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tastr_sorceress.api import Client
from tastr_sorceress.config import Settings
from tastr_sorceress.keystore import KeyStore
from tastr_sorceress.worker import Worker

PREFIX = "/pub/sorceress/v1"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Refuse any connection that isn't to the fake site on 127.0.0.1."""
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"test tried to reach {address}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class FakeTastr:
    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.valid_keys: set[str] = set()
        self.codes = deque(["HXKM-4TQR", "ABCD-2345", "WXYZ-6789"])
        self.poll_script: deque = deque()  # entries: "waiting" | "expired" | "paired"
        self.next_key = 1
        self.jobs: deque = deque()
        self.parts: dict[str, dict[int, bytes]] = {}
        self.part_script: deque = deque()  # status codes to answer part PUTs with, before 200
        self.done: dict[str, dict] = {}
        self.failed: dict[str, dict] = {}
        self.heartbeats: list[dict] = []
        self.override: dict[str, tuple[int, dict]] = {}  # "METHOD path" -> (status, body), always
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add_job(self, job_id="job1", url="https://www.youtube.com/watch?v=abc", **extra):
        job = {
            "job_id": job_id,
            "url": url,
            "purpose": "track",
            "lease_s": 900,
            "limits": {"max_seconds": 10800, "max_bytes": 1 << 30, "part_bytes": 5 << 20},
            "allow_hosts": ["youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "bandcamp.com",
                            "mixcloud.com"],
        }
        job.update(extra)
        self.jobs.append(job)
        return job

    def paths(self, method=None):
        return [r["path"] for r in self.requests if method is None or r["method"] == method]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path
                rec = {"method": self.command, "path": path, "headers": dict(self.headers), "raw": raw}
                fake.requests.append(rec)
                if not path.startswith(PREFIX):
                    return self._send(404, {"error": "not_found"})
                path = path[len(PREFIX):]
                key = f"{self.command} {path.split('?')[0]}"
                if key in fake.override:
                    return self._send(*fake.override[key])
                body = {}
                if self.headers.get("Content-Type") == "application/json" and raw:
                    body = json.loads(raw)
                rec["json"] = body
                if path == "/pair/start":
                    return self._send(200, {"pairing_id": "p1", "poll_secret": "s1", "code": fake.codes.popleft(),
                                            "expires_s": 600, "confirm_path": "/bank/sorceress"})
                if path == "/pair/poll":
                    step = fake.poll_script.popleft() if fake.poll_script else "paired"
                    if step == "waiting":
                        return self._send(202, {"state": "waiting"})
                    if step == "expired":
                        return self._send(410, {"state": "expired"})
                    new_key = f"sor_key{fake.next_key}"
                    fake.next_key += 1
                    fake.valid_keys.add(new_key)
                    return self._send(200, {"state": "paired", "key": new_key, "helper_id": "h1"})
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in fake.valid_keys:
                    return self._send(401, {"error": "unpaired"})
                if path == "/heartbeat":
                    fake.heartbeats.append(body)
                    return self._send(200, {"min_version": "0.1.0", "latest_version": "0.1.0"})
                if path.startswith("/jobs/next"):
                    if fake.jobs:
                        return self._send(200, fake.jobs.popleft())
                    return self._send(204)
                parts = path.split("/")
                if len(parts) == 5 and parts[3] == "parts" and self.command == "PUT":
                    if fake.part_script:
                        return self._send(fake.part_script.popleft(), {"error": "scripted"})
                    assert self.headers["Content-Type"] == "application/octet-stream"
                    fake.parts.setdefault(parts[2], {})[int(parts[4])] = raw
                    return self._send(200, {"n": int(parts[4]), "bytes": len(raw)})
                if len(parts) == 4 and parts[3] == "done":
                    fake.done[parts[2]] = body
                    return self._send(200, {"deposit_id": "d1"})
                if len(parts) == 4 and parts[3] == "failed":
                    fake.failed[parts[2]] = body
                    return self._send(200, {})
                return self._send(404, {"error": "not_found"})

            do_GET = do_POST = do_PUT = _handle

        return Handler


@pytest.fixture
def site():
    fake = FakeTastr()
    yield fake
    fake.close()


@pytest.fixture
def settings(site, tmp_path):
    data = tmp_path / "data"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return Settings(url=site.url, name="test helper", data_dir=str(data), tmp_dir=str(tmp))


class Recorder:
    def __init__(self):
        self.sleeps: list[float] = []
        self.lines: list[str] = []

    def sleep(self, s):
        self.sleeps.append(s)

    def out(self, line):
        self.lines.append(line)


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def make_worker(settings, rec):
    def make(fetcher=None, **kw):
        args = dict(
            client=Client(settings.url, timeout=10),
            keystore=KeyStore(settings.data_dir),
            sleep=rec.sleep,
            out=rec.out,
            poll_interval=0.0,
            long_poll_s=0,
            min_poll_gap=0.0,
        )
        if fetcher is not None:
            args["fetcher"] = fetcher
        args.update(kw)
        return Worker(settings, **args)

    return make


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL. Configure with class attributes via `fake_ydl`."""

    info: dict[str, Any] = {}
    ext = "webm"
    payload = b""
    extract_error: Exception | None = None
    download_error: Exception | None = None
    instances: list["FakeYDL"] = []

    def __init__(self, params):
        self.params = params
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        assert download is False
        if self.extract_error:
            raise self.extract_error
        return dict(self.info)

    def process_ie_result(self, info, download=True):
        assert download is True
        if self.download_error:
            raise self.download_error
        target = self.params["outtmpl"]["default"].replace("%(ext)s", self.ext)
        hooks = self.params["progress_hooks"]
        written = 0
        with open(target + ".part", "wb") as f:
            for i in range(0, max(len(self.payload), 1), 1 << 20):
                chunk = self.payload[i:i + (1 << 20)]
                f.write(chunk)
                written += len(chunk)
                for h in hooks:
                    h({"status": "downloading", "downloaded_bytes": written, "total_bytes": len(self.payload)})
        os.replace(target + ".part", target)
        return info


@pytest.fixture
def fake_ydl():
    class YDL(FakeYDL):
        info = {"id": "abc", "extractor": "youtube", "title": "A song", "duration": 200.0, "uploader": "Someone",
                "artist": "Band", "webpage_url": "https://www.youtube.com/watch?v=abc", "upload_date": "20260101",
                "ext": "webm"}
        instances = []

    return YDL
