"""Drive (NAS) file transfer with Portal-minted, path-scoped file tokens.

Downloads resume with `Range`. Uploads follow Drive's chunked service upload
(infocus-drive app/main.py: /api/service/upload/init|chunk|status|complete),
the same protocol the Portal's browser client uses; small files go in one
multipart POST to /api/service/upload.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

CHUNK_THRESHOLD = 8 * 1024 * 1024
CHUNK_SIZE = 16 * 1024 * 1024
RETRIES = 3
READ_BLOCK = 1024 * 1024

Sleep = Callable[[float], Awaitable[None]]


class TransferError(Exception):
    pass


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class DriveClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, sleep: Sleep = _default_sleep,
                 chunk_size: int = CHUNK_SIZE, chunk_threshold: int = CHUNK_THRESHOLD):
        self._transport = transport
        self._sleep = sleep
        self._chunk_size = chunk_size
        self._threshold = chunk_threshold

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(60.0, read=600.0, write=600.0)
        return httpx.AsyncClient(transport=self._transport, timeout=timeout, follow_redirects=True)

    # ---- download ---------------------------------------------------------

    async def download(self, url: str, dest: Path, expected_size: int | None = None) -> Path:
        """Stream `url` to `dest`, resuming a `.part` file; verify the size."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last: Exception | None = None
        async with self._client() as client:
            for attempt in range(RETRIES + 1):
                if attempt:
                    await self._sleep(2.0 * attempt)
                try:
                    total = await self._download_once(client, url, part)
                except (httpx.TransportError, TransferError) as e:
                    last = e
                    continue
                want = expected_size if expected_size is not None else total
                got = part.stat().st_size
                if want is not None and got != want:
                    part.unlink(missing_ok=True)
                    last = TransferError(f"Downloaded {got} bytes of {dest.name}, expected {want}")
                    continue
                part.replace(dest)
                return dest
        raise TransferError(f"Could not download {dest.name}: {last}")

    async def _download_once(self, client: httpx.AsyncClient, url: str, part: Path) -> int | None:
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        async with client.stream("GET", url, headers=headers) as res:
            if res.status_code == 416 and have:
                return have  # already complete
            if res.status_code == 401 or res.status_code == 403:
                raise TransferError(f"Drive refused the download ({res.status_code}); the link may have expired")
            if res.status_code not in (200, 206):
                raise TransferError(f"Drive returned {res.status_code}")
            resumed = res.status_code == 206
            total = _total_size(res, have if resumed else 0)
            with part.open("ab" if resumed else "wb") as fh:
                async for block in res.aiter_bytes(READ_BLOCK):
                    fh.write(block)
            return total

    # ---- upload -----------------------------------------------------------

    async def upload(self, base_url: str, remote_path: str, token: str, local: Path) -> None:
        size = local.stat().st_size
        if size == 0:
            raise TransferError(f"{local.name} is empty")
        async with self._client() as client:
            if size < self._threshold:
                await self._retry(lambda: self._upload_simple(client, base_url, remote_path, token, local))
            else:
                await self._upload_chunked(client, base_url, remote_path, token, local, size)

    async def _upload_simple(self, client, base_url, remote_path, token, local: Path) -> None:
        files = {"file": (Path(remote_path).name, local.read_bytes(), "application/octet-stream")}
        res = await client.post(f"{base_url}/api/service/upload", data={"path": remote_path, "token": token},
                                files=files)
        _check(res, "upload")

    async def _upload_chunked(self, client, base_url, remote_path, token, local: Path, size: int) -> None:
        init = await self._retry(lambda: self._post_form(client, f"{base_url}/api/service/upload/init", {
            "path": remote_path, "token": token, "name": Path(remote_path).name,
            "size": str(size), "chunk_size": str(self._chunk_size),
        }))
        upload_id = str(init.get("upload_id") or "")
        total = int(init.get("total_chunks") or 0)
        chunk = int(init.get("chunk_size") or self._chunk_size)
        if not upload_id or not total:
            raise TransferError("Drive could not start a chunked upload")
        received = set(int(i) for i in init.get("received") or [])
        for round_ in range(2):
            try:
                for index in range(total):
                    if index not in received:
                        await self._retry(lambda i=index: self._put_chunk(
                            client, base_url, upload_id, token, local, i, chunk, size))
                        received.add(index)
                break
            except (httpx.TransportError, TransferError):
                if round_:
                    raise
                status = await self._retry(lambda: self._get_json(
                    client, f"{base_url}/api/service/upload/status",
                    {"upload_id": upload_id, "token": token}))
                received = set(int(i) for i in status.get("received") or [])
        await self._retry(lambda: self._post_form(client, f"{base_url}/api/service/upload/complete",
                                                  {"upload_id": upload_id, "token": token}))

    async def _put_chunk(self, client, base_url, upload_id, token, local: Path, index, chunk, size) -> None:
        start = index * chunk
        length = min(chunk, size - start)
        with local.open("rb") as fh:
            fh.seek(start)
            body = fh.read(length)
        res = await client.put(f"{base_url}/api/service/upload/chunk",
                               params={"upload_id": upload_id, "index": str(index), "token": token},
                               content=body, headers={"Content-Type": "application/octet-stream"})
        _check(res, f"chunk {index}")

    async def _post_form(self, client, url: str, form: dict[str, str]) -> dict[str, Any]:
        res = await client.post(url, data=form)
        _check(res, url.rsplit("/", 1)[-1])
        return res.json() if res.content else {}

    async def _get_json(self, client, url: str, params: dict[str, str]) -> dict[str, Any]:
        res = await client.get(url, params=params)
        _check(res, "status")
        return res.json()

    async def _retry(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            if attempt:
                await self._sleep(0.5 * attempt)
            try:
                return await fn()
            except httpx.TransportError as e:
                last = e
            except TransferError as e:
                if getattr(e, "fatal", False):
                    raise
                last = e
        assert last is not None
        raise last


def _check(res: httpx.Response, what: str) -> None:
    if res.status_code < 400:
        return
    detail = ""
    try:
        detail = str((res.json() or {}).get("detail") or "")
    except ValueError:
        pass
    err = TransferError(f"Drive {what} failed ({res.status_code}){': ' + detail if detail else ''}")
    # Auth and path errors won't fix themselves on retry.
    err.fatal = res.status_code in (400, 401, 403, 404, 413)  # type: ignore[attr-defined]
    raise err


def _total_size(res: httpx.Response, offset: int) -> int | None:
    rng = res.headers.get("content-range")
    if rng and "/" in rng:
        tail = rng.rsplit("/", 1)[1]
        if tail.isdigit():
            return int(tail)
    length = res.headers.get("content-length")
    return offset + int(length) if length and length.isdigit() else None
