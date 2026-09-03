from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from chongzu.clean import process_source
from chongzu.extract.ocr.runner import OCRExtractionSummary
from chongzu.extract.unified import extract_unified
from chongzu.registry import Registry
from chongzu.search import SearchQuery, SearchService
from chongzu.vision.models import VisionCapabilities, VisionRequest, VisionResponse
from chongzu.vision.provider import VisionProviderError
from chongzu.vision.runner import extract_vision
from chongzu.vision.validator import VisionContractError, validate_vision_payload


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image_format = "JPEG" if path.suffix.casefold() in {".jpg", ".jpeg"} else "PNG"
    Image.new("RGB", (80, 60), "white").save(path, format=image_format)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeVisionProvider:
    name = "mock-vision"
    model = "mock-model"
    capabilities = VisionCapabilities(vision=True, contract="mock-vision-json-v1")

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.requests: list[VisionRequest] = []

    def extract(self, request: VisionRequest) -> VisionResponse:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return VisionResponse(
            payload=self.payload,
            provider=self.name,
            model=self.model,
            raw_size_bytes=128,
            request_id="mock-request-1",
        )


def _run_payload(tmp_path: Path, payload: dict[str, Any], *, force: bool = True):
    source = tmp_path / "images"
    image = _image(source / "research.png")
    before = _sha256(image)
    provider = FakeVisionProvider(payload)
    summary = extract_vision(
        source,
        provider=provider,
        force=force,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    return source, image, before, provider, summary


@pytest.mark.parametrize(
    ("payload", "expected_text", "expected_tables"),
    [
        (
            {
                "page_type": "text",
                "title": "Pure text image",
                "useful_text": [{"text": "visible research note", "role": "body"}],
                "tables": [],
            },
            1,
            0,
        ),
        (
            {
                "page_type": "table",
                "title": "",
                "useful_text": [],
                "tables": [{"title": "Measurements", "columns": ["Name", "Value"], "rows": [["A", "12"]]}],
            },
            0,
            1,
        ),
        (
            {
                "page_type": "mixed",
                "title": "Mixed image",
                "useful_text": [{"text": "caption above table", "role": "caption"}],
                "tables": [{"title": "Results", "columns": ["Item", "Value"], "rows": [["B", "8"]]}],
            },
            1,
            1,
        ),
    ],
)
def test_vision_image_routes_create_existing_assets(
    tmp_path: Path,
    payload: dict[str, Any],
    expected_text: int,
    expected_tables: int,
) -> None:
    source, image, before, provider, summary = _run_payload(tmp_path, payload)
    assert summary.files_attempted == 1
    assert summary.extracted == 1
    assert summary.failed == 0
    assert summary.text_assets_produced == expected_text
    assert summary.table_assets_produced == expected_tables
    assert provider.calls == 1
    assert provider.requests[0].media_type == "image/png"
    assert provider.requests[0].output_contract
    assert _sha256(image) == before

    registry = Registry.open(_registry(tmp_path))
    try:
        table_rows = registry.connection.execute(
            "SELECT table_id, extractor, source_kind, content_sha256, extraction_run_id, raw_artifact_path, normalized_artifact_path, metadata_artifact_path FROM table_assets WHERE is_current=TRUE"
        ).fetchall()
        text_rows = registry.connection.execute(
            "SELECT text_asset_id, extractor, source_kind, content_sha256, extraction_run_id, text, raw_artifact_path, normalized_artifact_path, metadata_artifact_path FROM text_assets WHERE is_current=TRUE"
        ).fetchall()
        run = registry.connection.execute(
            "SELECT attempted_route, status, route_reason, error_category FROM extraction_runs"
        ).fetchone()
    finally:
        registry.close()
    assert run == ("vision_llm", "successful", "explicit_ai_vision_image", None)
    assert all(row[1:3] == ("vision_llm", "image") for row in [*table_rows, *text_rows])
    assert all(row[3] == before for row in [*table_rows, *text_rows])
    artifacts = [
        artifact
        for row in table_rows
        for artifact in row[5:]
        if artifact
    ] + [
        artifact
        for row in text_rows
        for artifact in row[6:]
        if artifact
    ]
    assert all((_workspace(tmp_path) / artifact).is_file() for artifact in artifacts)


def test_vision_table_flows_through_cleaning_catalog_search_and_preview(tmp_path: Path) -> None:
    payload = {
        "page_type": "mixed",
        "title": "Lab results",
        "useful_text": [{"text": "sample alpha narrative", "role": "body"}],
        "tables": [{"title": "Results", "columns": ["Sample", "Value"], "rows": [["Alpha", "42"]]}],
    }
    source, image, before, provider, _ = _run_payload(tmp_path, payload)
    # Run the product path as a user would.  Existing deterministic cleaning
    # receives the Vision rows without any special asset model.
    processed = process_source(
        source,
        workers=1,
        vision_mode="ai_vision",
        vision_provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert processed.cleaning_failures == 0
    assert processed.table_assets == 1
    assert processed.text_assets == 1
    assert _sha256(image) == before

    registry = Registry.open(_registry(tmp_path))
    try:
        catalog = registry.connection.execute(
            "SELECT asset_id, asset_type, extractor, cleaning_status, raw_artifact_path, normalized_artifact_path FROM catalog_assets ORDER BY asset_type"
        ).fetchall()
        table_id = registry.connection.execute(
            "SELECT table_id FROM table_assets WHERE is_current=TRUE"
        ).fetchone()[0]
        text_id = registry.connection.execute(
            "SELECT text_asset_id FROM text_assets WHERE is_current=TRUE"
        ).fetchone()[0]
    finally:
        registry.close()
    assert {row[1] for row in catalog} == {"table", "text"}
    assert all(row[2] == "vision_llm" and row[3] == "successful" for row in catalog)
    assert all((_workspace(tmp_path) / row[index]).is_file() for row in catalog for index in (4, 5) if row[index])

    search = SearchService(registry_path=_registry(tmp_path))
    response = search.search(SearchQuery(query="sample alpha", asset_type="text", limit=10))
    assert response.results and response.results[0].asset_id == text_id
    assert "sample alpha" in response.results[0].snippet

    # The existing catalog detail/preview services can open a Vision table.
    from chongzu.services.catalog import CatalogService

    service = CatalogService(registry_path=_registry(tmp_path), workspace_root=_workspace(tmp_path))
    detail = service.asset_detail(table_id)
    preview = service.table_preview(table_id, layer="normalized", limit=20, offset=0)
    assert detail is not None
    assert detail["assetType"] == "table"
    assert preview["rows"] and preview["rows"][0]["Sample"] == "Alpha"


def test_vision_jpeg_uses_image_media_type(tmp_path: Path) -> None:
    source = tmp_path / "images"
    image = _image(source / "research.jpg")
    provider = FakeVisionProvider(
        {
            "page_type": "text",
            "title": "JPEG note",
            "useful_text": [{"text": "jpeg text", "role": "body"}],
            "tables": [],
        }
    )
    summary = extract_vision(
        source,
        provider=provider,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.extracted == 1
    assert provider.requests[0].media_type == "image/jpeg"
    assert image.is_file()


def test_vision_malformed_response_isolated_and_recorded(tmp_path: Path) -> None:
    payload = {
        "page_type": "table",
        "title": "bad",
        "useful_text": [],
        "tables": [{"title": "bad", "columns": ["A", "B"], "rows": [["only one"]]}],
    }
    _source, _image_path, _before, provider, summary = _run_payload(tmp_path, payload)
    assert provider.calls == 1
    assert summary.failed == 1
    assert summary.extracted == 0
    registry = Registry.open(_registry(tmp_path))
    try:
        row = registry.connection.execute(
            "SELECT status, error_category, error_message FROM extraction_runs"
        ).fetchone()
        assets = registry.connection.execute("SELECT COUNT(*) FROM table_assets").fetchone()[0]
    finally:
        registry.close()
    assert row[0] == "failed"
    assert row[1] == "vision_contract_error"
    assert "inconsistent width" in row[2]
    assert assets == 0


def test_vision_non_json_root_response_isolated(tmp_path: Path) -> None:
    _source, _image_path, _before, provider, summary = _run_payload(tmp_path, "not JSON")
    assert provider.calls == 1
    assert summary.failed == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        row = registry.connection.execute(
            "SELECT status, error_category, error_message FROM extraction_runs"
        ).fetchone()
    finally:
        registry.close()
    assert row[0] == "failed"
    assert row[1] == "vision_contract_error"
    assert "root must be a JSON object" in row[2]


def test_vision_empty_result_is_successful_without_assets(tmp_path: Path) -> None:
    _source, _image_path, _before, _provider, summary = _run_payload(
        tmp_path,
        {"page_type": "other", "title": "", "useful_text": [], "tables": []},
    )
    assert summary.extracted == 1
    assert summary.failed == 0
    assert summary.text_assets_produced == 0
    assert summary.table_assets_produced == 0
    registry = Registry.open(_registry(tmp_path))
    try:
        row = registry.connection.execute("SELECT status, warnings_json FROM extraction_runs").fetchone()
    finally:
        registry.close()
    assert row[0] == "successful"
    assert "vision_empty_result" in str(row[1])


def test_vision_provider_error_isolated_per_file(tmp_path: Path) -> None:
    source = tmp_path / "images"
    _image(source / "one.png")
    provider = FakeVisionProvider(
        error=VisionProviderError("synthetic timeout", code="timeout", retryable=True)
    )
    summary = extract_vision(
        source,
        provider=provider,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.failed == 1
    assert provider.calls == 1
    registry = Registry.open(_registry(tmp_path))
    try:
        row = registry.connection.execute("SELECT error_category, error_message FROM extraction_runs").fetchone()
    finally:
        registry.close()
    assert row == ("vision_provider_timeout", "synthetic timeout")


def test_vision_validator_rejects_ragged_and_nested_cells() -> None:
    base = {"page_type": "table", "title": "", "useful_text": [], "tables": []}
    with pytest.raises(VisionContractError):
        validate_vision_payload(
            {
                **base,
                "tables": [{"title": "T", "columns": ["A", "B"], "rows": [["A"]]}],
            }
        )
    with pytest.raises(VisionContractError):
        validate_vision_payload(
            {
                **base,
                "tables": [{"title": "T", "columns": ["A"], "rows": [[["nested"]]]}],
            }
        )


def test_local_mode_does_not_call_vision_and_keeps_ocr_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "images"
    _image(source / "offline.png")
    calls: list[bool] = []

    def fake_ocr(_source, **kwargs):
        calls.append(bool(kwargs["include_images"]))
        return OCRExtractionSummary(source_root=str(source.resolve()))

    class MustNotCallVision:
        def extract(self, _request):  # pragma: no cover - assertion is the test
            raise AssertionError("Vision provider called while local mode is selected")

    monkeypatch.setattr("chongzu.extract.unified.extract_ocr", fake_ocr)
    summary = extract_unified(
        source,
        workers=1,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
        vision_mode="local",
        vision_provider=MustNotCallVision(),
    )
    assert calls == [True]
    assert summary.vision_summary is None


def test_vision_provider_request_contains_image_not_registry_identity() -> None:
    # This is a contract-level test for the provider seam; local identity is
    # attached later by the runner and cannot be selected by the model.
    from chongzu.semantic.config import SemanticConfig
    from chongzu.vision.openai_compatible import OpenAICompatibleVisionProvider

    provider = OpenAICompatibleVisionProvider(
        SemanticConfig(base_url="https://example.invalid/v1", api_key="secret", model="vision-model")
    )
    body = provider._request_body(
        VisionRequest(
            image_bytes=b"image-bytes",
            media_type="image/png",
            model="vision-model",
            output_contract="VISION_JSON_CONTRACT",
        )
    )
    assert b"image_url" in body
    assert b"file_id" not in body
    assert b"asset_id" not in body
    assert b"source_sha256" not in body
