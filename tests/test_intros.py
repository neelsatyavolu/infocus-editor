"""Every look's intros from the infocus-graphics repo (editor/intros.py): download, place, clean up."""
import hashlib
import io
import tarfile
from pathlib import Path

import httpx

from editor.graphics import GraphicsRelease
from editor.intros import CACHE_DIR, sync_intros
from editor.portal import PortalClient

V1, V2 = "1" * 40, "2" * 40
CHRISTMAS = "InFocus Intro Christmas 2026 - Collage (new music).mp4"


def make_tarball(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / "03 Intro & Outro").mkdir(parents=True)
    (pkg / "03 Intro & Outro" / "InFocus Intro 2026.mp4").write_bytes(b"zip intro")
    (pkg / "Themes" / "Christmas" / "03 Intro & Outro").mkdir(parents=True)
    return pkg


class FakePortal:
    def __init__(self, release):
        self.release = release

    async def intros(self):
        return self.release


class FakeDrive:
    def __init__(self, tarball: Path):
        self.tarball = tarball
        self.downloads = []

    async def download(self, url, dest, size):
        self.downloads.append(url)
        dest.write_bytes(self.tarball.read_bytes())
        return dest


def release(version: str, tarball: Path, good_sum: bool = True) -> GraphicsRelease:
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest() if good_sum else "0" * 64
    return GraphicsRelease(version=version, url=f"https://signed.example/{version}", size=None, sha256=digest)


async def test_intros_are_placed_into_each_installed_looks_folder_once(tmp_path):
    pkg = package(tmp_path)
    tarball = make_tarball(tmp_path / "i.tar.gz", {
        "./regular/InFocus Intro 2026 - Carbon copy (old music).mp4": b"carbon",
        "./regular/InFocus Intro 2026.mp4": b"repo intro",
        f"./christmas/{CHRISTMAS}": b"collage",
        "./halloween/InFocus Intro Halloween 2026.mp4": b"spooky",   # Halloween isn't installed: skipped
        "./regular/notes.txt": b"not a video",
    })
    drive = FakeDrive(tarball)
    message = await sync_intros(FakePortal(release(V1, tarball)), drive, pkg)
    assert "updated" in message
    intro = pkg / "03 Intro & Outro"
    assert (intro / "InFocus Intro 2026 - Carbon copy (old music).mp4").read_bytes() == b"carbon"
    assert (intro / "InFocus Intro 2026.mp4").read_bytes() == b"repo intro"        # the repo's copy wins
    assert (pkg / "Themes" / "Christmas" / "03 Intro & Outro" / CHRISTMAS).read_bytes() == b"collage"
    assert not (pkg / "Themes" / "Halloween").exists()
    assert not (intro / "notes.txt").exists()
    await sync_intros(FakePortal(release(V1, tarball)), drive, pkg)
    assert len(drive.downloads) == 1                                              # same version: no download


async def test_intros_come_back_after_a_package_zip_replaced_the_folder(tmp_path):
    pkg = package(tmp_path)
    tarball = make_tarball(tmp_path / "i.tar.gz", {f"./christmas/{CHRISTMAS}": b"collage"})
    await sync_intros(FakePortal(release(V1, tarball)), FakeDrive(tarball), pkg)
    christmas = pkg / "Themes" / "Christmas" / "03 Intro & Outro"
    (christmas / CHRISTMAS).unlink()                                              # the daily zip swap
    await sync_intros(FakePortal(release(V1, tarball)), FakeDrive(tarball), pkg)
    assert (christmas / CHRISTMAS).read_bytes() == b"collage"


async def test_an_intro_dropped_from_the_repo_is_removed_but_zip_files_stay(tmp_path):
    pkg = package(tmp_path)
    first = make_tarball(tmp_path / "1.tar.gz", {"./regular/Old Intro.mp4": b"old", "./regular/Keep Intro.mp4": b"keep"})
    await sync_intros(FakePortal(release(V1, first)), FakeDrive(first), pkg)
    second = make_tarball(tmp_path / "2.tar.gz", {"./regular/Keep Intro.mp4": b"keep"})
    await sync_intros(FakePortal(release(V2, second)), FakeDrive(second), pkg)
    intro = pkg / "03 Intro & Outro"
    assert sorted(p.name for p in intro.iterdir()) == ["InFocus Intro 2026.mp4", "Keep Intro.mp4"]


async def test_dropping_an_intro_the_zip_also_has_never_deletes_the_zips_copy(tmp_path):
    pkg = package(tmp_path)
    first = make_tarball(tmp_path / "1.tar.gz", {"./regular/InFocus Intro 2026.mp4": b"repo intro"})
    await sync_intros(FakePortal(release(V1, first)), FakeDrive(first), pkg)
    second = make_tarball(tmp_path / "2.tar.gz", {"./regular/Other Intro.mp4": b"other"})
    await sync_intros(FakePortal(release(V2, second)), FakeDrive(second), pkg)
    assert (pkg / "03 Intro & Outro" / "InFocus Intro 2026.mp4").is_file()


