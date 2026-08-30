"""Reject local or operational artefacts from release archives."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {
    ".git",
    ".mypy_cache",
    ".planning",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "fixtures",
}
FORBIDDEN_SUFFIXES = {
    ".csv",
    ".db",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_NAMES = {"export_manifest.json"}
MACHINE_PATH_MARKERS = (b"/home/user/", b"C:\\Users\\")


def archive_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield regular-file member names and content from a wheel or source archive."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    yield member.filename, archive.read(member)
        return

    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError(f"could not read archive member: {member.name}")
                    yield member.name, extracted.read()
        return

    raise ValueError(f"unsupported distribution archive: {path}")


def check_archive(path: Path) -> None:
    """Raise ValueError when an archive contains a forbidden release artefact."""
    checked = 0
    for name, content in archive_members(path):
        checked += 1
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError(f"unsafe archive path in {path.name}: {name}")
        if FORBIDDEN_PARTS.intersection(member.parts):
            raise ValueError(f"forbidden archive path in {path.name}: {name}")
        if member.name in FORBIDDEN_NAMES or member.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"forbidden release artefact in {path.name}: {name}")
        if any(marker in content for marker in MACHINE_PATH_MARKERS):
            raise ValueError(f"machine-specific path in {path.name}: {name}")
    if checked == 0:
        raise ValueError(f"empty distribution archive: {path}")
    print(f"Checked distribution: {path.name} ({checked} files)")


def main() -> int:
    """Check every distribution path supplied on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    arguments = parser.parse_args()
    for archive in arguments.archives:
        check_archive(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
