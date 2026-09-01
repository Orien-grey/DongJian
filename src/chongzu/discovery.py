"""Read-only, bounded recursive discovery for a caller-supplied source root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .registry import canonical_source_root
from .types import DiscoveredFile, DiscoveryIssue


# Windows marks junctions and other reparse points with this bit.  The value is
# also harmless on non-Windows hosts used for unit tests.
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _relative(root: Path, path: Path) -> str:
    # POSIX separators make registry identities stable across command shells;
    # the absolute source root remains a native Windows path.
    return path.relative_to(root).as_posix()


def discover(source: Path | str) -> tuple[str, list[DiscoveredFile], list[DiscoveryIssue]]:
    """Discover regular files without following links or writing to *source*.

    ``os.scandir`` is used so directory handles are short lived and metadata is
    obtained without an extra path lookup.  A directory that cannot be read is
    recorded as an issue and discovery continues elsewhere.
    """

    source_root = canonical_source_root(source, require_directory=True)
    root = Path(source_root)
    discovered: list[DiscoveredFile] = []
    issues: list[DiscoveryIssue] = []
    pending: list[Path] = [root]
    visited: set[str] = set()

    while pending:
        directory = pending.pop()
        try:
            directory_key = str(directory.resolve()).casefold()
        except OSError:
            directory_key = str(directory).casefold()
        if directory_key in visited:
            continue
        visited.add(directory_key)

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        # Links and junctions are deliberately not traversed.
                        if entry.is_symlink() or _is_reparse_point(entry):
                            issues.append(DiscoveryIssue(str(path), "symlink_skipped", "symbolic link or reparse point not followed", False))
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                            continue
                        if entry.is_file(follow_symlinks=False):
                            rel = _relative(root, path)
                            discovered.append(
                                DiscoveredFile(
                                    path=path,
                                    relative_path=rel,
                                    filename=path.name,
                                    observed_extension=path.suffix.lower(),
                                )
                            )
                    except OSError as exc:
                        issues.append(DiscoveryIssue(str(path), "entry_stat_error", str(exc), True))
        except OSError as exc:
            issues.append(DiscoveryIssue(str(directory), "directory_error", str(exc), True))

    discovered.sort(key=lambda item: item.relative_path.casefold())
    return source_root, discovered, issues


def issue_prefixes(issues: Iterable[DiscoveryIssue]) -> tuple[Path, ...]:
    """Return directory prefixes for which missing inference is unsafe."""

    prefixes: list[Path] = []
    for issue in issues:
        if issue.is_error and issue.code == "directory_error":
            prefixes.append(Path(issue.path))
    return tuple(prefixes)


def is_under_issue(relative_path: str, source_root: str, prefixes: Iterable[Path]) -> bool:
    """Whether a historical relative path lies under an unreadable directory."""

    absolute = Path(source_root, *relative_path.split("/"))
    for prefix in prefixes:
        try:
            absolute.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False
