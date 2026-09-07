"""Small, offline fixtures for the M2 stability boundary tests.

The writers deliberately create only synthetic, local inputs.  They are kept
as factories so the repository does not contain personal documents or large
binary samples.
"""

from __future__ import annotations

from pathlib import Path
import struct
import threading
import time
from typing import Any
import zipfile

from dongjian.semantic.models import SemanticRequest, SemanticResponse

from tests.pdf_factory import write_pdf


def _biff_record(record_id: int, data: bytes) -> bytes:
    return struct.pack("<HH", record_id, len(data)) + data


def write_biff8_chinese_xls(path: Path) -> Path:
    """Write a minimal regular-stream BIFF8 workbook with Unicode SST data."""

    strings = ["\u59d3\u540d", "\u5b66\u53f7", "\u5f20\u4e09", "\u8bfe\u7a0b"]
    workbook_bof = _biff_record(
        0x0809,
        struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0x41, 6),
    )
    codepage = _biff_record(0x0042, struct.pack("<H", 0x04E4))
    datemode = _biff_record(0x0022, struct.pack("<H", 0))
    shared_string_data = struct.pack("<II", len(strings), len(strings))
    shared_string_data += b"".join(
        struct.pack("<HB", len(value), 1) + value.encode("utf-16le")
        for value in strings
    )
    shared_strings = _biff_record(0x00FC, shared_string_data)

    def boundsheet(offset: int) -> bytes:
        return _biff_record(
            0x0085,
            struct.pack("<IBBBB", offset, 0, 0, 6, 0) + b"Sheet1",
        )

    globals_without_offset = (
        workbook_bof
        + codepage
        + datemode
        + boundsheet(0)
        + shared_strings
        + _biff_record(0x000A, b"")
    )
    sheet_offset = len(globals_without_offset)
    globals_stream = (
        workbook_bof
        + codepage
        + datemode
        + boundsheet(sheet_offset)
        + shared_strings
        + _biff_record(0x000A, b"")
    )
    worksheet = _biff_record(
        0x0809,
        struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0x41, 6),
    )
    worksheet += b"".join(
        _biff_record(0x00FD, struct.pack("<HHHI", row, column, 0, index))
        for row, column, index in ((0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3))
    )
    worksheet += _biff_record(0x0203, struct.pack("<HHHd", 2, 1, 0, 99.0))
    worksheet += _biff_record(0x000A, b"")
    raw_stream = globals_stream + worksheet

    # Force the workbook into the regular FAT stream.  A minimal compound
    # file whose workbook is put in the mini-stream is more machinery than
    # this regression fixture needs.
    sector_size = 512
    stream_size = 4608
    stream = raw_stream.ljust(stream_size, b"\0")
    workbook_sector_count = stream_size // sector_size
    fat_sector = 1 + workbook_sector_count

    header = bytearray(sector_size)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<i", header, 48, 1)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<i", header, 60, -2)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<i", header, 68, -2)
    struct.pack_into("<I", header, 72, 0)
    struct.pack_into("<i", header, 76, 0)

    def directory_entry(name: str, object_type: int, start: int, size: int, child: int = -1) -> bytes:
        entry = bytearray(128)
        encoded_name = (name + "\0").encode("utf-16le")
        entry[: len(encoded_name)] = encoded_name
        struct.pack_into("<H", entry, 64, len(encoded_name))
        entry[66] = object_type
        entry[67] = 0 if object_type == 5 else 1
        struct.pack_into("<iii", entry, 68, -1, -1, child)
        struct.pack_into("<i", entry, 116, start)
        struct.pack_into("<Q", entry, 120, size)
        return bytes(entry)

    directory = (
        directory_entry("Root Entry", 5, -2, 0, 1)
        + directory_entry("Workbook", 2, 2, stream_size)
        + bytes(128 * 2)
    )
    fat = [-1] * 128
    fat[0] = -3
    fat[1] = -2
    for index in range(workbook_sector_count):
        sector = 2 + index
        fat[sector] = sector + 1 if index + 1 < workbook_sector_count else -2

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + struct.pack("<128i", *fat) + directory + stream)
    return path


def write_chinese_pdf(path: Path) -> Path:
    return write_pdf(
        path,
        [{"texts": [(72, 96, "\u59d3\u540d \u5b66\u53f7 \u8bfe\u7a0b \u6210\u7ee9"), (72, 120, "\u5f20\u4e09 20250001 \u7edf\u8ba1\u5b66 99")] }],
        title="Chinese source fixture",
    )


def write_merged_cell_pdf(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {
                "texts": [
                    (50, 80, "成绩表"),
                    (50, 120, "\u59d3\u540d"),
                    (180, 120, "\u5b66\u53f7"),
                    (310, 120, "\u8bfe\u7a0b"),
                    (50, 150, "\u5f20\u4e09"),
                    (180, 150, "20250001"),
                    (310, 150, "统计学"),
                ],
                "lines": [
                    (40, 90, 500, 90),
                    (40, 100, 500, 100),
                    (40, 130, 500, 130),
                    (40, 160, 500, 160),
                    (40, 90, 40, 160),
                    (500, 90, 500, 160),
                    (170, 100, 170, 160),
                    (300, 100, 300, 160),
                ],
            }
        ],
        title="merged-cell candidate fixture",
    )


def _docx_package(document_xml: str) -> dict[str, str]:
    return {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>'
        ),
        "word/document.xml": document_xml,
    }


def write_direct_docx(path: Path) -> Path:
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>\u4e2a\u4eba\u7b80\u5386</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>\u59d3\u540d</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>\u5f20\u4e09</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:sectPr/></w:body></w:document>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in _docx_package(document).items():
            archive.writestr(name, value)
    return path


def write_unsupported_layout_docx(path: Path) -> Path:
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<w:body><w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData uri="urn:synthetic:unsupported-layout"/>'
        '</a:graphic></wp:inline></w:drawing></w:r></w:p><w:sectPr/></w:body></w:document>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in _docx_package(document).items():
            archive.writestr(name, value)
    return path


class SlowInvalidFileInsightProvider:
    name = "slow-invalid-file-insight"
    model = "offline-slow-test"

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.calls += 1
        self.started.set()
        self.release.wait(self.delay)
        return SemanticResponse(
            payload={"invalid": True},
            provider=self.name,
            model=self.model,
        )


class BoundedChineseFileInsightProvider:
    name = "bounded-chinese-file-insight"
    model = "offline-bounded-test"

    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            return SemanticResponse(
                payload={
                    "summary": "这是一个有依据的本地文件摘要。",
                    "important_topics": ["研究资料"],
                    "key_entities_or_fields": [],
                    "quality_notes": [],
                    "analysis_suggestions": [],
                },
                provider=self.name,
                model=self.model,
            )
        finally:
            with self._lock:
                self.active -= 1


class SlowReportProvider:
    name = "slow-report"
    model = "offline-slow-test"

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.calls += 1
        self.started.set()
        self.release.wait(self.delay)
        return SemanticResponse(
            payload={
                "title": "offline stability report",
                "summary": "synthetic report",
                "sections": [],
            },
            provider=self.name,
            model=self.model,
        )
