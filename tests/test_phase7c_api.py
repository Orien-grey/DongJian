"""Phase 7C single-asset semantic API and UI contract tests."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chongzu.api.app import BackendApp
from chongzu.api.server import ChongZuHTTPServer
from chongzu.clean import process_source
from chongzu.semantic.fake_provider import FakeSemanticProvider
from chongzu.semantic.models import SemanticRequest, SemanticResponse
from chongzu.semantic.provider import SemanticProviderError


def _request(base: str, path: str, *, method: str = "GET", payload: object | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prepare(tmp_path: Path, *, configured: bool = True) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    workspace = project / "workspace"
    registry_path = workspace / "state" / "registry.duckdb"
    source = tmp_path / "synthetic source"
    source.mkdir()
    (source / "measurements.csv").write_text(
        "sample_id,temperature_c\nSYN-001,21.5\nSYN-002,22.0\n",
        encoding="utf-8",
    )
    (source / "note.txt").write_text(
        "Synthetic non-sensitive note for the single-asset semantic API test.\n",
        encoding="utf-8",
    )
    if configured:
        project.mkdir(parents=True, exist_ok=True)
        (project / ".env").write_text(
            "LLM_BASE_URL=http://127.0.0.1:1/v1\n"
            "LLM_API_KEY=synthetic-test-key\n"
            "LLM_MODEL=synthetic-model\n"
            "LLM_TIMEOUT_SECONDS=1\n"
            "LLM_MAX_RETRIES=2\n",
            encoding="utf-8",
        )
    process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    return project, workspace, registry_path


@contextmanager
def _server(tmp_path: Path, *, configured: bool = True, provider: object | None = None) -> Iterator[tuple[str, object, Path]]:
    project, workspace, registry_path = _prepare(tmp_path, configured=configured)
    selected_provider = provider or FakeSemanticProvider()
    app = BackendApp(
        project_root=project,
        registry_path=registry_path,
        workspace_root=workspace,
        semantic_provider=selected_provider,
    )
    server = ChongZuHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", selected_provider, workspace
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        thread.join(timeout=5)


def _catalog_ids(base: str) -> tuple[str, str]:
    status, payload = _request(base, "/api/v1/catalog?limit=100")
    assert status == 200
    table = next(item for item in payload["items"] if item["assetType"] == "table")
    text = next(item for item in payload["items"] if item["assetType"] == "text")
    return str(table["assetId"]), str(text["assetId"])


def test_semantic_endpoint_without_config_is_409_and_does_not_call_provider(tmp_path: Path) -> None:
    provider = FakeSemanticProvider()
    with _server(tmp_path, configured=False, provider=provider) as (base, _provider, _workspace):
        status, payload = _request(
            base,
            "/api/v1/assets/not-an-asset/semantic-enrich",
            method="POST",
            payload={},
        )
    assert status == 409
    assert payload["error"]["code"] == "SEMANTIC_NOT_CONFIGURED"
    assert payload["error"]["requestId"].startswith("req_")
    assert provider.calls == 0


def test_semantic_endpoint_fake_table_text_cache_and_hash_contract(tmp_path: Path) -> None:
    with _server(tmp_path) as (base, provider, workspace):
        status, health = _request(base, "/api/v1/health")
        assert status == 200
        assert health["llm"]["status"] == "CONFIGURED"
        assert health["llm"]["networkCalls"] == "disabled"
        assert "synthetic-test-key" not in json.dumps(health)

        table_id, text_id = _catalog_ids(base)
        status, table_before = _request(base, f"/api/v1/assets/{table_id}")
        assert status == 200
        assert table_before["semanticStatus"] == "pending"
        table_raw = _sha256(workspace / table_before["artifacts"]["raw"])
        table_normalized = _sha256(workspace / table_before["artifacts"]["normalized"])
        assert provider.calls == 0

        status, table_result = _request(base, f"/api/v1/assets/{table_id}/semantic-enrich", method="POST", payload={})
        assert status == 200
        assert table_result["status"] == "enriched"
        assert table_result["reused"] is False
        assert table_result["providerCalls"] == 1
        table_after = table_result["asset"]
        assert table_after["assetId"] == table_id
        assert table_after["semanticStatus"] == "enriched"
        assert table_after["semantic"]["model"] == "fake-semantic-v1"
        assert table_after["semantic"]["confidence"] == 0.5
        assert {field["source_column"] for field in table_after["semantic"]["semanticFields"]} == {
            "sample_id",
            "temperature_c",
        }
        assert table_after["displayName"] == table_after["semantic"]["display_name"]
        assert _sha256(workspace / table_after["artifacts"]["raw"]) == table_raw
        assert _sha256(workspace / table_after["artifacts"]["normalized"]) == table_normalized
        assert provider.calls == 1

        status, reuse = _request(base, f"/api/v1/assets/{table_id}/semantic-enrich", method="POST", payload={})
        assert status == 200
        assert reuse["status"] == "reused"
        assert reuse["reused"] is True
        assert reuse["providerCalls"] == 0
        assert provider.calls == 1

        status, rejected_force = _request(
            base,
            f"/api/v1/assets/{table_id}/semantic-enrich",
            method="POST",
            payload={"force": True},
        )
        assert status == 400
        assert rejected_force["error"]["code"] == "SEMANTIC_FORCE_NOT_ALLOWED"
        assert provider.calls == 1

        status, text_before = _request(base, f"/api/v1/assets/{text_id}")
        assert status == 200
        text_raw = _sha256(workspace / text_before["artifacts"]["raw"])
        text_normalized = _sha256(workspace / text_before["artifacts"]["normalized"])
        status, text_result = _request(base, f"/api/v1/assets/{text_id}/semantic-enrich", method="POST", payload={})
        assert status == 200
        assert text_result["status"] == "enriched"
        assert text_result["providerCalls"] == 1
        text_after = text_result["asset"]
        assert text_after["assetId"] == text_id
        assert text_after["semanticStatus"] == "enriched"
        assert text_after["semantic"]["display_name"].startswith("Fake |")
        assert _sha256(workspace / text_after["artifacts"]["raw"]) == text_raw
        assert _sha256(workspace / text_after["artifacts"]["normalized"]) == text_normalized
        assert provider.calls == 2


class _ErrorProvider:
    name = "fake"
    model = "synthetic-model"

    def __init__(self, code: str):
        self.code = code
        self.calls = 0

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.calls += 1
        raise SemanticProviderError("synthetic provider failure", code=self.code, retryable=False)


class _InvalidResponseProvider:
    name = "fake"
    model = "synthetic-model"

    def __init__(self):
        self.calls = 0

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.calls += 1
        return SemanticResponse(
            payload={"display_name": "incomplete"},
            provider=self.name,
            model=self.model,
        )


@pytest.mark.parametrize(
    ("provider", "expected_status", "expected_code"),
    [
        (_ErrorProvider("timeout"), 504, "SEMANTIC_TIMEOUT"),
        (_ErrorProvider("connection_error"), 503, "SEMANTIC_UNAVAILABLE"),
        (_ErrorProvider("http_401"), 502, "SEMANTIC_AUTH_FAILED"),
    ],
)
def test_semantic_endpoint_maps_provider_errors_without_secrets(
    tmp_path: Path,
    provider: _ErrorProvider,
    expected_status: int,
    expected_code: str,
) -> None:
    with _server(tmp_path, provider=provider) as (base, _selected, _workspace):
        _table_id, text_id = _catalog_ids(base)
        status, payload = _request(base, f"/api/v1/assets/{text_id}/semantic-enrich", method="POST", payload={})
    assert status == expected_status
    assert payload["error"]["code"] == expected_code
    assert "synthetic-test-key" not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)
    assert provider.calls == 1


def test_semantic_endpoint_maps_validation_error_without_traceback(tmp_path: Path) -> None:
    provider = _InvalidResponseProvider()
    with _server(tmp_path, provider=provider) as (base, _selected, _workspace):
        _table_id, text_id = _catalog_ids(base)
        status, payload = _request(base, f"/api/v1/assets/{text_id}/semantic-enrich", method="POST", payload={})
    assert status == 502
    assert payload["error"]["code"] == "SEMANTIC_INVALID_RESPONSE"
    assert "Traceback" not in json.dumps(payload)
    assert provider.calls == 1


def test_process_does_not_auto_enrich_and_frontend_state_contract_is_present() -> None:
    app_source = (Path(__file__).parents[1] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "尚未进行 AI 整理" in app_source
    assert "尚未配置 AI 模型" in app_source
    assert "AI 整理将向当前配置的大模型服务发送" in app_source
    assert "semanticEnrich" in app_source
