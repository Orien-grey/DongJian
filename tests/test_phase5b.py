from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket

from PIL import Image, ImageDraw, ImageFont

from chongzu.extract.ocr import OCRBlock, extract_ocr, ocr_data_from_blocks
from chongzu.extract.unified import extract_unified
from chongzu.registry import Registry

from .pdf_factory import write_pdf
from .xlsx_factory import write_xlsx


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


def _font(size: int = 30):
    for candidate in (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/msyh.ttc")):
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _image(path: Path, *, table: bool = True) -> Path:
    image = Image.new("RGB", (1500, 900), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 40), "Research report title", fill="black", font=font)
    draw.text((60, 100), "正文说明 paragraph before the table", fill="black", font=font)
    draw.text((60, 155), "Footer and source note", fill="black", font=font)
    if table:
        x0, y0, cell_width, row_height = 120, 280, 300, 90
        values = (("Name", "Value", "Unit"), ("Alpha", "12", "mg"), ("Beta", "8", "mg"))
        for row_index in range(len(values) + 1):
            y = y0 + row_index * row_height
            draw.line((x0, y, x0 + cell_width * 3, y), fill="black", width=4)
        for column_index in range(4):
            x = x0 + column_index * cell_width
            draw.line((x, y0, x, y0 + row_height * 3), fill="black", width=4)
        for row_index, row in enumerate(values):
            for column_index, value in enumerate(row):
                draw.text(
                    (x0 + column_index * cell_width + 28, y0 + row_index * row_height + 25),
                    value,
                    fill="black",
                    font=font,
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _borderless_table(path: Path) -> Path:
    image = Image.new("RGB", (1500, 850), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 50), "Borderless table", fill="black", font=font)
    values = (("Name", "Value", "Unit"), ("Alpha", "12", "mg"), ("Beta", "8", "mg"))
    for row_index, row in enumerate(values):
        for column_index, value in enumerate(row):
            draw.text((100 + column_index * 360, 220 + row_index * 100), value, fill="black", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _two_tables(path: Path) -> Path:
    image = Image.new("RGB", (1600, 1100), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 40), "Two independent tables", fill="black", font=font)
    for x0, y0, values in (
        (80, 220, (("A", "B"), ("1", "2"))),
        (880, 220, (("C", "D"), ("3", "4"))),
    ):
        cell_width, row_height = 280, 100
        for row_index in range(len(values) + 1):
            y = y0 + row_index * row_height
            draw.line((x0, y, x0 + cell_width * 2, y), fill="black", width=5)
        for column_index in range(3):
            x = x0 + column_index * cell_width
            draw.line((x, y0, x, y0 + row_height * len(values)), fill="black", width=5)
        for row_index, row in enumerate(values):
            for column_index, value in enumerate(row):
                draw.text(
                    (x0 + 30 + column_index * cell_width, y0 + 25 + row_index * row_height),
                    value,
                    fill="black",
                    font=font,
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _long_text_table(path: Path) -> Path:
    image = Image.new("RGB", (1600, 1200), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 35), "Results and source note", fill="black", font=font)
    draw.text(
        (60, 100),
        "This long explanatory paragraph stays outside the table and contains a conservative narrative note.",
        fill="black",
        font=font,
    )
    x0, y0, cell_width, row_height = 100, 320, 430, 110
    values = (("Field", "Result", "Unit"), ("Temperature", "22.5", "C"), ("Pressure", "101", "kPa"))
    for row_index in range(len(values) + 1):
        y = y0 + row_index * row_height
        draw.line((x0, y, x0 + cell_width * 3, y), fill="black", width=5)
    for column_index in range(4):
        x = x0 + column_index * cell_width
        draw.line((x, y0, x, y0 + row_height * len(values)), fill="black", width=5)
    for row_index, row in enumerate(values):
        for column_index, value in enumerate(row):
            draw.text(
                (x0 + 24 + column_index * cell_width, y0 + 30 + row_index * row_height),
                value,
                fill="black",
                font=font,
            )
    draw.text((60, 760), "Footer: source laboratory and acquisition date", fill="black", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _chinese_text(path: Path) -> Path:
    image = Image.new("RGB", (1200, 700), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 60), "研究报告标题", fill="black", font=font)
    draw.text((60, 150), "中文纯文本内容和实验说明", fill="black", font=font)
    draw.text((60, 240), "数据来源：本地实验室", fill="black", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _query(registry_path: Path, statement: str) -> list[tuple]:
    registry = Registry.open(registry_path)
    try:
        return registry.connection.execute(statement).fetchall()
    finally:
        registry.close()


def test_image_dual_extraction_reuses_one_ocr_pass(tmp_path: Path) -> None:
    source = tmp_path / "image corpus 中文 with spaces"
    image = _image(source / "report screenshot.png")
    before = _sha256(image)

    summary = extract_ocr(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert summary.text_assets_produced == 1
    assert summary.table_assets_produced >= 1
    assert summary.image_table_ocr_calls == 1
    assert summary.image_table_extraction_failures == 0
    assert _sha256(image) == before
    table_rows = _query(
        _registry(tmp_path),
        "SELECT extractor, source_kind, page_number, content_sha256, raw_artifact_path, normalized_artifact_path, metadata_artifact_path FROM table_assets WHERE is_current=TRUE",
    )
    assert table_rows
    assert table_rows[0][0] == "img2table-image"
    assert table_rows[0][1] == "image"
    assert table_rows[0][2] is None
    assert table_rows[0][3] == before
    metadata = json.loads((_workspace(tmp_path) / table_rows[0][6]).read_text(encoding="utf-8"))
    assert metadata["ocr_reused"] is True
    assert metadata["ocr_backend_calls"] == 0
    assert metadata["ocr_block_count"] >= 1
    assert metadata["ocr_min_confidence"] <= metadata["ocr_mean_confidence"]

    reused = extract_ocr(
        source,
        workers=1,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert reused.reused == 1
    assert reused.table_assets_produced == summary.table_assets_produced


def test_unified_extract_mixed_source_keeps_unsupported_and_text(tmp_path: Path) -> None:
    source = tmp_path / "mixed corpus 中文"
    source.mkdir()
    csv_path = source / "data.csv"
    csv_path.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
    write_xlsx(source / "book.xlsx", [("Data", [["name", "value"], ["alpha", 1], ["beta", 2]], None)])
    write_pdf(source / "native.pdf", [{"texts": [(72, 72, "Native PDF text")]}])
    _image(source / "screenshot.png")
    (source / "note.txt").write_text("plain text evidence", encoding="utf-8")
    (source / "page.html").write_text("<html><body>retained</body></html>", encoding="utf-8")
    before = {path.name: _sha256(path) for path in source.iterdir()}

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert summary.files_discovered == 6
    assert summary.supported == 5
    assert summary.unsupported == 1
    assert summary.processed == 5
    assert summary.failed == 0
    assert summary.table_assets >= 3
    assert summary.text_assets >= 3
    assert summary.text_chunks >= 3
    assert before == {path.name: _sha256(path) for path in source.iterdir()}
    unsupported = _query(
        _registry(tmp_path),
        "SELECT support_status FROM files WHERE relative_path='page.html'",
    )
    assert unsupported == [("unsupported",)]
    reused = extract_unified(
        source,
        workers=1,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert reused.processed == 5
    assert reused.failed == 0
    assert reused.reused >= 5
    assert reused.table_assets == summary.table_assets
    assert reused.text_assets == summary.text_assets


def test_scanned_page_route_stays_page_local(tmp_path: Path) -> None:
    source = tmp_path / "mixed pdf"
    image_path = _image(source / "page.png", table=True)
    pdf_path = source / "mixed.pdf"
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=800, height=500)
    page.insert_text((40, 60), "native page", fontsize=18)
    page = document.new_page(width=800, height=500)
    page.insert_image(pymupdf.Rect(0, 0, 800, 500), filename=str(image_path))
    document.save(pdf_path)
    document.close()
    image_path.unlink()

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.processed == 1
    assert summary.ocr_summary is not None
    assert summary.ocr_summary.pages_ocred == 1
    rows = _query(
        _registry(tmp_path),
        "SELECT extractor, page_number, source_kind FROM text_assets WHERE is_current=TRUE ORDER BY extractor, page_number",
    )
    assert ("pymupdf-native-text", 1, "page") in rows
    assert any(row[0] == "rapidocr-onnx" and row[1] == 2 and row[2] == "page" for row in rows)
    table_rows = _query(
        _registry(tmp_path),
        "SELECT source_kind, page_number FROM table_assets WHERE is_current=TRUE AND extractor='img2table-image'",
    )
    assert all(row[0] == "page" and row[1] == 2 for row in table_rows)


def test_no_table_image_publishes_text_without_default_table(tmp_path: Path) -> None:
    source = tmp_path / "webpage screenshot 中文"
    image = _image(source / "article.png", table=False)
    before = _sha256(image)

    summary = extract_ocr(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert summary.extracted == 1
    assert summary.text_assets_produced == 1
    assert summary.table_assets_produced == 0
    assert summary.failures == 0
    assert _sha256(image) == before


def test_ocr_block_contract_is_the_img2table_boundary() -> None:
    blocks = [
        OCRBlock(
            text="中文标题",
            confidence=0.91,
            bbox=((10.0, 20.0), (110.0, 20.0), (110.0, 50.0), (10.0, 50.0)),
            page_number=3,
            image="page-0003.png",
            block_index=7,
        )
    ]
    data = ocr_data_from_blocks(blocks, page_key=3)
    assert data is not None
    record = data.records[3][0]
    assert record["value"] == "中文标题"
    assert record["confidence"] == 91
    assert (record["x1"], record["y1"], record["x2"], record["y2"]) == (10, 20, 110, 50)


def test_extraction_network_guard_keeps_core_route_offline(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "offline image"
    _image(source / "offline.png", table=False)

    def blocked_connect(*args, **kwargs):
        raise AssertionError("core extraction attempted an external socket connection")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    summary = extract_ocr(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.failures == 0
    assert summary.text_assets_produced == 1


def test_unified_corrupt_image_isolated_from_other_files(tmp_path: Path) -> None:
    source = tmp_path / "unified failure isolation"
    (source / "data.csv").parent.mkdir(parents=True, exist_ok=True)
    (source / "data.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
    (source / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-an-image")
    (source / "page.html").write_text("<html>retained</html>", encoding="utf-8")

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert summary.files_discovered == 3
    assert summary.supported == 2
    assert summary.unsupported == 1
    assert summary.processed == 1
    assert summary.failed == 1


def test_synthetic_image_corpus_covers_layouts_and_preserves_text(tmp_path: Path) -> None:
    source = tmp_path / "synthetic image corpus 中文 with spaces"
    valid_paths = [
        _chinese_text(source / "01 Chinese text.png"),
        _image(source / "02 bordered table.png", table=True),
        _borderless_table(source / "03 borderless table.png"),
        _image(source / "04 title body table footer.png", table=True),
        _two_tables(source / "05 two tables.png"),
        _long_text_table(source / "06 long text and table.png"),
        _image(source / "07 webpage screenshot.png", table=False),
    ]
    from PIL import Image as PILImage

    blank = source / "08 blank.png"
    PILImage.new("RGB", (800, 400), "white").save(blank)
    high_resolution = _image(tmp_path / "high resolution source.png", table=True)
    low_resolution = source / "09 low resolution.png"
    PILImage.open(high_resolution).resize((240, 144)).save(low_resolution)
    photo = _image(tmp_path / "photo source.png", table=False)
    photo_jpg = source / "普通照片.jpg"
    PILImage.open(photo).save(photo_jpg, format="JPEG")
    corrupt = source / "10 corrupt.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\ncorrupt image payload")
    before = {path.name: _sha256(path) for path in source.iterdir()}

    summary = extract_ocr(
        source,
        workers=2,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert summary.files_attempted == 11
    assert summary.extracted == 10
    assert summary.failures == 1
    assert summary.text_assets_produced == 10
    table_paths = _query(
        _registry(tmp_path),
        "SELECT source_relative_path FROM table_assets WHERE is_current=TRUE AND extractor='img2table-image'",
    )
    table_sources = {row[0] for row in table_paths}
    assert any("bordered table.png" in path for path in table_sources)
    assert any("two tables.png" in path for path in table_sources)
    assert not any("webpage screenshot.png" in path for path in table_sources)
    assert not any("blank.png" in path for path in table_sources)
    assert before == {path.name: _sha256(path) for path in source.iterdir()}
