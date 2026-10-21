import httpx
import pytest

from editor.drive import DriveClient, TransferError

BASE = "https://drive.example.org"


async def no_sleep(_):
    return None


def drive(handler, **kw):
    return DriveClient(transport=httpx.MockTransport(handler), sleep=no_sleep, **kw)


async def test_download_streams_and_checks_size(tmp_path):
    data = b"x" * 1000
    dest = tmp_path / "a.mov"
    out = await drive(lambda req: httpx.Response(200, content=data)).download(f"{BASE}/f?t=1", dest, 1000)
    assert out == dest and dest.read_bytes() == data
    assert not (tmp_path / "a.mov.part").exists()


async def test_download_resumes_with_range(tmp_path):
    data = bytes(range(256)) * 4
    dest = tmp_path / "a.mov"
    (tmp_path / "a.mov.part").write_bytes(data[:300])
    seen = []

    def handler(req):
        seen.append(req.headers.get("range"))
        return httpx.Response(206, content=data[300:], headers={"content-range": f"bytes 300-1023/1024"})

    await drive(handler).download(f"{BASE}/f", dest, 1024)
    assert seen == ["bytes=300-"]
    assert dest.read_bytes() == data


async def test_download_restarts_when_server_ignores_range(tmp_path):
    data = b"abcdef" * 10
    (tmp_path / "a.mov.part").write_bytes(b"junk")
    await drive(lambda req: httpx.Response(200, content=data)).download(f"{BASE}/f", tmp_path / "a.mov", len(data))
    assert (tmp_path / "a.mov").read_bytes() == data


async def test_download_size_mismatch_fails(tmp_path):
    with pytest.raises(TransferError):
        await drive(lambda req: httpx.Response(200, content=b"short")).download(f"{BASE}/f", tmp_path / "a", 99)


async def test_download_retries_a_dropped_connection(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("reset")
        return httpx.Response(200, content=b"12345")

    await drive(handler).download(f"{BASE}/f", tmp_path / "a", 5)
    assert len(calls) == 2


class FakeChunkServer:
    """Mimics Drive's chunked service upload."""

    def __init__(self, fail_chunk_once: int | None = None, fail_times: int = 1):
        self.fail_times = fail_times
        self.chunks: dict[int, bytes] = {}
        self.calls: list[str] = []
        self.fail_chunk_once = fail_chunk_once
        self.init_form = None
        self.completed = False
        self.size = 0

    def __call__(self, req: httpx.Request):
        path = req.url.path
        self.calls.append(f"{req.method} {path}")
        if path.endswith("/upload/init"):
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            self.init_form = form
            self.size = int(form["size"])
            cs = int(form["chunk_size"])
            return httpx.Response(200, json={"upload_id": "u1", "chunk_size": cs,
                                             "total_chunks": -(-self.size // cs), "size": self.size, "received": []})
        if path.endswith("/upload/chunk"):
            index = int(req.url.params["index"])
            assert req.url.params["token"] == "rw.tok" and req.url.params["upload_id"] == "u1"
            if self.fail_chunk_once == index:
                self.fail_times -= 1
                if self.fail_times <= 0:
                    self.fail_chunk_once = None
                return httpx.Response(500, json={"detail": "disk hiccup"})
            self.chunks[index] = req.content
            return httpx.Response(200, json={"index": index})
        if path.endswith("/upload/status"):
            return httpx.Response(200, json={"received": sorted(self.chunks)})
        if path.endswith("/upload/complete"):
            assert len(b"".join(self.chunks[i] for i in sorted(self.chunks))) == self.size
            self.completed = True
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/api/service/upload"):
            self.completed = True
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


async def test_chunked_upload_lands_at_the_exact_path(tmp_path):
    data = bytes(range(256)) * 40  # 10240 bytes
    local = tmp_path / "out.mp4"
    local.write_bytes(data)
    server = FakeChunkServer(fail_chunk_once=1)
    d = drive(server, chunk_size=4096, chunk_threshold=1000)
    await d.upload(BASE, "Package Storage/Shows/2026-10-21/InFocus Show 2026-10-21-x.mp4", "rw.tok", local)
    assert server.completed
    assert server.init_form["path"].startswith("Package+Storage%2FShows%2F2026-10-21%2FInFocus+Show")
    assert b"".join(server.chunks[i] for i in range(3)) == data


async def test_small_file_uses_simple_upload(tmp_path):
    local = tmp_path / "p.jpg"
    local.write_bytes(b"jpegdata")
    server = FakeChunkServer()
    await drive(server).upload(BASE, "Package Storage/Shows/d/p.poster.jpg", "rw.tok", local)
    assert server.calls == ["POST /api/service/upload"]


async def test_upload_auth_error_is_not_retried(tmp_path):
    local = tmp_path / "p.jpg"
    local.write_bytes(b"x")
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"detail": "File token expired"})

    with pytest.raises(TransferError) as err:
        await drive(handler).upload(BASE, "Package Storage/x.jpg", "t", local)
    assert "expired" in str(err.value) and len(calls) == 1


async def test_upload_resumes_from_status_after_retries_run_out(tmp_path):
    data = b"z" * 10240
    local = tmp_path / "out.mp4"
    local.write_bytes(data)
    server = FakeChunkServer(fail_chunk_once=1, fail_times=4)
    await drive(server, chunk_size=4096, chunk_threshold=1000).upload(BASE, "Package Storage/x.mp4", "rw.tok", local)
    assert "GET /api/service/upload/status" in server.calls
    assert server.completed
    assert server.calls.count("PUT /api/service/upload/chunk") == 1 + 4 + 1 + 1  # chunk0, 4 failures, chunk1, chunk2
