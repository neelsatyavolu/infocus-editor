"""Every look's intros from the infocus-graphics repo, so a new or revised intro is just a commit there.

The repo's intros workflow publishes one tarball per commit ("<look>/<file>", look = "regular" or a theme such as
"christmas"). Once a day, right after the package zips are checked (editor/packages.py), the editor asks the Portal
for the newest one, keeps it unpacked in `_Intros/`, and copies each intro into its look's "03 Intro & Outro" folder.
That copy runs every time, so a zip that replaced a look's folder gets the repo's intros back straight away. Intros the
repo dropped are removed again; files that came only from a zip are left alone. Anything going wrong keeps what's
installed: the Portal's intro picker then shows what the Mac has.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import tarfile
import threading
from pathlib import Path
from typing import Any

from editor.graphics import GraphicsRelease
from editor.packages import INTRO_FOLDER, VIDEO_SUFFIXES, target_dir

log = logging.getLogger(__name__)

CACHE_DIR = "_Intros"
VERSION_FILE = ".intros-version"
PLACED_FILE = ".placed.json"  # what was copied into the looks' folders, so dropped intros can be removed
LOOK_RE = re.compile(r"^[a-z]+$")
_LOCK = threading.Lock()


class IntrosError(Exception):
    pass


def _installed_version(cache: Path) -> str | None:
    try:
        return (cache / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


async def sync_intros(portal: Any, drive: Any, package_dir: Path) -> str:
    """Bring the intros up to date and put them in place; returns what happened, for the log. Never raises."""
    cache = package_dir / CACHE_DIR
    try:
        release = await portal.intros()
        if release is None:
            status = "kept the installed intros"
        elif release.version == _installed_version(cache):
            status = f"up to date ({release.version[:12]})"
        else:
            tarball = package_dir / f"intros-{release.version}.tar.gz"
            try:
                await drive.download(release.url, tarball, release.size)
                await asyncio.to_thread(_install, tarball, cache, release)
            finally:
                tarball.unlink(missing_ok=True)
                tarball.with_name(tarball.name + ".part").unlink(missing_ok=True)
            status = f"updated to {release.version[:12]}"
        placed = await asyncio.to_thread(place, package_dir)
        return f"intros: {status}; {placed} in place"
    except Exception as e:  # noqa: BLE001 - the installed intros still work
        return f"intros: kept the installed intros, the update failed ({e.__class__.__name__}: {e})"[:400]


def _is_intro(name: str) -> bool:
    return not name.startswith(".") and "intro" in name.lower() and name.lower().endswith(VIDEO_SUFFIXES)


def _install(tarball: Path, cache: Path, release: GraphicsRelease) -> None:
    if release.sha256 and hashlib.sha256(tarball.read_bytes()).hexdigest() != release.sha256.lower():
        raise IntrosError("the download doesn't match its checksum")
    new = cache.with_name(cache.name + ".new")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    try:
        with tarfile.open(tarball) as tar:
            tar.extractall(new, filter="data")
        with _LOCK:
            placed = cache / PLACED_FILE
            if placed.is_file():
                shutil.copy2(placed, new / PLACED_FILE)
            (new / VERSION_FILE).write_text(release.version + "\n")
            shutil.rmtree(cache, ignore_errors=True)
            new.rename(cache)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise


def place(package_dir: Path) -> int:
    """Copy the unpacked intros into their looks' folders (only looks that are installed) and remove ones the repo
    dropped. Returns how many intros are in place."""
    cache = package_dir / CACHE_DIR
    with _LOCK:
        wanted: dict[str, Path] = {}
        for look_dir in sorted(cache.iterdir()) if cache.is_dir() else []:
            if not look_dir.is_dir() or not LOOK_RE.match(look_dir.name):
                continue
            look = target_dir(package_dir, "" if look_dir.name == "regular" else look_dir.name)
            if not look.is_dir():
                continue
            for src in sorted(look_dir.iterdir()):
                if src.is_file() and _is_intro(src.name):
                    wanted[str((look / INTRO_FOLDER / src.name).relative_to(package_dir))] = src
        manifest = cache / PLACED_FILE
        try:
            before = set(json.loads(manifest.read_text()))
        except (OSError, ValueError):
            before = set()
        for rel in before - set(wanted):
            (package_dir / rel).unlink(missing_ok=True)
        # Only files this copy created are ours to remove later; one a zip also has (its usual intro) never is.
        owned = before & set(wanted)
        for rel, src in wanted.items():
            dest = package_dir / rel
            if not dest.is_file():
                owned.add(rel)
            if not dest.is_file() or dest.stat().st_size != src.stat().st_size:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest.with_name(dest.name + ".part"))
                dest.with_name(dest.name + ".part").replace(dest)
        if cache.is_dir():
            manifest.write_text(json.dumps(sorted(owned)))
        return len(wanted)


async def sync_looks(portal: Any, drive: Any, package_dir: Path) -> str:
    """The daily refresh: the package zips from Drive first (they may replace a look's folder), then the intros."""
    from editor.packages import sync_packages

    return f"{await sync_packages(portal, drive, package_dir)}\n{await sync_intros(portal, drive, package_dir)}"
