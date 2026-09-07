"""Read-only, bounded recursive discovery for a caller-supplied source root."""

from __future__ import annotations

import os
import fnmatch
from pathlib import Path
from collections.abc import Iterable, Iterator

from .registry import canonical_source_root
from .types import DiscoveredFile, DiscoveryIssue


# Windows marks junctions and other reparse points with this bit.  The value is
# also harmless on non-Windows hosts used for unit tests.
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

# These are structural markers, rather than directory-name guesses.  A source
# tree is treated as an exploded Office package only when the package manifest
# and at least one package content root are present together.
_OOXML_CONTENT_ROOTS = ("word", "xl", "ppt")
_PRODUCT_GENERATED_PATHS = (
    "workspace/artifacts",
    "workspace/state",
    "workspace/logs",
    "workspace/output",
    "workspace/quarantine",
    "workspace/staging",
    "cache",
    "runtime",
    "models",
    "application",
    "frontend",
    "src",
    "config",
    ".git",
)


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


def _is_exploded_ooxml_root(directory: Path) -> bool:
    try:
        return (directory / "[Content_Types].xml").is_file() and any(
            (directory / name).is_dir() for name in ("_rels", *_OOXML_CONTENT_ROOTS, "docProps")
        )
    except OSError:
        return False


def _is_product_root(root: Path) -> bool:
    """Recognize a DongJian checkout before excluding its owned subtrees."""

    return (
        (root / "src" / "dongjian").is_dir()
        and (root / "scripts" / "env.ps1").is_file()
    )


def _matches_exclude(relative_path: str, patterns: Iterable[str]) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")
    return any(
        fnmatch.fnmatchcase(normalized, str(pattern).replace("\\", "/").strip("/"))
        for pattern in patterns
        if str(pattern).strip()
    )


def discover_iter(
    source: Path | str,
    *,
    exclude_patterns: Iterable[str] | None = None,
) -> tuple[str, Iterator[DiscoveredFile], list[DiscoveryIssue]]:
    """Return a lazy regular-file iterator and its shared issue list.

    ``os.scandir`` is consumed directly.  The caller can consume the iterator
    in bounded batches while the directory is still being walked; no whole
    directory or whole-source candidate list is built.  The compatibility
    ``discover`` wrapper applies deterministic sorting after materialization;
    the streaming path intentionally keeps discovery order supplied by the
    filesystem so it can release the first candidate immediately.
    ``issues`` is populated as the iterator advances.
    """

    source_root = canonical_source_root(source, require_directory=True)
    root = Path(source_root)
    issues: list[DiscoveryIssue] = []
    configured_excludes = tuple(exclude_patterns or ())
    product_root = _is_product_root(root)

    def excluded(path: Path, *, directory: bool = False) -> tuple[bool, str | None]:
        relative = _relative(root, path)
        if _matches_exclude(relative, configured_excludes):
            return True, "excluded_by_pattern"
        if product_root and any(
            relative == owned or relative.startswith(owned + "/")
            for owned in _PRODUCT_GENERATED_PATHS
        ):
            return True, "product_internal"
        if directory and _is_exploded_ooxml_root(path):
            return True, "package_internal"
        return False, None

    def iterate() -> Iterator[DiscoveredFile]:
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
                skip, reason = excluded(directory, directory=True)
                if skip:
                    issues.append(
                        DiscoveryIssue(
                            str(directory),
                            reason or "excluded",
                            "directory excluded from source discovery",
                            False,
                        )
                    )
                    continue
                with os.scandir(directory) as entries:
                    child_directories: list[Path] = []
                    for entry in entries:
                        path = Path(entry.path)
                        try:
                            # Links and junctions are deliberately not traversed.
                            if entry.is_symlink() or _is_reparse_point(entry):
                                issues.append(DiscoveryIssue(str(path), "symlink_skipped", "symbolic link or reparse point not followed", False))
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                skip, reason = excluded(path, directory=True)
                                if skip:
                                    issues.append(
                                        DiscoveryIssue(
                                            str(path),
                                            reason or "excluded",
                                            "directory excluded from source discovery",
                                            False,
                                        )
                                    )
                                    continue
                                child_directories.append(path)
                                continue
                            if entry.is_file(follow_symlinks=False):
                                skip, reason = excluded(path)
                                if skip:
                                    issues.append(
                                        DiscoveryIssue(
                                            str(path),
                                            reason or "excluded",
                                            "file excluded from source discovery",
                                            False,
                                        )
                                    )
                                    continue
                                yield DiscoveredFile(
                                    path=path,
                                    relative_path=_relative(root, path),
                                    filename=path.name,
                                    observed_extension=path.suffix.lower(),
                                )
                        except OSError as exc:
                            issues.append(DiscoveryIssue(str(path), "entry_stat_error", str(exc), True))
                    # Keep only directory identities for traversal; regular
                    # files are yielded above and never retained in a source-
                    # sized candidate list.
                    pending.extend(reversed(child_directories))
            except OSError as exc:
                issues.append(DiscoveryIssue(str(directory), "directory_error", str(exc), True))

    return source_root, iterate(), issues


def discover(
    source: Path | str,
    *,
    exclude_patterns: Iterable[str] | None = None,
) -> tuple[str, list[DiscoveredFile], list[DiscoveryIssue]]:
    """Discover regular files without following links or writing to *source*.

    ``os.scandir`` is used so directory handles are short lived and metadata is
    obtained without an extra path lookup.  A directory that cannot be read is
    recorded as an issue and discovery continues elsewhere.
    """

    source_root, iterator, issues = discover_iter(source, exclude_patterns=exclude_patterns)
    discovered = list(iterator)
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
