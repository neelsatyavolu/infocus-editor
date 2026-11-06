"""Keep the package's Graphics Source at the newest `main` of the infocus-graphics repo.

Before each job the editor asks the Portal for the newest Graphics Source tarball (built by that repo's
graphics-source workflow, one per commit). A new one is downloaded, checked, given this Mac's node_modules
(or a fresh `npm ci` when the lockfile changed) and swapped in whole. Anything going wrong leaves the copy
that's there and the job carries on with it: a graphics update never fails an edit.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

VERSION_FILE = ".graphics-version"
REQUIRED = ("graphics.html", "render.mjs")
NPM_TIMEOUT_S = 10 * 60

Run = Callable[..., subprocess.CompletedProcess]


class GraphicsError(Exception):
    pass


@dataclass(frozen=True)
class GraphicsRelease:
    version: str
    url: str
    size: int | None
    sha256: str | None


def graphics_dir(package_dir: Path) -> Path:
    return package_dir / "_Working Files" / "Graphics Source"


def installed_version(gfx: Path) -> str | None:
    try:
        return (gfx / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


async def sync_graphics(portal: Any, drive: Any, package_dir: Path, *, run: Run = subprocess.run) -> str:
    """Bring Graphics Source up to date; returns what happened, for the job log. Never raises."""
    gfx = graphics_dir(package_dir)
    try:
        await asyncio.to_thread(recover, gfx)
        release = await portal.graphics()
        if release is None:
            return f"graphics: kept the installed copy ({installed_version(gfx) or 'version unknown'})"
        if release.version == installed_version(gfx):
            return f"graphics: up to date ({release.version[:12]})"
        tarball = gfx.with_name(f"graphics-source-{release.version}.tar.gz")
        try:
            await drive.download(release.url, tarball, release.size)
            await asyncio.to_thread(install, tarball, gfx, release, run)
        finally:
            tarball.unlink(missing_ok=True)
        return f"graphics: updated to {release.version[:12]}"
    except Exception as e:  # noqa: BLE001 - the installed graphics still work
        return f"graphics: kept the installed copy, the update failed ({e.__class__.__name__}: {e})"[:400]


def recover(gfx: Path) -> None:
    """Put the previous copy back if a swap was cut off between its two renames."""
    old = gfx.with_name(gfx.name + ".old")
    if not gfx.exists() and old.is_dir():
        old.rename(gfx)


def install(tarball: Path, gfx: Path, release: GraphicsRelease, run: Run = subprocess.run) -> None:
    if release.sha256 and _sha256(tarball) != release.sha256.lower():
        raise GraphicsError("the download doesn't match its checksum")
    new = gfx.with_name(gfx.name + ".new")
    old = gfx.with_name(gfx.name + ".old")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    try:
        with tarfile.open(tarball) as tar:
            tar.extractall(new, filter="data")
        missing = [name for name in REQUIRED if not (new / name).is_file()]
        if missing:
            raise GraphicsError(f"the tarball has no {', '.join(missing)}")
        _node_modules(gfx, new, run)
        (new / VERSION_FILE).write_text(release.version + "\n")
        shutil.rmtree(old, ignore_errors=True)
        if gfx.exists():
            gfx.rename(old)
        new.rename(gfx)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


def _node_modules(gfx: Path, new: Path, run: Run) -> None:
    """Reuse this Mac's node_modules when the lockfile is unchanged (an APFS clone, so instant), else npm ci."""
    lock = "package-lock.json"
    same_lock = (gfx / lock).is_file() and (new / lock).is_file() and (gfx / lock).read_bytes() == (new / lock).read_bytes()
    if same_lock and (gfx / "node_modules").is_dir():
        proc = run(["cp", "-cR", str(gfx / "node_modules"), str(new / "node_modules")], capture_output=True, text=True,
                   check=False)
        if proc.returncode == 0:
            return
        shutil.rmtree(new / "node_modules", ignore_errors=True)
        shutil.copytree(gfx / "node_modules", new / "node_modules", symlinks=True)
        return
    if not (new / "package.json").is_file():
        return
    cmd = ["npm", "ci" if (new / lock).is_file() else "install", "--prefix", str(new)]
    proc = run(cmd, capture_output=True, text=True, check=False, timeout=NPM_TIMEOUT_S)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise GraphicsError(f"npm failed ({tail[0][:150]})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
