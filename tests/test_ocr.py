from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from chongzu.extract.ocr import extract_ocr
from chongzu.registry import Registry

from .pdf_factory import write_pdf


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


def _image(path: Path, text: str = "ChongZu OCR 123") -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 260), "white")
    ImageDraw.Draw(image).text((35, 90), text, fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _query(tmp_path: Path, statement: str) -> list[tuple]:
    registry = Registry.open(_registry(tmp_path))
    try:
        return registry.connection.execute(statement).fetchall()
    finally:
        registry.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_image_ocr_publishes_asset_chunks_and_reuses(tmp_path: Path, workers: int) -> None:
    source = tmp_path / "中文 image source with spaces"
    image = _image(source / "网页截图 with spaces.png")
    before = _sha256(image)

    first = extract_ocr(source, workers=workers, force=True, registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    assert first.files_attempted == 1
    assert first.extracted == 1
    assert first.text_assets_produced == 1
    assert first.ocr_chars > 0
    assert first.failures == 0
    assert _sha256(image) == before

    rows = _query(
        tmp_path,
        "SELECT text_asset_id, file_id, content_sha256, extraction_run_id, extractor, source_kind, page_number, bbox_json, text, raw_artifact_path, normalized_artifact_path, metadata_artifact_path FROM text_assets WHERE extractor='rapidocr-onnx' AND is_current=TRUE",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row[4] == "rapidocr-onnx"
    assert row[5] == "image"
    assert row[6] is None
    assert row[7]
    assert "OCR" in row[8]
    for artifact in row[9:12]:
        assert artifact
        assert (_workspace(tmp_path) / artifact).is_file()
    metadata = json.loads((_workspace(tmp_path) / row[11]).read_text(encoding="utf-8"))
    assert metadata["content_sha256"] == before
    assert metadata["source_relative_path"] == "网页截图 with spaces.png"
    assert metadata["ocr_blocks"]

    chunks = _query(tmp_path, "SELECT text_asset_id, provenance_json FROM text_chunks")
    assert chunks and chunks[0][0] == row[0]
    assert "rapidocr-onnx" in chunks[0][1]

    reused = extract_ocr(source, workers=1, registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    assert reused.reused == 1
    assert reused.extracted == 0
    assert _sha256(image) == before


def test_mixed_pdf_ocr_only_scanned_page_preserves_native_text(tmp_path: Path) -> None:
    source = tmp_path / "mixed PDF 中文 with spaces"
    image_path = _image(source / "page.png", "Scanned page 456")
    pdf_path = source / "mixed.pdf"
    document = pymupdf.open()
    page = document.new_page(width=800, height=260)
    page.insert_text((35, 70), "Native page text", fontsize=16)
    scanned = document.new_page(width=800, height=260)
    scanned.insert_image(pymupdf.Rect(0, 0, 800, 260), filename=str(image_path))
    document.save(pdf_path)
    document.close()
    image_path.unlink()
    before = _sha256(pdf_path)

    summary = extract_ocr(source, workers=1, force=True, registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    assert summary.pdfs_considered == 1
    assert summary.pages_ocred == 1
    assert summary.text_assets_produced == 1
    assert summary.failures == 0
    assert _sha256(pdf_path) == before

    rows = _query(
        tmp_path,
        "SELECT extractor, page_number, source_kind, text FROM text_assets WHERE is_current=TRUE ORDER BY extractor, page_number",
    )
    assert any(row[0] == "pymupdf-native-text" and row[1] == 1 and "Native page text" in row[3] for row in rows)
    assert any(row[0] == "rapidocr-onnx" and row[1] == 2 and "Scanned" in row[3] for row in rows)
    assert {row[2] for row in rows} >= {"page"}


def test_jpeg_image_is_an_independent_ocr_candidate(tmp_path: Path) -> None:
    source = tmp_path / "jpeg source"
    image = _image(source / "截图.jpg", "JPEG OCR 456")
    before = _sha256(image)

    summary = extract_ocr(source, workers=1, force=True, registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    assert summary.images_considered == 1
    assert summary.extracted == 1
    assert summary.text_assets_produced == 1
    assert summary.failures == 0
    assert _sha256(image) == before

    rows = _query(
        tmp_path,
        "SELECT extractor, source_kind, text FROM text_assets WHERE extractor='rapidocr-onnx' AND is_current=TRUE",
    )
    assert rows and rows[0][1] == "image" and "JPEG" in rows[0][2]


def test_blank_and_corrupt_images_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "image failures"
    blank = _image(source / "blank.png", "")
    corrupt = source / "corrupt.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
    before = {path.name: _sha256(path) for path in source.iterdir()}

    summary = extract_ocr(source, workers=2, force=True, registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    assert summary.files_attempted == 2
    assert summary.extracted == 1
    assert summary.failures == 1
    assert summary.quality_issues >= 1
    assert before == {path.name: _sha256(path) for path in source.iterdir()}


def test_image_ocr_models_are_project_local() -> None:
    from chongzu.extract.ocr.rapidocr_engine import RapidOCREngine, validate_ocr_models

    paths = validate_ocr_models()
    assert all(path.is_file() for path in paths.values())
    assert all("runtime\\models\\ocr" in str(path).casefold() for path in paths.values())
    assert RapidOCREngine.extractor == "rapidocr-onnx"
