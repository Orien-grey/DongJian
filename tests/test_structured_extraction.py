from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import duckdb
import polars as pl

from chongzu.extract.artifacts import artifact_absolute
from chongzu.extract.structured import MAX_STRUCTURED_WORKERS, extract_structured, normalize_workers
from chongzu.registry import Registry, canonical_source_root

from .xlsx_factory import write_xlsx


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _extract(tmp_path: Path, source: Path, **kwargs):
    return extract_structured(
        source,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
        **kwargs,
    )


def _current_tables(tmp_path: Path) -> list[dict[str, object]]:
    registry = Registry.open(_registry(tmp_path))
    try:
        cursor = registry.connection.execute("SELECT * FROM table_assets WHERE is_current=TRUE ORDER BY source_relative_path, table_id")
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        registry.close()


def test_csv_tsv_bom_quotes_multiline_duplicate_empty_and_corrupt_isolation(tmp_path: Path) -> None:
    source = tmp_path / "中文 source with spaces"
    source.mkdir()
    (source / "normal.csv").write_text('name,value\n北京,12\n"上海,市",8\n', encoding="utf-8")
    (source / "tabs.tsv").write_text('name\tnote\na\t"line 1\nline 2"\n', encoding="utf-8")
    (source / "bom.csv").write_text("甲,乙\n1,2\n", encoding="utf-8-sig")
    (source / "duplicate.csv").write_text("name,name\na,b\n", encoding="utf-8")
    (source / "header-only.csv").write_text("a,b\n", encoding="utf-8")
    (source / "gb18030.csv").write_bytes("城市,数值\n北京,12\n".encode("gb18030"))
    (source / "empty.csv").write_bytes(b"")
    (source / "ragged.csv").write_text("a,b\n1,2,3\n", encoding="utf-8")
    hashes_before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}

    summary = _extract(tmp_path, source, workers=2)
    assert summary.structured_supported == 8
    assert summary.extracted == 7
    assert summary.failed == 1
    assert summary.tables_produced == 6
    assert summary.total_rows == 6
    assert summary.quality_issues >= 2
    assert hashes_before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}

    tables = _current_tables(tmp_path)
    assert {row["source_relative_path"] for row in tables} == {
        "bom.csv", "duplicate.csv", "gb18030.csv", "header-only.csv", "normal.csv", "tabs.tsv"
    }
    duplicate = next(row for row in tables if row["source_relative_path"] == "duplicate.csv")
    assert json.loads(duplicate["columns_json"]) == ["name", "name__2"]
    normalized = pl.read_parquet(artifact_absolute(duplicate["normalized_artifact_path"], _workspace(tmp_path)))
    assert normalized.columns == ["name", "name__2"]
    registry = Registry.open(_registry(tmp_path))
    try:
        failed = registry.connection.execute(
            "SELECT error_category FROM extraction_runs WHERE source_relative_path='ragged.csv' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert failed == ("malformed_delimited_file",)
        issue_types = {row[0] for row in registry.connection.execute("SELECT issue_type FROM quality_issues").fetchall()}
        assert {"duplicate_column_names", "empty_file"} <= issue_types
    finally:
        registry.close()


def test_xlsx_multisheet_unicode_empty_regions_types_and_provenance(tmp_path: Path) -> None:
    source = tmp_path / "工作簿 source"
    source.mkdir()
    workbook = source / "研究 数据.xlsx"
    write_xlsx(
        workbook,
        [
            ("数据 表", [[None, None, None], [None, "地区", "值"], [None, "北京", 12], [None, "上海", 8]], None),
            ("两个区域", [["a", "b"], [1, 2], [None, None], ["c", "d"], [3, 4]], None),
            ("标题注释", [["年度统计", None], ["地区", "值"], ["北京", 12], ["注：截至年末", None]], "hidden"),
            ("类型", [["number", "date", "datetime", "bool", "null"], [1.5, date(2025, 1, 2), datetime(2025, 1, 2, 3, 4), True, None]], None),
            ("空表", [], None),
        ],
    )
    before = hashlib.sha256(workbook.read_bytes()).hexdigest()
    summary = _extract(tmp_path, source, workers=1)
    assert summary.failed == 0
    assert summary.extracted == 1
    assert summary.sheets == 5
    assert summary.tables_produced == 5
    assert summary.quality_issues >= 4
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == before

    tables = _current_tables(tmp_path)
    assert {row["sheet_name"] for row in tables} >= {"数据 表", "两个区域", "标题注释", "类型"}
    region = next(row for row in tables if row["sheet_name"] == "数据 表")
    assert (region["source_row_start"], region["source_row_end"]) == (1, 4)
    assert (region["source_column_start"], region["source_column_end"]) == (1, 3)
    metadata_path = artifact_absolute(region["metadata_artifact_path"], _workspace(tmp_path))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["content_sha256"] == before
    assert metadata["source_relative_path"] == "研究 数据.xlsx"
    assert metadata["full_sheet_raw_artifact"]
    assert metadata["normalized_row_mapping"]["parquet_row_zero_maps_to_source_row"] == 2
    assert artifact_absolute(region["raw_artifact_path"], _workspace(tmp_path)).read_bytes() != artifact_absolute(
        region["normalized_artifact_path"], _workspace(tmp_path)
    ).read_bytes()
    frame = pl.read_parquet(artifact_absolute(region["normalized_artifact_path"], _workspace(tmp_path)))
    assert frame.to_dicts() == [{"地区": "北京", "值": 12}, {"地区": "上海", "值": 8}]


def test_native_xls_fixture_and_corrupt_workbook_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "xls"
    source.mkdir()
    shutil.copy2(Path(__file__).parent / "fixtures" / "python-calamine-base.xls", source / "base.xls")
    (source / "corrupt.xlsx").write_bytes(b"PK\x03\x04not-a-workbook")
    summary = _extract(tmp_path, source, workers=2)
    assert summary.structured_supported == 2
    assert summary.extracted == 1
    assert summary.failed == 1
    assert summary.tables_produced >= 1
    assert any(row["source_relative_path"] == "base.xls" for row in _current_tables(tmp_path))


def test_incremental_reuse_change_force_and_catalog_runs(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    csv_path = source / "data.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
    first = _extract(tmp_path, source)
    assert (first.extracted, first.reused) == (1, 0)
    second = _extract(tmp_path, source)
    assert (second.extracted, second.reused) == (0, 1)
    forced = _extract(tmp_path, source, force=True)
    assert (forced.extracted, forced.reused) == (1, 0)

    csv_path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    changed = _extract(tmp_path, source)
    assert (changed.extracted, changed.reused, changed.total_rows) == (1, 0, 2)

    import chongzu.extract.structured as structured

    original = structured._extractor
    monkeypatch.setattr(structured, "_extractor", lambda name: (original(name)[0], "test-version-invalidation"))
    invalidated = _extract(tmp_path, source)
    assert (invalidated.extracted, invalidated.reused) == (1, 0)

    registry = Registry.open(_registry(tmp_path))
    try:
        assert registry.schema_version() == 3
        assert registry.connection.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0] == 4
        current = registry.connection.execute("SELECT COUNT(*) FROM table_assets WHERE is_current=TRUE").fetchone()[0]
        assert current == 1
        assert registry.connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 2
        assert canonical_source_root(source) == registry.connection.execute("SELECT source_root FROM files LIMIT 1").fetchone()[0]
        catalog = registry.catalog_summary(canonical_source_root(source))
        assert catalog["table_assets"] == 1
        assert catalog["table_rows"] == 2
    finally:
        registry.close()


def test_duplicate_content_files_keep_independent_assets(tmp_path: Path) -> None:
    source = tmp_path / "duplicate content"
    source.mkdir()
    payload = "a,b\n1,2\n"
    (source / "first.csv").write_text(payload, encoding="utf-8")
    (source / "second.csv").write_text(payload, encoding="utf-8")
    summary = _extract(tmp_path, source, workers=2)
    assert summary.extracted == 2
    tables = _current_tables(tmp_path)
    assert {row["source_relative_path"] for row in tables} == {"first.csv", "second.csv"}
    assert len({row["table_id"] for row in tables}) == 2


def test_structured_worker_bound() -> None:
    assert normalize_workers(1) == 1
    assert normalize_workers(MAX_STRUCTURED_WORKERS) == MAX_STRUCTURED_WORKERS
    try:
        normalize_workers(MAX_STRUCTURED_WORKERS + 1)
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded structured workers were accepted")


def test_cli_force_is_forwarded(monkeypatch, capsys, tmp_path: Path) -> None:
    import chongzu.__main__ as cli

    captured: dict[str, object] = {}

    def fake_extract(source, *, workers, force):
        captured.update(source=source, workers=workers, force=force)
        return SimpleNamespace(
            source_root=str(source), files_considered=0, structured_supported=0,
            extracted=0, reused=0, tables_produced=0, quality_issues=0,
            failed=0, total_rows=0, total_bytes=0, wall_time_ms=0.0,
        )

    monkeypatch.setattr(cli, "extract_structured", fake_extract)
    assert cli.main(["extract", "structured", str(tmp_path), "--workers", "2", "--force"]) == 0
    assert captured["force"] is True
    assert captured["workers"] == 2
    assert "Reused: 0" in capsys.readouterr().out
