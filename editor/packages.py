"""Keep the package's looks in step with the zips on Drive (All Resources/Graphic Files).

Once a day, between jobs, the editor asks the Portal for the package zips: "InFocus 2026 Package.zip" (the regular
look) and one per seasonal theme ("InFocus Christmas 2026 Package.zip"). A zip whose size or modified time differs
from what was installed last is downloaded, and the folders the skill reads are swapped in: the theme's whole
`Themes/<Name>/` folder, or the regular look's folders at the package root. Graphics Source (synced from GitHub,
editor/graphics.py) and `Themes/` are never touched by the regular zip. Anything going wrong keeps what's installed.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

VERSION_FILE = ".package-version"
# What the Setup app copies and the skill reads (setup-app/build.sh); the rest of a zip (MOGRTs, DaVinci titles,
# installers) is for editors working by hand.
KEEP = ("README.md", "01 Logos", "02 Fonts", "03 Intro & Outro", "04 Virtual Sets", "08 Follow InFocus",
        "09 TV Standby")
REQUIRED = ("03 Intro & Outro", "04 Virtual Sets")
SKIP_NAMES = (".DS_Store",)
_LOCK = threading.Lock()


class PackageError(Exception):
    pass


@dataclass(frozen=True)
class PackageZip:
    theme: str          # "" for the regular look
    name: str
    size: int
    modified_at: str
    url: str

    @property
    def version(self) -> str:
        return f"{self.size} {self.modified_at}"


def target_dir(package_dir: Path, theme: str) -> Path:
    return package_dir / "Themes" / theme.capitalize() if theme else package_dir


def installed_version(package_dir: Path, theme: str) -> str | None:
    try:
        return (target_dir(package_dir, theme) / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


async def sync_packages(portal: Any, drive: Any, package_dir: Path) -> str:
    """Bring every look up to date with Drive; returns what happened, for the log. Never raises."""
    try:
        await asyncio.to_thread(recover, package_dir)
        zips = await portal.packages()
    except Exception as e:  # noqa: BLE001 - the installed looks still work
        return f"packages: couldn't check Drive ({e.__class__.__name__}: {e})"[:400]
    if not zips:
        return "packages: nothing to check"
    return "packages: " + "; ".join([await _sync_one(z, drive, package_dir) for z in zips])


async def _sync_one(z: PackageZip, drive: Any, package_dir: Path) -> str:
    label = z.theme or "regular"
    if installed_version(package_dir, z.theme) == z.version:
        return f"{label} up to date"
    dest = package_dir / f".{z.name}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await drive.download(z.url, dest, z.size)
        await asyncio.to_thread(install_zip, dest, package_dir, z)
        return f"{label} updated"
    except Exception as e:  # noqa: BLE001
        return f"{label} kept ({e.__class__.__name__}: {e})"[:200]
    finally:
        dest.unlink(missing_ok=True)
        dest.with_name(dest.name + ".part").unlink(missing_ok=True)


def install_zip(zip_path: Path, package_dir: Path, z: PackageZip) -> None:
    with _LOCK:
        target = target_dir(package_dir, z.theme)
        staging = target.with_name(f".{target.name}.new")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            _extract(zip_path, staging)
            missing = [name for name in REQUIRED if not (staging / name).is_dir()]
            if missing:
                raise PackageError(f"{z.name} has no {', '.join(missing)}")
            (staging / VERSION_FILE).write_text(z.version + "\n")
            if z.theme:
                _swap_all([(staging, target)])
            else:
                _swap_all([(staging / name, target / name) for name in (*KEEP, VERSION_FILE)
                           if (staging / name).exists()])
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def recover(package_dir: Path) -> None:
    """After a swap was cut off (the Mac lost power): put back any folder that was moved aside and never
    replaced, and clear leftover backups and half-extracted copies."""
    with _LOCK:
        for parent in (package_dir, package_dir / "Themes"):
            if not parent.is_dir():
                continue
            for path in parent.iterdir():
                name = path.name
                if name.startswith(".") and name.endswith(".old"):
                    target = parent / name[1:-len(".old")]
                    if not target.exists():
                        path.rename(target)
                    else:
                        _remove(path)
                elif name.startswith(".") and name.endswith(".new"):
                    _remove(path)


def _extract(zip_path: Path, staging: Path) -> None:
    """The KEEP items from inside the zip's one top folder, into `staging`."""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            parts = PurePosixPath(info.filename).parts
            if not parts or parts[0] == "__MACOSX" or len(parts) < 2 or parts[1] not in KEEP:
                continue
            if any(p in ("..", "") or p.startswith("/") for p in parts) or info.filename.startswith("/"):
                raise PackageError(f"unsafe path in the zip: {info.filename}")
            if parts[-1] in SKIP_NAMES:
                continue
            out = staging.joinpath(*parts[1:])
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)


def _swap_all(pairs: list[tuple[Path, Path]]) -> None:
    """Put each `new` at its `target`, all or nothing: the old copies stay until every one is in place, and a
    failure part way puts every one of them back."""
    done: list[tuple[Path, Path]] = []      # (target, its old copy)
    try:
        for new, target in pairs:
            old = target.with_name(f".{target.name}.old")
            _remove(old)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.rename(old)
            done.append((target, old))
            new.rename(target)
    except BaseException:
        for target, old in reversed(done):
            if target.exists() or target.is_symlink():
                _remove(target)
            if old.exists() or old.is_symlink():
                old.rename(target)
        raise
    for _, old in done:
        _remove(old)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
