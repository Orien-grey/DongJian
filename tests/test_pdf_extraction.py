from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from dongjian.extract.pdf.blocks import chunk_text, normalize_text
from dongjian.extract.pdf.runner import (
    MAX_PDF_WORKERS,
    extract_pdf,
    normalize_pdf_workers,
)
from dongjian.registry import Registry

from .pdf_factory import write_pdf


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _extract(tmp_path: Path, source: Path, **kwargs):
    return extract_pdf(
        source,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
        **kwargs,
    )


def _rows(tmp_path: Path, query: str, parameters: list[object] | None = None) -> list[tuple]:
    registry = Registry.open(_registry(tmp_path))
    try:
        return registry.connection.execute(query, parameters or []).fetchall()
    finally:
        registry.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pdf_corpus_extracts_native_text_profiles_and_isolates_corruption(tmp_path: Path) -> None:
    source = tmp_path / "中文 PDF source with spaces"
    source.mkdir()
    write_pdf(source / "native.pdf", [{"texts": [(72, 72, "Native title"), (72, 100, "line two")]}])
    write_pdf(
        source / "multi-page.pdf",
        [{"texts": [(72, 72, "page one") ]}, {"texts": [(72, 72, "page two") ]}],
    )
    write_pdf(
        source / "中文资料.pdf",
        [{"fontname": "china-s", "texts": [(72, 72, "中文内容 fixture")]}],
        title="中文 PDF 标题",
    )
    write_pdf(source / "blank.pdf", [{}])
    write_pdf(source / "image-only.pdf", [{"image_rect": (0, 0, 595, 842)}])
    write_pdf(
        source / "mixed.pdf",
        [{"texts": [(72, 72, "native page") ]}, {"image_rect": (0, 0, 595, 842)}],
    )
    write_pdf(source / "rotated.pdf", [{"rotation": 90, "texts": [(72, 72, "rotated page")]}])
    (source / "corrupt.pdf").write_bytes(b"%PDF-1.7\nnot a valid document")
    before = {path.name: _sha256(path) for path in source.iterdir()}

    summary = _extract(tmp_path, source, workers=1)

    assert summary.pdf_files == 8
    assert summary.files_considered == 8
    assert summary.extracted == 7
    assert summary.failed_pdfs == 1
    assert summary.text_assets_produced >= 6
    assert summary.pages == 9
    assert summary.native_text_pdfs >= 3
    assert summary.mixed_pdfs == 1
    assert summary.suspected_scanned_pdfs == 1
    assert summary.unknown_pdfs == 1
    assert summary.quality_issues >= 3
    assert {
        "PDF files",
        "pages",
        "total bytes",
        "pages/sec",
        "MB/sec",
        "chars extracted",
        "text extraction ms",
        "profiling ms",
        "artifact write ms",
        "registry write ms",
        "wall clock ms",
        "peak memory",
    } <= set(summary.benchmark_metrics())
    assert before == {path.name: _sha256(path) for path in source.iterdir()}

    assets = _rows(
        tmp_path,
        "SELECT text_asset_id, source_relative_path, page_number, bbox_json, raw_artifact_path, normalized_artifact_path, metadata_artifact_path FROM text_assets WHERE is_current=TRUE ORDER BY source_relative_path, page_number, text_asset_id",
    )
    assert assets
    assert {row[1] for row in assets} == {
        "multi-page.pdf",
        "mixed.pdf",
        "native.pdf",
        "rotated.pdf",
        "中文资料.pdf",
    }
    assert all(row[2] and row[3] for row in assets)
    for _, _, _, _, raw, normalized, metadata in assets:
        assert raw and normalized and metadata
        assert raw != normalized
        assert (_workspace(tmp_path) / raw).is_file()
        assert (_workspace(tmp_path) / normalized).is_file()
        assert (_workspace(tmp_path) / metadata).is_file()

    profiles = _rows(
        tmp_path,
        "SELECT source_relative_path, warnings_json, status FROM extraction_runs ORDER BY source_relative_path",
    )
    profile_by_name = {
        row[0]: (json.loads(row[1]), row[2]) for row in profiles
    }
    assert profile_by_name["native.pdf"][0]["profile"]["classification"] == "native_text"
    assert profile_by_name["image-only.pdf"][0]["profile"]["classification"] == "suspected_scanned"
    assert profile_by_name["mixed.pdf"][0]["profile"]["classification"] == "mixed"
    assert profile_by_name["blank.pdf"][0]["profile"]["classification"] == "unknown"
    assert profile_by_name["corrupt.pdf"][1] == "failed"
    profile_path = profile_by_name["native.pdf"][0]["profile_artifact_path"]
    profile = json.loads((_workspace(tmp_path) / profile_path).read_text(encoding="utf-8"))
    assert profile["source_relative_path"] == "native.pdf"
    assert profile["pages"][0]["page_number"] == 1
    assert profile["metadata"]["title"] == "Synthetic DongJian PDF"
    assert profile["pages"][0]["rotation"] == 0
    rotated_profile = profile_by_name["rotated.pdf"][0]["profile"]
    assert rotated_profile["pages"][0]["rotation"] == 90

    issues = _rows(tmp_path, "SELECT issue_type, status FROM quality_issues")
    assert {row[0] for row in issues} >= {
        "no_native_text_layer",
        "suspected_scanned_page",
        "mixed_pdf_evidence",
    }
    assert {row[1] for row in issues} == {"open"}
    assert _rows(tmp_path, "SELECT COUNT(*) FROM table_assets")[0][0] == 0


