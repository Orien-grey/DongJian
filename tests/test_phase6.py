from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import socket

import polars as pl
from PIL import Image

from chongzu.clean import process_source
from chongzu.registry import Registry

from .pdf_factory import write_pdf
from .xlsx_factory import write_xlsx
from .test_phase5b import _image


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _query(registry_path: Path, statement: str, parameters: list[object] | None = None) -> list[tuple]:
    registry = Registry.open(registry_path)
    try:
        return registry.connection.execute(statement, parameters or []).fetchall()
    finally:
        registry.close()


def test_process_table_cleaning_profiles_and_reuses_independently(tmp_path: Path) -> None:
    source = tmp_path / "中文 cleaning source with spaces"
    source.mkdir()
    table = source / "records.csv"
    table.write_text(
        " Name ,Name,ID,amount,flag,date,marker,empty\n"
        " A , A ,0012,12.5,true,2026-01-02,-,\n"
        " B ,B,0003,0,false,2026/01/03,/,\n"
        " B ,B,0003,0,false,2026/01/03,/,\n"
        ",,,,,,,\n",
        encoding="utf-8",
    )
    source_before = _sha256(table)

    first = process_source(
        source,
        workers=1,
        force=True,
        drop_exact_duplicates=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert first.files_discovered == 1
    assert first.files_supported == 1
    assert first.files_unsupported == 0
    assert first.cleaned == 1
    assert first.cleaning_failures == 0
    assert first.table_assets == 1
    assert first.text_assets == 0
    assert _sha256(table) == source_before

    asset = _query(
        _registry(tmp_path),
        "SELECT asset_id, content_sha256, raw_artifact_path, normalized_artifact_path, extraction_normalized_artifact_path, cleaning_manifest_path, profile_artifact_path, quality_status, cleaning_status FROM catalog_assets WHERE asset_type='table'",
    )[0]
    assert asset[1] == _query(_registry(tmp_path), "SELECT sha256 FROM files")[0][0]
    assert asset[2] != asset[4]
    assert asset[2].endswith("/raw.parquet")
    assert asset[7] == "needs_review"
    assert asset[8] == "successful"

    workspace = _workspace(tmp_path)
    manifest_path = workspace / asset[5]
    profile_path = workspace / asset[6]
    normalized_path = workspace / asset[3]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    frame = pl.read_parquet(normalized_path)
    raw_before_cleaning = _sha256(workspace / asset[2])
    assert manifest["layers"]["raw"] == asset[2]
    assert manifest["layers"]["semantic"] is None
    assert {action["type"] for action in manifest["actions"]} >= {
        "trim_empty_rows",
        "trim_empty_columns",
        "mark_exact_duplicate_rows",
        "drop_exact_duplicate_rows",
    }
    assert frame.columns == ["Name", "Name__2", "ID", "amount", "flag", "date", "marker"]
    assert frame.height == 2
    assert frame["ID"].dtype == pl.String
    assert frame["ID"].to_list() == ["0012", "0003"]
    assert frame["amount"].dtype == pl.Float64
    assert frame["flag"].dtype == pl.Boolean
    assert frame["date"].dtype == pl.Date
    assert frame["marker"].to_list() == ["-", "/"]
    assert profile["row_count"] == 2
    assert profile["column_count"] == 7
    assert profile["exact_duplicate_row_count"] == 2
    assert profile["columns"][2]["possible_identifier_hint"] is True
    assert profile["columns"][2]["inferred_physical_type"] == "String"

    first_counts = _query(
        _registry(tmp_path),
        "SELECT COUNT(*) FROM extraction_runs UNION ALL SELECT COUNT(*) FROM cleaning_runs",
    )
    second = process_source(
        source,
        workers=1,
        drop_exact_duplicates=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert second.reused_extraction >= 1
    assert second.reused_cleaning == 1
    assert second.cleaned == 0
    assert _query(
        _registry(tmp_path),
        "SELECT COUNT(*) FROM extraction_runs UNION ALL SELECT COUNT(*) FROM cleaning_runs",
    ) == first_counts
    assert _sha256(workspace / asset[2]) == raw_before_cleaning


def test_text_cleaning_preserves_raw_and_catalogs_chunks(tmp_path: Path) -> None:
    source = tmp_path / "text source"
    source.mkdir()
    text = source / "note.txt"
    text.write_text("标题\r\n\r\n\r\n正文\x01  \r\n", encoding="utf-8")
    source_before = _sha256(text)

    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.cleaned == 1
    assert summary.text_assets == 1
    assert summary.chars > 0
    assert _sha256(text) == source_before

    row = _query(
        _registry(tmp_path),
        "SELECT normalized_artifact_path, extraction_normalized_artifact_path, cleaning_manifest_path, profile_artifact_path, chunks, quality_status, semantic_status FROM catalog_assets WHERE asset_type='text'",
    )[0]
    assert row[0] != row[1]
    assert row[4] >= 1
    assert row[5] == "needs_review"
    assert row[6] == "pending"
    normalized = (_workspace(tmp_path) / row[0]).read_text(encoding="utf-8")
    assert normalized == "标题\n\n正文\n\n"
    manifest = json.loads((_workspace(tmp_path) / row[2]).read_text(encoding="utf-8"))
    assert manifest["layers"]["semantic"] is None
    assert {item["type"] for item in manifest["actions"]} >= {
        "compress_excessive_blank_lines",
        "remove_control_characters",
        "trim_trailing_whitespace",
    }


def test_cleaner_version_bump_reuses_extraction_only(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "versioned"
    source.mkdir()
    (source / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    first = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert first.cleaned == 1
    extraction_count = _query(_registry(tmp_path), "SELECT COUNT(*) FROM extraction_runs")[0][0]
    cleaning_count = _query(_registry(tmp_path), "SELECT COUNT(*) FROM cleaning_runs")[0][0]

    monkeypatch.setattr("chongzu.paths.TABLE_CLEANER_VERSION", "table-clean-v2")
    second = process_source(
        source,
        workers=1,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert second.reused_extraction >= 1
    assert second.cleaned == 1
    assert second.reused_cleaning == 0
    assert _query(_registry(tmp_path), "SELECT COUNT(*) FROM extraction_runs")[0][0] == extraction_count
    assert _query(_registry(tmp_path), "SELECT COUNT(*) FROM cleaning_runs")[0][0] == cleaning_count + 1
    assert _query(
        _registry(tmp_path),
        "SELECT cleaner_version FROM cleaning_runs ORDER BY finished_at DESC LIMIT 1",
    )[0][0] == "table-clean-v2"


def test_catalog_view_keeps_unsupported_and_isolates_cleaning_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "failure isolation"
    source.mkdir()
    good = source / "good.csv"
    good.write_text("a,b\n1,2\n", encoding="utf-8")
    bad = source / "bad.csv"
    bad.write_text("a,b\n3,4\n", encoding="utf-8")
    unsupported = source / "page.html"
    unsupported.write_text("<html>retained</html>", encoding="utf-8")
    process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    raw_assets = dict(
        _query(
            _registry(tmp_path),
            "SELECT source_relative_path, raw_artifact_path FROM table_assets WHERE is_current=TRUE",
        )
    )
    bad_raw_path = _workspace(tmp_path) / raw_assets["bad.csv"]

    import chongzu.clean.table_cleaner as table_cleaner

    original_read = table_cleaner.pl.read_parquet

    def fail_bad(path, *args, **kwargs):
        if Path(path).resolve() == bad_raw_path.resolve():
            raise OSError("synthetic cleaner failure")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(table_cleaner.pl, "read_parquet", fail_bad)
    failed = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert failed.cleaning_failures == 1
    assert failed.cleaned == 1
    assert _query(
        _registry(tmp_path),
        "SELECT support_status FROM files WHERE relative_path='page.html'",
    ) == [("unsupported",)]
    assert all((_workspace(tmp_path) / path).is_file() for path in raw_assets.values())
    statuses = dict(
        _query(
            _registry(tmp_path),
            "SELECT source_file, cleaning_status FROM catalog_assets WHERE asset_type='table'",
        )
    )
    assert statuses["bad.csv"] == "failed"
    assert statuses["good.csv"] == "successful"


def test_catalog_cli_summary_list_and_show(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "cli source"
    source.mkdir()
    (source / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (source / "note.txt").write_text("A sufficiently long local text note for Catalog preview.\n", encoding="utf-8")
    process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    import chongzu.__main__ as cli
    from chongzu import paths

    monkeypatch.setattr(paths, "REGISTRY_PATH", _registry(tmp_path))
    monkeypatch.setattr(paths, "WORKSPACE_ROOT", _workspace(tmp_path))
    assert cli.main(["catalog", "summary", "--source", str(source)]) == 0
    assert "TableAssets: 1" in capsys.readouterr().out
    assert cli.main(["catalog", "list", "--source", str(source), "--type", "table", "--limit", "1"]) == 0
    listing = capsys.readouterr().out
    asset_id = json.loads(listing)[0]["asset_id"]
    assert cli.main(["catalog", "show", asset_id, "--rows", "1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["asset"]["asset_id"] == asset_id
    assert len(shown["preview"]) == 1
    assert cli.main(["catalog", "list", "--source", str(source), "--type", "text", "--limit", "1"]) == 0
    text_id = json.loads(capsys.readouterr().out)[0]["asset_id"]
    assert cli.main(["catalog", "show", text_id, "--chars", "12"]) == 0
    text_shown = json.loads(capsys.readouterr().out)
    assert text_shown["asset"]["asset_id"] == text_id
    assert len(text_shown["normalized_text_preview"]) <= 12


def test_process_mixed_synthetic_corpus_catalogs_all_supported_routes(tmp_path: Path) -> None:
    source = tmp_path / "mixed process corpus 中文 with spaces"
    source.mkdir()
    (source / "records.csv").write_text("name,name\nalpha,1\nbeta,2\n", encoding="utf-8")
    (source / "records.tsv").write_text("name\tvalue\nalpha\t1\nbeta\t2\n", encoding="utf-8")
    shutil.copy2(Path(__file__).parent / "fixtures" / "python-calamine-base.xls", source / "legacy.xls")
    write_xlsx(source / "book.xlsx", [("Data", [["name", "value"], ["alpha", 1], ["beta", 2]], None)])
    write_pdf(source / "native.pdf", [{"texts": [(72, 72, "Native PDF text with enough content")] }])

    scan_image = _image(source / "_scanned-page.png", table=True)
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=1500, height=900)
    page.insert_image(pymupdf.Rect(0, 0, 1500, 900), filename=str(scan_image))
    document.save(source / "scanned.pdf")
    document.close()
    scan_image.unlink()
    _image(source / "report.png", table=True)
    photo_source = _image(tmp_path / "photo-source.png", table=False)
    Image.open(photo_source).save(source / "photo.jpg", format="JPEG")
    photo_source.unlink()
    (source / "note.txt").write_text(
        "This plain text note is long enough to remain a useful deterministic text asset.\n",
        encoding="utf-8",
    )
    (source / "page.html").write_text("<html><body>retained but unsupported</body></html>", encoding="utf-8")
    (source / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\ncorrupt image payload")
    source_before = {path.name: _sha256(path) for path in source.iterdir()}

    first = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert first.files_discovered == 11
    assert first.files_supported == 10
    assert first.files_unsupported == 1
    assert first.extracted == 9
    assert first.cleaning_failures == 0
    assert first.table_assets >= 6
    assert first.text_assets >= 5
    assert first.quality_issues >= 1
    assert _sha256(source / "broken.png") == source_before["broken.png"]
    assert source_before == {path.name: _sha256(path) for path in source.iterdir()}

    catalog_rows = _query(
        _registry(tmp_path),
        "SELECT asset_type, source_format, source_file, quality_status, semantic_status FROM catalog_assets ORDER BY asset_type, source_file",
    )
    assert any(row[0] == "table" and row[1] == "csv" for row in catalog_rows)
    assert any(row[0] == "table" and row[1] == "xls" for row in catalog_rows)
    assert any(row[0] == "text" and row[1] == "pdf" for row in catalog_rows)
    assert any(row[0] == "text" and row[1] in {"jpeg", "png"} for row in catalog_rows)
    assert all(row[4] == "pending" for row in catalog_rows)
    assert _query(
        _registry(tmp_path),
        "SELECT support_status FROM files WHERE relative_path='page.html'",
    ) == [("unsupported",)]

    second = process_source(
        source,
        workers=1,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert second.reused_extraction >= 9
    assert second.extraction_failures == 1
    assert second.cleaned == 0
    assert second.reused_cleaning == first.table_assets + first.text_assets


def test_process_network_guard_is_local_only(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "offline process"
    _image(source / "article.png", table=False)

    def blocked_connect(*args, **kwargs):
        raise AssertionError("Phase 6 process attempted an external socket connection")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.cleaning_failures == 0
    assert summary.text_assets == 1


def test_empty_table_is_unusable_but_raw_artifact_is_retained(tmp_path: Path) -> None:
    source = tmp_path / "empty table"
    source.mkdir()
    (source / "empty.csv").write_text("a,b\n", encoding="utf-8")
    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.cleaned == 1
    assert summary.unusable == 1
    row = _query(
        _registry(tmp_path),
        "SELECT raw_artifact_path, normalized_artifact_path, quality_status, cleaning_status FROM catalog_assets",
    )[0]
    assert row[2:] == ("unusable", "successful")
    assert (_workspace(tmp_path) / row[0]).is_file()
    assert (_workspace(tmp_path) / row[1]).is_file()
