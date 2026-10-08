import io
import json

import pytest

from aria_code.apps.cli import release_download as transfers


class Response(io.BytesIO):
    def __init__(self, data, *, status=200, headers=None, interrupt=False):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}
        self.interrupt = interrupt

    def read1(self, size):
        if self.interrupt and self.tell():
            raise OSError("connection interrupted")
        return super().read1(4 if self.interrupt else size)


def test_interrupted_download_resumes_the_confirmed_range(monkeypatch, tmp_path):
    requests = []
    def open_response(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            return Response(b"abcdefgh", headers={"Content-Length": "8", "ETag": '"release"'}, interrupt=True)
        assert request.get_header("Range") == "bytes=4-"
        assert request.get_header("If-range") == '"release"'
        return Response(b"efgh", status=206, headers={"Content-Range": "bytes 4-7/8", "ETag": '"release"'})
    monkeypatch.setattr(transfers.urllib.request, "urlopen", open_response)
    monkeypatch.setattr(transfers.time, "sleep", lambda _: None)
    target = tmp_path / "release.tar.gz"
    transfers.download("https://example.test/release", target)
    assert target.read_bytes() == b"abcdefgh"
    assert len(requests) == 2
    assert not target.with_name(target.name + ".part").exists()


def seed_partial(target, *, validator='"old"'):
    target.with_name(target.name + ".part").write_bytes(b"abcd")
    target.with_name(target.name + ".part.json").write_text(json.dumps({
        "url": "https://example.test/release", "validator": validator, "total": 8,
    }))


def test_server_ignoring_range_restarts_instead_of_appending(monkeypatch, tmp_path):
    target = tmp_path / "release"
    seed_partial(target)
    monkeypatch.setattr(transfers.urllib.request, "urlopen", lambda *a, **kw:
                        Response(b"new-body", headers={"Content-Length": "8"}))
    transfers.download("https://example.test/release", target)
    assert target.read_bytes() == b"new-body"


@pytest.mark.parametrize("headers", [
    {"Content-Range": "bytes 0-7/8"},
    {"Content-Range": "bytes 4-7/9"},
    {"Content-Range": "bytes 4-7/8", "ETag": '"changed"'},
])
def test_invalid_resume_never_replaces_a_completed_file(monkeypatch, tmp_path, headers):
    target = tmp_path / "release"
    target.write_bytes(b"previous")
    seed_partial(target)
    monkeypatch.setattr(transfers.urllib.request, "urlopen", lambda *a, **kw:
                        Response(b"efgh", status=206, headers=headers))
    with pytest.raises(ValueError):
        transfers.download("https://example.test/release", target)
    assert target.read_bytes() == b"previous"
    assert not target.with_name(target.name + ".part").exists()


def test_trickling_data_cannot_bypass_total_deadline(monkeypatch, tmp_path):
    now = [0.0]
    class Trickle(Response):
        def read1(self, size):
            now[0] += 1
            return b"x"
    monkeypatch.setattr(transfers.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transfers.urllib.request, "urlopen", lambda *a, **kw: Trickle(b""))
    target = tmp_path / "release"
    with pytest.raises(transfers.DownloadTimeout):
        transfers.download("https://example.test/release", target, deadline_seconds=2.5)
    assert not target.exists()
    assert target.with_name(target.name + ".part").read_bytes() == b"xx"
    assert now[0] == 3


def test_partial_survives_failure_and_resumes_on_next_invocation(monkeypatch, tmp_path):
    target = tmp_path / "release"
    monkeypatch.setattr(transfers.urllib.request, "urlopen", lambda *a, **kw:
                        Response(b"abcdefgh", headers={"Content-Length": "8"}, interrupt=True))
    with pytest.raises(OSError):
        transfers.download("https://example.test/release", target, attempts=1)
    assert target.with_name(target.name + ".part").read_bytes() == b"abcd"
    requests = []
    def resume(request, **kwargs):
        requests.append(request)
        return Response(b"efgh", status=206, headers={"Content-Range": "bytes 4-7/8"})
    monkeypatch.setattr(transfers.urllib.request, "urlopen", resume)
    transfers.download("https://example.test/release", target)
    assert requests[0].get_header("Range") == "bytes=4-"
    assert target.read_bytes() == b"abcdefgh"


def test_concurrent_updates_cannot_append_to_the_same_partial(tmp_path):
    with transfers._lock(tmp_path / "release.lock"):
        with pytest.raises(RuntimeError, match="Another update"):
            with transfers._lock(tmp_path / "release.lock"):
                pytest.fail("lock must be exclusive")