def test_pdf_text_asset_and_chunk_provenance_and_paragraph_chunking(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pdf(
        source / "long.pdf",
        [{"textboxes": [(72, 72, 520, 500, "first paragraph\nsecond paragraph\n" + "x" * 100)]}],
    )
    summary = _extract(tmp_path, source)
    assert summary.extracted == 1
    asset = _rows(
        tmp_path,
        "SELECT text_asset_id, file_id, content_sha256, extraction_run_id, extractor, extractor_version, source_relative_path, page_number, raw_artifact_path, normalized_artifact_path FROM text_assets WHERE is_current=TRUE",
    )[0]
    chunk = _rows(
        tmp_path,
        "SELECT chunk_id, text_asset_id, file_id, chunk_index, text, char_start, char_end, provenance_json FROM text_chunks ORDER BY chunk_index",
    )[0]
    assert chunk[1] == asset[0] and chunk[2] == asset[1]
    provenance = json.loads(chunk[7])
    assert provenance["content_sha256"] == asset[2]
    assert provenance["extraction_run_id"] == asset[3]
    assert provenance["page_number"] == 1
    assert chunk[4] == (_workspace(tmp_path) / asset[9]).read_text(encoding="utf-8")[: len(chunk[4])]
    assert chunk[6] >= chunk[5]
    assert (_workspace(tmp_path) / asset[8]).is_file()
    assert (_workspace(tmp_path) / asset[9]).is_file()
    assert normalize_text("a\r\nb\x00") == "a\nb"
    chunks = chunk_text("one\ntwo\nthree", max_chars=7)
    assert chunks[0].text.endswith("\n")
    assert chunks[0].char_start == 0


def test_pdf_table_hint_is_quality_evidence_only(tmp_path: Path) -> None:
    source = tmp_path / "table hint"
    source.mkdir()
    write_pdf(
        source / "layout.pdf",
        [{
            "texts": [
                (72, 72, "A"),
                (180, 72, "B"),
                (288, 72, "C"),
                (396, 72, "D"),
            ],
            "lines": [(60, 60, 450, 60), (60, 90, 450, 90), (60, 120, 450, 120), (60, 150, 450, 150)],
        }],
    )
    summary = _extract(tmp_path, source)
    assert summary.extracted == 1
    assert _rows(tmp_path, "SELECT COUNT(*) FROM table_assets")[0][0] == 0
    issue_types = {row[0] for row in _rows(tmp_path, "SELECT issue_type FROM quality_issues")}
    assert "possible_table_candidate" in issue_types
    warnings = json.loads(_rows(tmp_path, "SELECT warnings_json FROM extraction_runs")[0][0])
    profile = json.loads(
        (_workspace(tmp_path) / warnings["profile_artifact_path"]).read_text(encoding="utf-8")
    )
    assert profile["pages"][0]["possible_table_candidate"] is True


def test_small_image_does_not_alone_trigger_scanned_classification(tmp_path: Path) -> None:
    source = tmp_path / "small image"
    source.mkdir()
    write_pdf(source / "logo.pdf", [{"image_rect": (10, 10, 30, 30)}])
    summary = _extract(tmp_path, source)
    assert summary.extracted == 1
    assert summary.unknown_pdfs == 1
    warning = json.loads(_rows(tmp_path, "SELECT warnings_json FROM extraction_runs")[0][0])
    profile = json.loads(
        (_workspace(tmp_path) / warning["profile_artifact_path"]).read_text(encoding="utf-8")
    )
    assert profile["classification"] == "unknown"
    assert "low_text_but_scanned_signal_inconclusive" in profile["pages"][0]["reason_codes"]


def test_pdf_reuse_force_change_and_extractor_version_invalidation(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    pdf = write_pdf(source / "data.pdf", [{"texts": [(72, 72, "one")]}])
    first = _extract(tmp_path, source)
    second = _extract(tmp_path, source)
    assert (first.extracted, first.reused) == (1, 0)
    assert (second.extracted, second.reused) == (0, 1)
    forced = _extract(tmp_path, source, force=True)
    assert (forced.extracted, forced.reused) == (1, 0)
    before = _sha256(pdf)
    write_pdf(source / "data.pdf", [{"texts": [(72, 72, "one"), (72, 100, "two")]}])
    changed = _extract(tmp_path, source)
    assert changed.extracted == 1 and changed.reused == 0
    assert _sha256(pdf) != before

    import dongjian.extract.pdf.runner as runner
    import dongjian.extract.pdf.pymupdf_extractor as extractor

    monkeypatch.setattr(runner, "EXTRACTOR_VERSION", "test-pymupdf-version")
    monkeypatch.setattr(extractor, "EXTRACTOR_VERSION", "test-pymupdf-version")
    invalidated = _extract(tmp_path, source)
    assert invalidated.extracted == 1 and invalidated.reused == 0
    assert _rows(tmp_path, "SELECT COUNT(*) FROM extraction_runs")[0][0] == 4
    assert _rows(tmp_path, "SELECT COUNT(*) FROM text_assets WHERE is_current=TRUE")[0][0] >= 1


def test_pdf_failure_isolated_and_workers_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write_pdf(source / "good.pdf", [{"texts": [(72, 72, "good")]}])
    (source / "bad.pdf").write_bytes(b"%PDF-1.7\nnot a valid document")
    summary = _extract(tmp_path, source, workers=2)
    assert summary.extracted == 1
    assert summary.failed_pdfs == 1
    assert normalize_pdf_workers(1) == 1
    assert normalize_pdf_workers(MAX_PDF_WORKERS) == MAX_PDF_WORKERS
    try:
        normalize_pdf_workers(MAX_PDF_WORKERS + 1)
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded PDF workers were accepted")


def test_pdf_cli_force_is_forwarded(monkeypatch, capsys, tmp_path: Path) -> None:
    import dongjian.__main__ as cli

    captured: dict[str, object] = {}

    def fake_extract(source, *, workers, force):
        captured.update(source=source, workers=workers, force=force)
        return SimpleNamespace(
            source_root=str(source), files_considered=0, pdf_files=0,
            extracted=0, reused=0, text_assets_produced=0, quality_issues=0,
            failed_pdfs=0, pages=0, total_chars=0, total_bytes=0,
            native_text_pdfs=0, mixed_pdfs=0, suspected_scanned_pdfs=0,
            unknown_pdfs=0, wall_time_ms=0.0,
        )

    monkeypatch.setattr(cli, "extract_pdf", fake_extract)
    assert cli.main(["extract", "pdf", str(tmp_path), "--workers", "2", "--force"]) == 0
    assert captured["force"] is True
    assert captured["workers"] == 2
    assert "Reused: 0" in capsys.readouterr().out
