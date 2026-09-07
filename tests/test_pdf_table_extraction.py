from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl

from dongjian.extract.pdf.table_quality import DetectedTable, ExpectedTable, score_tables
from dongjian.extract.pdf.table_runner import (
    MAX_PDF_TABLE_WORKERS,
    extract_pdf_tables,
    normalize_pdf_table_workers,
)
from dongjian.registry import Registry

from .pdf_factory import write_pdf


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry_path(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _query(tmp_path: Path, statement: str) -> list[tuple]:
    registry = Registry.open(_registry_path(tmp_path))
    try:
        return registry.connection.execute(statement).fetchall()
    finally:
        registry.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _grid(x0: float, y0: float, rows: list[list[object]], *, cell_width: float = 70, row_height: float = 30) -> dict:
    texts: list[tuple[float, float, str]] = []
    lines: list[tuple[float, float, float, float]] = []
    height = len(rows) * row_height
    width = (len(rows[0]) if rows else 0) * cell_width
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            texts.append(
                (
                    x0 + column_index * cell_width + 10,
                    y0 + row_index * row_height + 20,
                    str(value),
                )
            )
    for row_index in range(len(rows) + 1):
        y = y0 + row_index * row_height
        lines.append((x0, y, x0 + width, y))
    for column_index in range((len(rows[0]) if rows else 0) + 1):
        x = x0 + column_index * cell_width
        lines.append((x, y0, x, y0 + height))
    return {"texts": texts, "lines": lines}


def test_pdf_table_preserves_text_and_publishes_common_assets(tmp_path: Path) -> None:
    source = tmp_path / "网页式 PDF source 中文 with spaces"
    source.mkdir()
    page = _grid(80, 210, [["地区", "月份", "数量"], ["北京", "1月", "12"], ["上海", "2月", "8"]])
    page["fontname"] = "china-s"
    page["texts"] = [
        (72, 72, "年度统计情况"),
        (72, 110, "这是正文段落，表格前后的说明应继续保留。"),
        *page["texts"],
        (72, 360, "来源：合成测试；数据截至本页。"),
    ]
    pdf = write_pdf(source / "网页式.pdf", [page])
    before = _sha256(pdf)
    summary = extract_pdf_tables(
        source,
        workers=1,
        force=True,
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
        ground_truth={
            "网页式.pdf": (
                ExpectedTable(
                    page_number=1,
                    rows=(("地区", "月份", "数量"), ("北京", "1月", "12"), ("上海", "2月", "8")),
                ),
            )
        },
    )

    assert summary.table_assets >= 1
    assert summary.detected_tables == summary.table_assets
    assert summary.true_positives == 1
    assert summary.expected_tables == 1
    assert _sha256(pdf) == before
    rows = _query(
        tmp_path,
        "SELECT file_id, content_sha256, extraction_run_id, source_relative_path, page_number, raw_artifact_path, normalized_artifact_path, metadata_artifact_path, row_count, column_count FROM table_assets WHERE is_current=TRUE",
    )
    assert len(rows) == 1
    table = rows[0]
    assert table[3] == "网页式.pdf"
    assert table[4] == 1
    assert table[1] == before
    workspace = _workspace(tmp_path)
    raw = workspace / table[5]
    normalized = workspace / table[6]
    metadata_path = workspace / table[7]
    assert raw.is_file() and normalized.is_file() and metadata_path.is_file()
    assert raw != normalized
    assert pl.read_parquet(raw).height == table[8]
    assert pl.read_parquet(normalized).width == table[9]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["candidate_status"] == "candidate"
    assert metadata["ocr_enabled"] is False
    assert metadata["source_relative_path"] == "网页式.pdf"
    text_rows = _query(
        tmp_path,
        "SELECT file_id, source_relative_path, page_number FROM text_assets WHERE is_current=TRUE",
    )
    assert text_rows and text_rows[0][0] == table[0] and text_rows[0][2] == 1
    assert text_rows[0][1] == "网页式.pdf"
    assert _query(tmp_path, "SELECT COUNT(*) FROM semantic_metadata")[0][0] == 0


def test_pdf_table_multi_page_and_multiple_tables_have_independent_assets(tmp_path: Path) -> None:
    source = tmp_path / "tables"
    source.mkdir()
    first = _grid(70, 100, [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]])
    first["texts"].insert(0, (72, 65, "Table A"))
    second = _grid(335, 100, [["D", "日期"], ["7", "2024-01-01"], ["9", "2024-02-01"]], cell_width=70)
    second["texts"].insert(0, (337, 65, "Table B"))
    write_pdf(source / "two-on-page.pdf", [{"texts": first["texts"] + second["texts"], "lines": first["lines"] + second["lines"]}])
    write_pdf(source / "multi-page.pdf", [first, second])

    summary = extract_pdf_tables(
        source,
        workers=2,
        force=True,
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
        ground_truth={
            "two-on-page.pdf": (
                ExpectedTable(page_number=1, table_index=0, rows=(("A", "B", "C"), ("1", "2", "3"), ("4", "5", "6"))),
                ExpectedTable(page_number=1, table_index=1, rows=(("D", "日期"), ("7", "2024-01-01"), ("9", "2024-02-01"))),
            ),
            "multi-page.pdf": (
                ExpectedTable(page_number=1, rows=(("A", "B", "C"), ("1", "2", "3"), ("4", "5", "6"))),
                ExpectedTable(page_number=2, rows=(("D", "日期"), ("7", "2024-01-01"), ("9", "2024-02-01"))),
            ),
        },
    )
    assert summary.table_assets >= 4
    assert summary.pages == 3
    assert summary.expected_tables == 4
    assert summary.true_positives >= 2
    assert summary.rows >= 12
    assert _query(tmp_path, "SELECT COUNT(*) FROM table_assets WHERE is_current=TRUE")[0][0] == summary.table_assets
    assert _query(tmp_path, "SELECT COUNT(DISTINCT file_id) FROM table_assets WHERE is_current=TRUE")[0][0] == 2
    issue_types = {row[0] for row in _query(tmp_path, "SELECT issue_type FROM quality_issues")}
    assert "multiple_tables_on_page" in issue_types


