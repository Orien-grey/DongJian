from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import duckdb
import pytest

from chongzu.detection import detect_file
from chongzu.discovery import discover
from chongzu.fingerprint import hash_file
from chongzu.registry import Registry, RegistryError, canonical_source_root, utc_now
from chongzu.scan import MAX_WORKERS, normalize_workers, scan_source


def _registry(tmp_path: Path) -> Path:
    return tmp_path / "workspace" / "state" / "registry.duckdb"


def _scan(tmp_path: Path, source: Path, **kwargs):
    return scan_source(source, registry_path=_registry(tmp_path), **kwargs)


def test_first_scan_hashes_every_file_and_second_reuses(tmp_path: Path) -> None:
    source = tmp_path / "源 数据"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.bin").write_bytes(b"\x00\x01binary")
    first = _scan(tmp_path, source, workers=2)
    assert first.discovered_count == 2
    assert first.hashed_count == 2
    assert first.reused_hash_count == 0
    second = _scan(tmp_path, source, workers=2)
    assert second.hashed_count == 0
    assert second.reused_hash_count == 2
    assert second.unchanged_count == 2


def test_changed_new_deleted_and_duplicate_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "same one.txt").write_text("same", encoding="utf-8")
    (source / "same two.txt").write_text("same", encoding="utf-8")
    (source / "gone.txt").write_text("gone", encoding="utf-8")
    first = _scan(tmp_path, source)
    assert first.exact_duplicate_paths == 2
    (source / "same one.txt").write_text("changed", encoding="utf-8")
    (source / "new.txt").write_text("new", encoding="utf-8")
    (source / "gone.txt").unlink()
    second = _scan(tmp_path, source)
    assert second.changed_count == 1
    assert second.new_count == 1
    assert second.missing_count == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        rows = registry.list_files(canonical_source_root(source))
        states = {row["relative_path"]: row["current_presence_state"] for row in rows}
        assert states["gone.txt"] == "missing"
    finally:
        registry.close()


def test_rehash_forces_hashing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    _scan(tmp_path, source)
    forced = _scan(tmp_path, source, rehash=True)
    assert forced.hashed_count == 1
    assert forced.reused_hash_count == 0


def test_registry_content_identity_is_distinct_from_path_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("same", encoding="utf-8")
    (source / "b.txt").write_text("same", encoding="utf-8")
    _scan(tmp_path, source)
    registry = Registry.open(_registry(tmp_path))
    try:
        root = canonical_source_root(source)
        files = registry.list_files(root)
        contents = registry.connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
        assert len(files) == 2
        assert contents == 1
        assert files[0]["file_id"] != files[1]["file_id"]
        assert files[0]["sha256"] == files[1]["sha256"]
    finally:
        registry.close()


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("x.download", b"%PDF-1.7\nbody", "pdf"),
        ("image.any", b"\xff\xd8\xff\xe0jpeg", "jpeg"),
        ("image.bin", b"\x89PNG\r\n\x1a\nrest", "png"),
        ("style.css", b"body { color: red; }", "css"),
        ("page.html", b"<!doctype html><html></html>", "html"),
        ("data.xml", b"<?xml version=\"1.0\"?><root />", "xml"),
        ("Zone.Identifier", b"[ZoneTransfer]\nZoneId=3", "zone_identifier"),
    ],
)
def test_lightweight_magic_and_metadata_detection(tmp_path: Path, name: str, payload: bytes, expected: str) -> None:
    path = tmp_path / name
    path.write_bytes(payload)
    result = detect_file(path)
    assert result.detected_type == expected
    assert result.routing_class in {"document", "image", "web_asset", "structured", "metadata"}


