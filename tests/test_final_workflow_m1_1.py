"""Focused contracts for the Final Workflow M1.1 closure slice."""

from __future__ import annotations

import json
from pathlib import Path
import time

from chongzu.api.app import BackendApp
from chongzu.clean import process_source
from chongzu.search import SearchQuery, SearchService
from chongzu.semantic.models import SemanticResponse
from chongzu.services.catalog import CatalogService
from chongzu.services.file_insight import FileInsightQueueStore


class _BulkProvider:
    model = "fake-file-insight-m1-1"

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.calls: list[object] = []
        self.fail_ids = fail_ids or set()

    def generate(self, request):
        self.calls.append(request)
        if str(request.asset_id) in self.fail_ids:
            raise RuntimeError("bounded synthetic provider failure")
        return SemanticResponse(
            payload={
                "file_type": "txt",
                "document_kind": "note",
                "summary": "这是受控的本地文件摘要。",
                "important_topics": ["研究"],
                "key_entities_or_fields": [],
                "important_metrics": [],
                "table_summaries": [],
                "date_range": "",
                "quality_notes": [],
                "analysis_suggestions": [],
                "evidence_refs": [],
                "confidence": 0.8,
            },
            provider="fake",
            model=self.model,
        )


def _wait_for_task(app: BackendApp, task_id: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = app.tasks.get(task_id)
        if task is not None and task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return task
        time.sleep(0.02)
    task = app.tasks.get(task_id)
    raise AssertionError(f"task did not finish: {task.public_dict() if task else task_id}")


def _ready_app(tmp_path: Path, count: int = 2) -> tuple[BackendApp, _BulkProvider]:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    for index in range(count):
        (source / f"file-{index}.txt").write_text(f"file {index} bounded workflow context", encoding="utf-8")
    registry_path = workspace / "state" / "registry.duckdb"
    process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    provider = _BulkProvider()
    return BackendApp(project_root=tmp_path, registry_path=registry_path, workspace_root=workspace, semantic_provider=provider), provider


def test_file_insight_bulk_preview_enqueue_and_cache_are_bounded(tmp_path: Path) -> None:
    app, provider = _ready_app(tmp_path)
    try:
        preview = app.handle_api("POST", "/api/v1/file-insights/bulk", {}, b"{}").payload
        assert preview["confirmationRequired"] is True
        assert preview["pending"] == 2
        assert preview["confirmationMessage"] == "将向当前配置的 AI 服务发送每个文件的有界整理上下文。"

        started = app.handle_api(
            "POST",
            "/api/v1/file-insights/bulk",
            {},
            json.dumps({"confirmed": True}).encode("utf-8"),
        )
        assert started.status == 202
        task = _wait_for_task(app, started.payload["taskId"])
        assert task.task_type == "file_insight_batch"
        assert task.status == "succeeded"
        assert task.counts["completed"] == 2
        assert task.counts["failed"] == 0
        assert len(provider.calls) == 2

        queue = app.handle_api("GET", "/api/v1/file-insights/queue", {}).payload
        assert queue["pendingReady"] == 0
        assert queue["cached"] == 2
        assert queue["queued"] == 0
        assert queue["running"] == 0

        repeated = app.handle_api(
            "POST",
            "/api/v1/file-insights/bulk",
            {},
            json.dumps({"confirmed": True}).encode("utf-8"),
        )
        assert repeated.status == 200
        assert repeated.payload["queued"] == 0
        assert repeated.payload["cached"] == 2
        assert len(provider.calls) == 2
    finally:
        app.close()


def test_file_insight_batch_isolates_one_failure_and_continues(tmp_path: Path) -> None:
    app, provider = _ready_app(tmp_path)
    try:
        files = app.catalog.list_files(limit=10)["items"]
        provider.fail_ids.add(next(item["fileId"] for item in files if item["relativePath"] == "file-0.txt"))
        started = app.handle_api(
            "POST",
            "/api/v1/file-insights/bulk",
            {},
            json.dumps({"confirmed": True}).encode("utf-8"),
        )
        task = _wait_for_task(app, started.payload["taskId"])
        assert task.status == "succeeded"
        assert task.counts["completed"] == 1
        assert task.counts["failed"] == 1
        assert len(provider.calls) == 2
    finally:
        app.close()


def test_file_insight_queue_repeat_does_not_reset_active_work_and_cancel_is_bounded(tmp_path: Path) -> None:
    queue = FileInsightQueueStore(tmp_path / "workspace")
    queue.enqueue("file-1", "a" * 64)
    queue.mark_running("file-1")
    queue.enqueue("file-1", "a" * 64)
    assert [item["status"] for item in queue.pending()] == ["running"]

    queue.enqueue_many(
        [{"file_id": "file-2", "source_sha256": "b" * 64}, {"file_id": "file-3", "source_sha256": "c" * 64}]
    )
    assert queue.cancel_pending() == 2
    remaining = queue.pending()
    assert len(remaining) == 1
    assert remaining[0]["file_id"] == "file-1"
    assert remaining[0]["status"] == "running"
    assert queue.counts()["cancelled"] == 2
    assert queue.recover_running() == 1
    assert queue.pending()[0]["status"] == "queued"


def test_file_insight_queue_replaces_stale_queued_content_identity(tmp_path: Path) -> None:
    queue = FileInsightQueueStore(tmp_path / "workspace")
    assert queue.enqueue("file-1", "a" * 64)
    assert queue.enqueue("file-1", "b" * 64)
    pending = queue.pending()
    assert [(item["file_id"], item["source_sha256"]) for item in pending] == [("file-1", "b" * 64)]
    assert queue.counts()["cancelled"] == 1


def test_text_search_returns_a_file_local_locator_for_continuation_loading(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    prefix = "prefix " * 2_500
    (source / "long.txt").write_text(prefix + "needle appears after the first viewer window", encoding="utf-8")
    registry_path = workspace / "state" / "registry.duckdb"
    process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    file_id = CatalogService(registry_path=registry_path, workspace_root=workspace).list_files(limit=10)["items"][0]["fileId"]

    response = SearchService(registry_path=registry_path).search(SearchQuery("needle", file_id=file_id, limit=20))
    assert response.results
    result = response.results[0].as_dict()
    locator = result["locator"]
    assert locator["kind"] == "text"
    assert locator["assetId"]
    assert locator["offset"] >= 12_000
    assert locator["matchOffsets"][0][0] >= locator["offset"]
