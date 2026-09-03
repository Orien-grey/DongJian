from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
from threading import Event
from typing import Any

import pymupdf
import pytest
from PIL import Image

from chongzu.clean import process_source
from chongzu.extract.unified import extract_unified
from chongzu.registry import Registry
from chongzu.search import SearchQuery, SearchService
from chongzu.vision.models import VisionCapabilities, VisionRequest, VisionResponse
from chongzu.vision.pdf_runner import extract_vision_pdf
from chongzu.vision.provider import VisionProviderError


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _png_bytes() -> bytes:
    image = Image.new("RGB", (320, 220), "white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _pdf(path: Path, pages: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    try:
        for kind in pages:
            page = document.new_page(width=420, height=300)
            if kind == "native":
                page.insert_text((40, 80), "native page text", fontsize=18)
            else:
                page.insert_image(page.rect, stream=_png_bytes())
        path.write_bytes(document.tobytes())
    finally:
        document.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SequenceVisionProvider:
    name = "mock-pdf-vision"
    model = "mock-pdf-model"
    endpoint = "mock://pdf"
    capabilities = VisionCapabilities(vision=True, contract="mock-pdf-json-v1")

    def __init__(self, values: list[Any], *, cancel_after_first: Event | None = None) -> None:
        self.values = list(values)
        self.calls = 0
        self.requests: list[VisionRequest] = []
        self.cancel_after_first = cancel_after_first

    def extract(self, request: VisionRequest) -> VisionResponse:
        self.calls += 1
        self.requests.append(request)
        if self.cancel_after_first is not None and self.calls == 1:
            self.cancel_after_first.set()
        value = self.values[min(self.calls - 1, len(self.values) - 1)]
        if isinstance(value, Exception):
            raise value
        return VisionResponse(
            payload=value,
            provider=self.name,
            model=self.model,
            raw_size_bytes=128,
            request_id=f"pdf-request-{self.calls}",
        )


def _mixed_payload(title: str = "scanned page") -> dict[str, Any]:
    return {
        "page_type": "mixed",
        "title": title,
        "useful_text": [{"text": "vision page narrative", "role": "body"}],
        "tables": [{"title": "measurements", "columns": ["sample", "value"], "rows": [["A", 12]]}],
    }


def _catalog_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    registry = Registry.open(_registry(tmp_path))
    try:
        return registry.connection.execute(
            """
            SELECT asset_id, asset_type, extractor, source_kind, page_number,
                   content_sha256, cleaning_status
            FROM catalog_assets ORDER BY asset_type, page_number, asset_id
            """
        ).fetchall()
    finally:
        registry.close()


def test_scanned_pdf_one_page_renders_and_publishes_existing_assets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    pdf = _pdf(source / "scan.pdf", ["scanned"])
    before = _sha(pdf)
    provider = SequenceVisionProvider([_mixed_payload()])
    substages: list[str] = []

    def progress(_stage: str, _value: float, **kwargs: Any) -> None:
        if kwargs.get("current_substage"):
            substages.append(str(kwargs["current_substage"]))

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
        progress_callback=progress,
    )

    assert summary.vision_pdf_summary is not None
    assert summary.vision_pdf_summary.pages_attempted == 1
    assert summary.vision_pdf_summary.pages_succeeded == 1
    assert provider.calls == 1
    assert _sha(pdf) == before
    assert {
        "inspecting_pdf",
        "rendering_page",
        "calling_model",
        "validating_response",
        "writing_assets",
    }.issubset(set(substages))
    rows = _catalog_rows(tmp_path)
    assert {(row[1], row[2], row[3], row[4]) for row in rows} == {
        ("table", "vision_llm", "page", 1),
        ("text", "vision_llm", "page", 1),
    }


def test_scanned_pdf_multi_page_is_page_by_page_and_text_aggregates_per_page(tmp_path: Path) -> None:
    source = tmp_path / "source"
    pdf = _pdf(source / "multi.pdf", ["scanned", "scanned", "scanned"])
    provider = SequenceVisionProvider([_mixed_payload(f"page {index}") for index in range(1, 4)])

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )

    assert summary.vision_pdf_summary.pages_attempted == 3
    assert provider.calls == 3
    registry = Registry.open(_registry(tmp_path))
    try:
        text_pages = registry.connection.execute(
            "SELECT page_number, COUNT(*) FROM text_assets WHERE extractor='vision_llm' AND is_current=TRUE GROUP BY page_number ORDER BY page_number"
        ).fetchall()
        run = registry.connection.execute(
            "SELECT status, pipeline_version, configuration_version FROM extraction_runs WHERE attempted_route='vision_llm'"
        ).fetchone()
    finally:
        registry.close()
    assert text_pages == [(1, 1), (2, 1), (3, 1)]
    assert run == ("successful", "phase-m2-vision-scanned-pdf", "vision-pdf-json-v1")


def test_mixed_pdf_keeps_native_page_and_vision_scanned_page_separate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _pdf(source / "mixed.pdf", ["native", "scanned"])
    provider = SequenceVisionProvider([_mixed_payload("scanned only")])

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.native_pdf_pages == 2
    assert provider.calls == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        rows = registry.connection.execute(
            "SELECT extractor, page_number FROM text_assets WHERE is_current=TRUE ORDER BY page_number, extractor"
        ).fetchall()
    finally:
        registry.close()
    assert ("pymupdf-native-text", 1) in rows
    assert ("vision_llm", 2) in rows


