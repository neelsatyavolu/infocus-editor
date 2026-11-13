import zipfile
from pathlib import Path

import httpx
import pytest

from editor import packages
from editor.packages import VERSION_FILE, PackageZip, install_zip, recover, sync_packages, target_dir
from editor.portal import PortalClient

PARTS = ("01 Logos/logo.png", "02 Fonts/Lexend.ttf", "03 Intro & Outro/intro.mp4", "04 Virtual Sets/set.png",
         "05 Lower Thirds/skip.mogrt", "08 Follow InFocus/follow.mov", "09 TV Standby/standby.png", "README.md")


def make_zip(path: Path, top: str, body: bytes = b"new", extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for part in PARTS:
            zf.writestr(f"{top}/{part}", body)
        zf.writestr(f"__MACOSX/{top}/._README.md", b"junk")
        zf.writestr(f"{top}/.DS_Store", b"junk")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


def zip_entry(theme: str, size: int = 10, modified: str = "2026-11-13T19:00:00+00:00") -> PackageZip:
    name = f"InFocus {theme.capitalize() + ' ' if theme else ''}2026 Package.zip"
    return PackageZip(theme=theme, name=name, size=size, modified_at=modified, url=f"https://drive.example/{theme}")


class FakePortal:
    def __init__(self, zips):
        self.zips = zips

    async def packages(self):
        if isinstance(self.zips, Exception):
            raise self.zips
        return self.zips


class FakeDrive:
    """Writes a zip for whichever theme the URL names."""

    def __init__(self, body: bytes = b"new"):
        self.body = body
        self.urls: list[str] = []

    async def download(self, url, dest, expected_size=None):
        self.urls.append(url)
        theme = url.rsplit("/", 1)[1]
        top = f"InFocus {theme.capitalize() + ' ' if theme else ''}2026 Package"
        return make_zip(dest, top, self.body)


def test_target_dirs(tmp_path):
    assert target_dir(tmp_path, "") == tmp_path
    assert target_dir(tmp_path, "christmas") == tmp_path / "Themes" / "Christmas"


def test_a_theme_zip_installs_only_what_the_skill_reads(tmp_path):
    pkg = tmp_path / "package"
    zf = make_zip(tmp_path / "c.zip", "InFocus Christmas 2026 Package")
    install_zip(zf, pkg, zip_entry("christmas"))
    theme = pkg / "Themes" / "Christmas"
    assert (theme / "03 Intro & Outro" / "intro.mp4").read_bytes() == b"new"
    assert (theme / "04 Virtual Sets" / "set.png").is_file()
    assert (theme / "README.md").is_file()
    assert not (theme / "05 Lower Thirds").exists()
    assert not (theme / ".DS_Store").exists()
    assert (theme / VERSION_FILE).read_text().strip() == "10 2026-11-13T19:00:00+00:00"


def test_a_theme_is_replaced_whole(tmp_path):
    pkg = tmp_path / "package"
    stale = pkg / "Themes" / "Christmas" / "03 Intro & Outro" / "old.mp4"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    install_zip(make_zip(tmp_path / "c.zip", "InFocus Christmas 2026 Package"), pkg, zip_entry("christmas"))
    assert not stale.exists()
    assert not list((pkg / "Themes").glob("*.new")) and not list((pkg / "Themes").glob("*.old"))


def test_the_regular_zip_replaces_its_folders_and_keeps_the_rest(tmp_path):
    pkg = tmp_path / "package"
    (pkg / "_Working Files" / "Graphics Source").mkdir(parents=True)
    (pkg / "_Working Files" / "Graphics Source" / "graphics.html").write_text("synced from GitHub")
    (pkg / "Themes" / "Halloween").mkdir(parents=True)
    (pkg / "03 Intro & Outro").mkdir()
    (pkg / "03 Intro & Outro" / "old.mp4").write_bytes(b"old")
    install_zip(make_zip(tmp_path / "r.zip", "InFocus 2026 Package"), pkg, zip_entry(""))
    assert (pkg / "03 Intro & Outro" / "intro.mp4").read_bytes() == b"new"
    assert not (pkg / "03 Intro & Outro" / "old.mp4").exists()
    assert (pkg / "_Working Files" / "Graphics Source" / "graphics.html").read_text() == "synced from GitHub"
    assert (pkg / "Themes" / "Halloween").is_dir()
    assert not (pkg / "05 Lower Thirds").exists()
    assert (pkg / VERSION_FILE).is_file()


def test_a_zip_without_the_intro_or_sets_is_refused(tmp_path):
    pkg = tmp_path / "package"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("InFocus Christmas 2026 Package/README.md", b"x")
    with pytest.raises(Exception, match="03 Intro & Outro"):
        install_zip(bad, pkg, zip_entry("christmas"))
    assert not (pkg / "Themes" / "Christmas").exists()


def test_paths_outside_the_package_are_refused(tmp_path):
    pkg = tmp_path / "package"
    zf = make_zip(tmp_path / "evil.zip", "InFocus Christmas 2026 Package",
                  extra={"InFocus Christmas 2026 Package/03 Intro & Outro/../../../escape.txt": b"x"})
    with pytest.raises(Exception, match="unsafe"):
        install_zip(zf, pkg, zip_entry("christmas"))
    assert not (tmp_path / "escape.txt").exists()


def test_recover_puts_back_a_folder_cut_off_mid_swap(tmp_path):
    pkg = tmp_path / "package"
    (pkg / ".03 Intro & Outro.old").mkdir(parents=True)          # renamed away, new one never arrived
    (pkg / ".04 Virtual Sets.old").mkdir()                        # swap finished, backup left behind
    (pkg / "04 Virtual Sets").mkdir()
    (pkg / "Themes" / ".Christmas.old").mkdir(parents=True)
    (pkg / "Themes" / ".Christmas.new").mkdir()
    recover(pkg)
    assert (pkg / "03 Intro & Outro").is_dir() and not (pkg / ".03 Intro & Outro.old").exists()
    assert (pkg / "04 Virtual Sets").is_dir() and not (pkg / ".04 Virtual Sets.old").exists()
    assert (pkg / "Themes" / "Christmas").is_dir()
    assert not (pkg / "Themes" / ".Christmas.old").exists() and not (pkg / "Themes" / ".Christmas.new").exists()


def test_a_failed_regular_update_rolls_every_folder_back(tmp_path, monkeypatch):
    pkg = tmp_path / "package"
    for name in ("03 Intro & Outro", "04 Virtual Sets", "09 TV Standby"):
        (pkg / name).mkdir(parents=True)
        (pkg / name / "old.txt").write_text("old")
    real_rename = Path.rename

    def rename(self, target):
        if Path(target).name == "09 TV Standby" and ".new" in str(self):
            raise PermissionError("locked")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(PermissionError):
        install_zip(make_zip(tmp_path / "r.zip", "InFocus 2026 Package"), pkg, zip_entry(""))
    for name in ("03 Intro & Outro", "04 Virtual Sets", "09 TV Standby"):
        assert (pkg / name / "old.txt").read_text() == "old", name
    assert not (pkg / VERSION_FILE).exists()
    assert not [p for p in pkg.iterdir() if p.name.endswith((".old", ".new"))]


async def test_sync_downloads_only_changed_zips(tmp_path):
    pkg = tmp_path / "package"
    install_zip(make_zip(tmp_path / "h.zip", "InFocus Halloween 2026 Package"), pkg, zip_entry("halloween"))
    drive = FakeDrive()
    portal = FakePortal([zip_entry("halloween"), zip_entry("christmas")])
    report = await sync_packages(portal, drive, pkg)
    assert drive.urls == ["https://drive.example/christmas"]
    assert "christmas updated" in report and "halloween up to date" in report
    assert (pkg / "Themes" / "Christmas" / "04 Virtual Sets" / "set.png").is_file()
    assert not list(pkg.glob("*.zip*"))

    drive.urls.clear()
    portal.zips = [zip_entry("christmas", size=11)]   # re-uploaded
    await sync_packages(portal, drive, pkg)
    assert drive.urls == ["https://drive.example/christmas"]


async def test_sync_never_raises(tmp_path):
    report = await sync_packages(FakePortal(RuntimeError("Portal down")), FakeDrive(), tmp_path / "package")
    assert "Portal down" in report
    assert "nothing to check" in await sync_packages(FakePortal(None), FakeDrive(), tmp_path / "package")


async def test_one_bad_zip_does_not_stop_the_others(tmp_path):
    pkg = tmp_path / "package"

    class HalfBrokenDrive(FakeDrive):
        async def download(self, url, dest, expected_size=None):
            if url.endswith("halloween"):
                raise RuntimeError("connection reset")
            return await super().download(url, dest, expected_size)

    report = await sync_packages(FakePortal([zip_entry("halloween"), zip_entry("christmas")]), HalfBrokenDrive(), pkg)
    assert "halloween kept" in report and "christmas updated" in report
    assert (pkg / "Themes" / "Christmas").is_dir()


# ---- the Portal route ----------------------------------------------------------------------------------

async def no_sleep(_):
    return None


def portal_answering(response):
    return PortalClient("https://portal.example.org", "tok", sleep=no_sleep,
                        transport=httpx.MockTransport(lambda req: response))


async def test_portal_packages_parses_the_zips():
    data = {"zips": [
        {"theme": "christmas", "name": "InFocus Christmas 2026 Package.zip", "size": 5,
         "modifiedAt": "2026-11-13T19:00:00+00:00", "url": "https://drive.example/c"},
        {"theme": "../evil", "name": "x.zip", "size": 1, "modifiedAt": "t", "url": "https://x"},
        {"theme": "halloween", "name": "InFocus Halloween 2026 Package.zip", "size": "big", "modifiedAt": "t",
         "url": "https://x"},
    ]}
    zips = await portal_answering(httpx.Response(200, json={"data": data})).packages()
    assert zips == [PackageZip("christmas", "InFocus Christmas 2026 Package.zip", 5, "2026-11-13T19:00:00+00:00",
                               "https://drive.example/c")]


async def test_portal_packages_none_without_the_route():
    response = httpx.Response(404, json={"error": {"message": "Not found"}})
    assert await portal_answering(response).packages() is None