async def test_a_bad_download_keeps_what_is_installed(tmp_path):
    pkg = package(tmp_path)
    tarball = make_tarball(tmp_path / "i.tar.gz", {"./regular/New.mp4": b"new"})
    message = await sync_intros(FakePortal(release(V1, tarball, good_sum=False)), FakeDrive(tarball), pkg)
    assert "failed" in message
    assert not (pkg / "03 Intro & Outro" / "New.mp4").exists()
    assert not (pkg / CACHE_DIR).exists()


async def test_unsafe_paths_in_the_tarball_are_refused(tmp_path):
    pkg = package(tmp_path)
    tarball = make_tarball(tmp_path / "i.tar.gz", {"../../evil/Intro.mp4": b"x"})
    message = await sync_intros(FakePortal(release(V1, tarball)), FakeDrive(tarball), pkg)
    assert "failed" in message
    assert not (tmp_path / "evil").exists()


async def test_no_release_keeps_placing_what_was_downloaded(tmp_path):
    pkg = package(tmp_path)
    assert "kept" in await sync_intros(FakePortal(None), FakeDrive(tmp_path / "none"), pkg)


async def test_portal_intros_route():
    def handler(req):
        assert req.url.path.endswith("/api/service/show-editor/intros")
        return httpx.Response(200, json={"data": {"version": V1, "url": "https://signed.example/x", "size": 5,
                                                  "sha256": None}})

    async def no_sleep(_):
        return None

    client = PortalClient("https://portal.example.org", "tok", sleep=no_sleep, transport=httpx.MockTransport(handler))
    assert await client.intros() == GraphicsRelease(version=V1, url="https://signed.example/x", size=5, sha256=None)
    missing = PortalClient("https://portal.example.org", "tok", sleep=no_sleep,
                           transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": {}})))
    assert await missing.intros() is None


async def test_a_revised_intro_of_the_same_size_still_replaces_the_old_one(tmp_path):
    pkg = package(tmp_path)
    first = make_tarball(tmp_path / "1.tar.gz", {"./regular/Collage Intro.mp4": b"aaaa"})
    await sync_intros(FakePortal(release(V1, first)), FakeDrive(first), pkg)
    second = make_tarball(tmp_path / "2.tar.gz", {"./regular/Collage Intro.mp4": b"bbbb"})
    import os
    import time
    await sync_intros(FakePortal(release(V2, second)), FakeDrive(second), pkg)
    cached = pkg / CACHE_DIR / "regular" / "Collage Intro.mp4"
    os.utime(cached, ns=(time.time_ns(), time.time_ns() + 10**9))   # the new tarball's files are newer
    await sync_intros(FakePortal(release(V2, second)), FakeDrive(second), pkg)
    assert (pkg / "03 Intro & Outro" / "Collage Intro.mp4").read_bytes() == b"bbbb"


async def test_a_zip_that_replaced_our_intro_keeps_its_file_when_the_repo_drops_it(tmp_path):
    pkg = package(tmp_path)
    first = make_tarball(tmp_path / "1.tar.gz", {"./regular/Shared Intro.mp4": b"repo"})
    await sync_intros(FakePortal(release(V1, first)), FakeDrive(first), pkg)
    (pkg / "03 Intro & Outro" / "Shared Intro.mp4").write_bytes(b"from the zip")   # the daily zip swap
    await sync_intros(FakePortal(release(V1, first)), FakeDrive(first), pkg)       # repo copy wins, not ours
    second = make_tarball(tmp_path / "2.tar.gz", {"./regular/Other Intro.mp4": b"other"})
    await sync_intros(FakePortal(release(V2, second)), FakeDrive(second), pkg)
    assert (pkg / "03 Intro & Outro" / "Shared Intro.mp4").is_file()


async def test_cached_intros_are_put_back_even_when_the_update_fails(tmp_path):
    pkg = package(tmp_path)
    tarball = make_tarball(tmp_path / "i.tar.gz", {f"./christmas/{CHRISTMAS}": b"collage"})
    await sync_intros(FakePortal(release(V1, tarball)), FakeDrive(tarball), pkg)
    christmas = pkg / "Themes" / "Christmas" / "03 Intro & Outro"
    (christmas / CHRISTMAS).unlink()                                   # a zip replaced the folder

    class DownPortal:
        async def intros(self):
            raise OSError("Portal down")

    message = await sync_intros(DownPortal(), FakeDrive(tarball), pkg)
    assert "failed" in message and (christmas / CHRISTMAS).read_bytes() == b"collage"