def test_native_pdf_does_not_call_vision_and_local_mode_keeps_pdf_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    _pdf(source / "native.pdf", ["native"])
    provider = SequenceVisionProvider([_mixed_payload()])
    native = extract_unified(
        source,
        workers=1,
        force=True,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert native.vision_pdf_summary is not None
    assert native.vision_pdf_summary.pages_considered == 0
    assert provider.calls == 0

    calls: list[bool] = []

    def fake_ocr(_source: Path, **kwargs: Any):
        calls.append(bool(kwargs["include_pdfs"]))
        from chongzu.extract.ocr.runner import OCRExtractionSummary

        return OCRExtractionSummary(source_root=str(source.resolve()))

    monkeypatch.setattr("chongzu.extract.unified.extract_ocr", fake_ocr)
    fallback = extract_unified(
        source,
        workers=1,
        force=True,
        vision_mode="local",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert fallback.vision_summary is None
    assert calls == [True]
    assert provider.calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"page_type": "mixed", "title": "bad", "useful_text": [], "tables": [{"title": "T", "columns": ["A", "B"], "rows": [[1]]}]},
        {"page_type": "mixed", "title": "bad", "useful_text": [], "tables": "not-an-array"},
        "malformed-json-root",
    ],
)
def test_page_failure_isolated_and_following_page_continues(tmp_path: Path, payload: Any) -> None:
    source = tmp_path / "source"
    _pdf(source / "partial.pdf", ["scanned", "scanned"])
    provider = SequenceVisionProvider([payload, _mixed_payload("good page")])

    summary = extract_unified(
        source,
        workers=1,
        force=True,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.vision_pdf_summary.pages_failed == 1
    assert summary.vision_pdf_summary.pages_succeeded == 1
    assert provider.calls == 2
    registry = Registry.open(_registry(tmp_path))
    try:
        running = registry.connection.execute("SELECT COUNT(*) FROM extraction_runs WHERE status='running'").fetchone()[0]
        issue_count = registry.connection.execute(
            "SELECT COUNT(*) FROM quality_issues WHERE issue_type IN ('vision_contract_error', 'vision_page_failure')"
        ).fetchone()[0]
        pages = registry.connection.execute(
            "SELECT page_number FROM text_assets WHERE extractor='vision_llm' AND is_current=TRUE"
        ).fetchall()
    finally:
        registry.close()
    assert running == 0
    assert issue_count == 1
    assert pages == [(2,)]


def test_cancellation_after_first_pdf_page_preserves_partial_registry_and_stops_calls(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _pdf(source / "cancel.pdf", ["scanned", "scanned", "scanned"])
    cancel = Event()
    provider = SequenceVisionProvider([_mixed_payload()], cancel_after_first=cancel)
    with pytest.raises(Exception) as failure:
        extract_unified(
            source,
            workers=1,
            force=True,
            vision_mode="ai_vision",
            vision_provider=provider,
            registry_path=_registry(tmp_path),
            workspace_root=_workspace(tmp_path),
            cancel_event=cancel,
        )
    assert type(failure.value).__name__ == "CancellationRequested"
    assert provider.calls == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        running = registry.connection.execute("SELECT COUNT(*) FROM extraction_runs WHERE status='running'").fetchone()[0]
        terminal = registry.connection.execute(
            "SELECT status FROM extraction_runs WHERE attempted_route='vision_llm' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    finally:
        registry.close()
    assert running == 0
    assert terminal is not None and terminal[0] in {"successful", "partial", "interrupted"}


def test_pdf_vision_assets_flow_through_clean_catalog_search_and_detail(tmp_path: Path) -> None:
    source = tmp_path / "source"
    pdf = _pdf(source / "detail.pdf", ["scanned"])
    before = _sha(pdf)
    provider = SequenceVisionProvider([_mixed_payload("catalog vision")])
    substages: list[str] = []

    def progress(_stage: str, _value: float, **kwargs: Any) -> None:
        if kwargs.get("current_substage"):
            substages.append(str(kwargs["current_substage"]))

    processed = process_source(
        source,
        workers=1,
        force=True,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
        progress_callback=progress,
    )
    assert processed.cleaning_failures == 0
    assert processed.table_assets == 1
    assert processed.text_assets == 1
    assert "cleaning_assets" in substages
    assert _sha(pdf) == before
    search = SearchService(registry_path=_registry(tmp_path)).search(
        SearchQuery(query="vision page narrative", asset_type="text", limit=10)
    )
    assert search.results and search.results[0].page_number == 1
    vision_text_id = search.results[0].asset_id
    from chongzu.services.catalog import CatalogService

    catalog = CatalogService(registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    detail = catalog.asset_detail(vision_text_id)
    assert detail is not None
    assert detail["source"]["format"] == "pdf"
    assert detail["provenance"]["pageNumber"] == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        table_id = registry.connection.execute(
            "SELECT table_id FROM table_assets WHERE extractor='vision_llm' AND is_current=TRUE LIMIT 1"
        ).fetchone()[0]
    finally:
        registry.close()
    assert catalog.table_preview(table_id, layer="normalized", limit=10)["rows"] == [{"sample": "A", "value": 12}]