def test_pdf_table_negative_and_deferred_files_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "negative"
    source.mkdir()
    write_pdf(source / "body-only.pdf", [{"textboxes": [(72, 72, 520, 180, "正文没有表格。\n第二行说明。")] }])
    write_pdf(source / "image-only.pdf", [{"image_rect": (0, 0, 595, 842)}])
    (source / "corrupt.pdf").write_bytes(b"%PDF-1.7\\nnot a valid PDF")
    summary = extract_pdf_tables(
        source,
        workers=1,
        force=True,
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.table_assets == 0
    assert summary.detected_tables == 0
    assert summary.deferred_to_ocr >= 1
    assert summary.extraction_failures == 0
    assert _query(tmp_path, "SELECT COUNT(*) FROM table_assets")[0][0] == 0
    statuses = {row[0]: row[1] for row in _query(tmp_path, "SELECT source_relative_path, status FROM extraction_runs WHERE attempted_route='pdf_table_candidate'")}
    assert statuses["image-only.pdf"] == "deferred_to_ocr"


def test_pdf_table_borderless_native_text_candidate(tmp_path: Path) -> None:
    source = tmp_path / "borderless"
    source.mkdir()
    write_pdf(
        source / "borderless.pdf",
        [{
            "texts": [
                (80, 120, "地区"), (220, 120, "数量"), (360, 120, "备注"),
                (80, 150, "北京"), (220, 150, "12"), (360, 150, "正常"),
                (80, 180, "上海"), (220, 180, "8"), (360, 180, "复核"),
            ]
        }],
    )
    summary = extract_pdf_tables(
        source,
        workers=1,
        force=True,
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    # Borderless support is enabled explicitly in the candidate configuration;
    # this fixture is retained even when the conservative detector declines a
    # weak whitespace-only region so the benchmark records a false negative.
    assert summary.pages == 1
    assert summary.extraction_failures == 0


def test_pdf_table_reuse_force_and_independent_text_cache(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "reuse"
    source.mkdir()
    write_pdf(source / "data.pdf", [_grid(80, 100, [["A", "B"], ["1", "2"]])])
    first = extract_pdf_tables(source, workers=1, force=True, registry_path=_registry_path(tmp_path), workspace_root=_workspace(tmp_path))
    second = extract_pdf_tables(source, workers=1, registry_path=_registry_path(tmp_path), workspace_root=_workspace(tmp_path))
    assert first.table_assets == 1
    assert second.reused == 1
    assert second.table_assets == 1
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_native_text'")[0][0] == 1
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_table_candidate'")[0][0] == 1

    import dongjian.extract.pdf.img2table_extractor as candidate
    import dongjian.extract.pdf.table_runner as table_runner

    monkeypatch.setattr(candidate, "EXTRACTOR_VERSION", "test-img2table-version")
    monkeypatch.setattr(table_runner, "EXTRACTOR_VERSION", "test-img2table-version")
    changed = extract_pdf_tables(source, workers=1, registry_path=_registry_path(tmp_path), workspace_root=_workspace(tmp_path))
    assert changed.reused == 0
    assert changed.table_assets == 1
    # Candidate identity changed, but the independent PyMuPDF text identity
    # did not, so no second text run is created.
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_native_text'")[0][0] == 1
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_table_candidate'")[0][0] == 2
    forced = extract_pdf_tables(source, workers=1, force=True, registry_path=_registry_path(tmp_path), workspace_root=_workspace(tmp_path))
    assert forced.reused == 0
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_native_text'")[0][0] == 1
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_table_candidate'")[0][0] == 3


def test_pdf_table_changed_content_gets_new_identity(tmp_path: Path) -> None:
    source = tmp_path / "changed"
    source.mkdir()
    pdf = source / "data.pdf"
    write_pdf(pdf, [_grid(80, 100, [["A", "B"], ["1", "2"]])])
    first_sha = _sha256(pdf)
    first = extract_pdf_tables(source, workers=1, force=True, registry_path=_registry_path(tmp_path), workspace_root=_workspace(tmp_path))
    write_pdf(pdf, [_grid(80, 100, [["A", "B"], ["1", "3"], ["4", "5"]])])
    assert _sha256(pdf) != first_sha
    changed = extract_pdf_tables(source, workers=1, registry_path=_registry_path(tmp_path), workspace_root=_workspace(tmp_path))
    assert first.table_assets == 1
    assert changed.reused == 0
    assert changed.table_assets == 1
    assert _query(tmp_path, "SELECT COUNT(*) FROM extraction_runs WHERE attempted_route='pdf_table_candidate'")[0][0] == 2


def test_ground_truth_reports_granular_shape_and_cell_differences() -> None:
    score = score_tables(
        [ExpectedTable(page_number=1, rows=(("A", "B"), ("1", "2")))],
        [DetectedTable(page_number=1, table_index=0, rows=((" A ", "B"), ("1", "wrong"))), DetectedTable(page_number=2, table_index=0, rows=(("extra",),))],
    )
    assert score.true_positives == 1
    assert score.false_negatives == 0
    assert score.obvious_false_positives == 1
    assert score.exact_shape_matches == 1
    assert score.normalized_cell_matches == 3
    assert score.exact_cell_matches == 2
    assert score.extra_cells == 1


def test_pdf_table_worker_bound_is_explicit() -> None:
    assert normalize_pdf_table_workers(1) == 1
    assert normalize_pdf_table_workers(MAX_PDF_TABLE_WORKERS) == MAX_PDF_TABLE_WORKERS
    for value in (0, MAX_PDF_TABLE_WORKERS + 1):
        try:
            normalize_pdf_table_workers(value)
        except ValueError:
            pass
        else:
            raise AssertionError("unbounded PDF table worker count was accepted")
