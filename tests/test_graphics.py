import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from editor.graphics import (VERSION_FILE, GraphicsError, GraphicsRelease, graphics_dir, install, installed_version,
                             recover, sync_graphics)
from editor.portal import PortalClient

V1, V2 = "1" * 40, "2" * 40
LOCK = b'{"lockfileVersion": 3}'


def make_tarball(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


def good_files(html: bytes = b"<html>new</html>", lock: bytes = LOCK) -> dict[str, bytes]:
    return {"graphics.html": html, "render.mjs": b"export {}", "package.json": b"{}", "package-lock.json": lock,
            "assets/icon.png": b"png"}


def installed(tmp_path: Path) -> Path:
    gfx = graphics_dir(tmp_path / "package")
    (gfx / "node_modules" / "playwright-core").mkdir(parents=True)
    (gfx / "node_modules" / "playwright-core" / "package.json").write_text('{"version": "1.0.0"}')
    (gfx / "graphics.html").write_text("<html>old</html>")
    (gfx / "render.mjs").write_text("export {}")
    (gfx / "package-lock.json").write_bytes(LOCK)
    return gfx


class Run:
    """Records commands; `cp` fails (so the copytree fallback runs) unless told otherwise."""

    def __init__(self, returncode=1):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, "", "npm ERR! boom" if self.returncode else "")


def release(tarball: Path | None = None, version: str = V2, sha256: str | None = None) -> GraphicsRelease:
    size = tarball.stat().st_size if tarball else None
    return GraphicsRelease(version, "https://signed.example/t.tar.gz", size, sha256)


def test_install_swaps_in_the_new_files_and_keeps_node_modules(tmp_path):
    gfx = installed(tmp_path)
    tarball = make_tarball(tmp_path / "t.tar.gz", good_files())
    install(tarball, gfx, release(tarball, sha256=hashlib.sha256(tarball.read_bytes()).hexdigest()), Run())
    assert (gfx / "graphics.html").read_text() == "<html>new</html>"
    assert (gfx / "assets" / "icon.png").read_bytes() == b"png"
    assert (gfx / "node_modules" / "playwright-core" / "package.json").is_file()
    assert installed_version(gfx) == V2
    assert sorted(p.name for p in gfx.parent.iterdir()) == ["Graphics Source"]


def test_install_runs_npm_ci_when_the_lockfile_changed(tmp_path):
    gfx = installed(tmp_path)
    tarball = make_tarball(tmp_path / "t.tar.gz", good_files(lock=b'{"lockfileVersion": 3, "x": 1}'))
    run = Run(returncode=0)
    install(tarball, gfx, release(tarball), run)
    assert run.calls == [["npm", "ci", "--prefix", str(gfx.with_name("Graphics Source.new"))]]
    assert installed_version(gfx) == V2


@pytest.mark.parametrize("files, sha, why", [
    (good_files(), "0" * 64, "checksum"),
    ({"graphics.html": b"x"}, None, "render.mjs"),
    (good_files(lock=b"changed"), None, "npm failed"),
])
def test_a_bad_update_leaves_the_installed_copy(tmp_path, files, sha, why):
    gfx = installed(tmp_path)
    tarball = make_tarball(tmp_path / "t.tar.gz", files)
    with pytest.raises(GraphicsError, match=why):
        install(tarball, gfx, release(tarball, sha256=sha), Run(returncode=1))
    assert (gfx / "graphics.html").read_text() == "<html>old</html>"
    assert installed_version(gfx) is None
    assert sorted(p.name for p in gfx.parent.iterdir()) == ["Graphics Source"]


def test_install_refuses_paths_outside_the_folder(tmp_path):
    gfx = installed(tmp_path)
    tarball = tmp_path / "evil.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        info = tarfile.TarInfo("../../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        install(tarball, gfx, release(tarball), Run())
    assert not (tmp_path / "escape.txt").exists()
    assert (gfx / "graphics.html").read_text() == "<html>old</html>"


def test_recover_puts_back_a_copy_left_mid_swap(tmp_path):
    gfx = installed(tmp_path)
    gfx.rename(gfx.with_name("Graphics Source.old"))
    recover(gfx)
    assert (gfx / "graphics.html").is_file()


class Portal:
    def __init__(self, rel):
        self.rel = rel

    async def graphics(self):
        if isinstance(self.rel, Exception):
            raise self.rel
        return self.rel


class Drive:
    def __init__(self, tarball: Path | None = None):
        self.tarball = tarball
        self.downloads = []

    async def download(self, url, dest, size):
        self.downloads.append(url)
        dest.write_bytes(self.tarball.read_bytes())
        return dest


async def test_sync_updates_once_then_is_up_to_date(tmp_path):
    gfx = installed(tmp_path)
    tarball = make_tarball(tmp_path / "t.tar.gz", good_files())
    drive = Drive(tarball)
    portal = Portal(release(tarball))
    assert "updated to 222222222222" in await sync_graphics(portal, drive, tmp_path / "package", run=Run())
    assert "up to date" in await sync_graphics(portal, drive, tmp_path / "package", run=Run())
    assert len(drive.downloads) == 1
    assert not list(gfx.parent.glob("*.tar.gz"))


async def test_sync_never_raises(tmp_path):
    gfx = installed(tmp_path)
    assert "kept the installed copy" in await sync_graphics(Portal(None), Drive(), tmp_path / "package")
    msg = await sync_graphics(Portal(RuntimeError("Portal 500")), Drive(), tmp_path / "package")
    assert "update failed (RuntimeError: Portal 500)" in msg
    bad = make_tarball(tmp_path / "bad.tar.gz", {"graphics.html": b"x"})
    msg = await sync_graphics(Portal(release(bad)), Drive(bad), tmp_path / "package", run=Run())
    assert "update failed" in msg and (gfx / "graphics.html").read_text() == "<html>old</html>"
    assert not list(gfx.parent.glob("*.tar.gz"))


# ---- the Portal route ----------------------------------------------------------------------------------

async def no_sleep(_):
    return None


def portal_answering(response):
    return PortalClient("https://portal.example.org", "tok", sleep=no_sleep,
                        transport=httpx.MockTransport(lambda req: response))


async def test_portal_graphics_parses_a_release():
    data = {"version": V1, "url": "https://signed.example/x", "size": 99, "sha256": "a" * 64}
    rel = await portal_answering(httpx.Response(200, json={"data": data})).graphics()
    assert rel == GraphicsRelease(V1, "https://signed.example/x", 99, "a" * 64)


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"error": {"message": "Not found"}}),  # a Portal without the route
    httpx.Response(200, json={"data": {"version": None, "reason": "no token"}}),
    httpx.Response(200, json={"data": {"version": "../../etc", "url": "https://x"}}),
])
async def test_portal_graphics_none_when_there_is_nothing_to_use(response):
    assert await portal_answering(response).graphics() is None


def test_version_file_name_is_hidden():
    assert VERSION_FILE.startswith(".")