def _write_ooxml(path: Path, member: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(member, "content")


@pytest.mark.parametrize(
    ("member", "expected"),
    [("xl/workbook.xml", "xlsx"), ("word/document.xml", "docx"), ("ppt/presentation.xml", "pptx")],
)
def test_ooxml_container_detection_and_ordinary_zip(tmp_path: Path, member: str, expected: str) -> None:
    path = tmp_path / "renamed.下载"
    _write_ooxml(path, member)
    result = detect_file(path)
    assert result.detected_type == expected
    ordinary = tmp_path / "ordinary.zip"
    with zipfile.ZipFile(ordinary, "w") as archive:
        archive.writestr("notes.txt", "not Office")
    assert detect_file(ordinary).detected_type == "zip"


def test_ole_corrupt_zip_empty_and_binary_unknown(tmp_path: Path) -> None:
    ole = tmp_path / "old.bin"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    assert detect_file(ole).detected_type == "ole_compound"
    corrupt = tmp_path / "bad.zip"
    corrupt.write_bytes(b"PK\x03\x04broken")
    result = detect_file(corrupt)
    assert result.detected_type == "zip"
    assert result.error_code == "corrupted_zip"
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert detect_file(empty).detected_type == "unknown"
    binary = tmp_path / "random.bin"
    binary.write_bytes(bytes(range(256)))
    assert detect_file(binary).detected_type == "unknown"


def test_corrupt_zip_isolated_in_a_successful_batch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "bad.zip").write_bytes(b"PK\x03\x04truncated")
    (source / "good.txt").write_text("still processed", encoding="utf-8")
    summary = _scan(tmp_path, source)
    assert summary.status == "complete"
    assert summary.discovered_count == 2
    assert summary.failed_count == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        rows = registry.list_files(canonical_source_root(source))
        bad = next(row for row in rows if row["relative_path"] == "bad.zip")
        assert bad["current_presence_state"] == "present"
        assert bad["latest_error_code"] == "corrupted_zip"
    finally:
        registry.close()


def test_source_is_not_modified_and_unicode_spaces_are_supported(tmp_path: Path) -> None:
    source = tmp_path / "含 空格"
    source.mkdir()
    path = source / "文档 文件.txt"
    path.write_text("immutable", encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    before_stat = path.stat()
    _scan(tmp_path, source)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    after_stat = path.stat()
    assert (before_stat.st_size, before_stat.st_mtime_ns) == (after_stat.st_size, after_stat.st_mtime_ns)


def test_broken_symlink_is_skipped_without_crashing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    link = source / "broken-link"
    try:
        link.symlink_to(source / "does-not-exist")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable without extra Windows privileges")
    _, files, issues = discover(source)
    assert files == []
    assert any(issue.code == "symlink_skipped" for issue in issues)


def test_worker_bound_and_schema_version(tmp_path: Path) -> None:
    assert normalize_workers(1) == 1
    assert normalize_workers(MAX_WORKERS) == MAX_WORKERS
    with pytest.raises(ValueError):
        normalize_workers(MAX_WORKERS + 1)
    registry = Registry.open(_registry(tmp_path))
    try:
        assert registry.schema_version() == 1
        tables = {row[0] for row in registry.connection.execute("SHOW TABLES").fetchall()}
        assert {"scan_runs", "files", "contents", "file_attempts", "run_errors", "registry_meta"} <= tables
    finally:
        registry.close()


def test_newer_registry_schema_is_rejected(tmp_path: Path) -> None:
    database = _registry(tmp_path)
    database.parent.mkdir(parents=True)
    connection = duckdb.connect(str(database))
    connection.execute("CREATE TABLE registry_meta (meta_key VARCHAR PRIMARY KEY, meta_value VARCHAR NOT NULL, updated_at TIMESTAMP NOT NULL)")
    connection.execute("INSERT INTO registry_meta VALUES ('chongzu_file_registry', '99', CURRENT_TIMESTAMP)")
    connection.close()
    with pytest.raises(RegistryError):
        Registry.open(database)


def test_open_run_is_marked_interrupted_for_resume(tmp_path: Path) -> None:
    database = _registry(tmp_path)
    registry = Registry.open(database)
    try:
        registry.create_run("open-run", "C:/source", utc_now(), None)
        assert registry.recover_incomplete_runs("C:/source") == 1
        row = registry.latest_run("C:/source")
        assert row is not None and row["status"] == "interrupted"
    finally:
        registry.close()


def test_changed_during_hash_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "changing.bin"
    path.write_bytes(b"a" * (2 * 1024 * 1024))
    original_stat = path.stat
    calls = {"count": 0}

    def changing_stat(_path: Path):
        calls["count"] += 1
        result = original_stat()
        if calls["count"] == 2:
            path.write_bytes(b"b" * (2 * 1024 * 1024))
            result = original_stat()
        from chongzu.types import FileStat

        return FileStat(result.st_size, result.st_mtime_ns)

    result = hash_file(path, stat_func=changing_stat)
    assert result.stable is False
    assert result.error_code == "changed_during_scan"
