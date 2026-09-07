"""Focused offline regression coverage for the M2 stability boundary."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from dongjian.clean import process_source
from dongjian.services.catalog import CatalogService
from dongjian.services.file_insight import FileInsightService
from dongjian.services.process import ProcessTaskManager
from dongjian.services.report import ReportComposer, ReportRunStore
from dongjian.services.sql import SqlAssetError, SqlQueryService
from dongjian.services.table_trust import CANDIDATE_ONLY, CONFIRMED_STRUCTURE, UNUSABLE, table_trust_level
from tests.fixtures.stability_factory import (
    SlowInvalidFileInsightProvider,
    SlowReportProvider,
    BoundedChineseFileInsightProvider,
    write_biff8_chinese_xls,
    write_direct_docx,
    write_unsupported_layout_docx,
)
from tests.test_report_v1 import _run


def _processed(source: Path, workspace: Path) -> CatalogService:
    registry_path = workspace / "state" / "registry.duckdb"
    process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    return CatalogService(registry_path=registry_path, workspace_root=workspace)


def _file_id(catalog: CatalogService, name: str) -> str:
    for item in catalog.list_files(limit=20)["items"]:
        if item["displayName"] == name:
            return str(item["fileId"])
    raise AssertionError(name)


def _wait_terminal(manager: ProcessTaskManager, task_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task is not None and task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task did not reach terminal state: {task_id}")


def test_biff8_fixture_is_identified_and_quarantined_from_sql(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    xls = write_biff8_chinese_xls(source / "known.xls")
    raw = xls.read_bytes()
    assert raw[:8] == bytes.fromhex("d0cf11e0a1b11ae1")
    assert "\u5f20\u4e09".encode("utf-16le") in raw

    catalog = _processed(source, workspace)
    file_id = _file_id(catalog, "known.xls")
    content = catalog.file_content(file_id)
    assert content is not None
    block = content["sections"][0]["blocks"][0]
    assert block["parserValidity"] == "invalid"
    assert block["qualityStatus"] == "needs_review"

    asset_id = catalog.file_detail(file_id)["tables"][0]["assetId"]
    with pytest.raises(SqlAssetError, match="confirmed structured"):
        SqlQueryService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace).schema([asset_id])


def test_docx_fixture_separates_supported_content_from_layout_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    write_direct_docx(source / "direct.docx")
    write_unsupported_layout_docx(source / "layout.docx")
    catalog = _processed(source, workspace)

    direct = catalog.file_content(_file_id(catalog, "direct.docx"))
    layout = catalog.file_content(_file_id(catalog, "layout.docx"))
    assert direct is not None and direct["sections"]
    assert layout is not None and layout["sections"] == []
    assert layout["sourcePreview"]["sourceUrl"].endswith("/source")


def test_table_trust_levels_keep_candidates_out_of_analysis(tmp_path: Path) -> None:
    assert table_trust_level({"qualityStatus": "ready", "sourceKind": "file"}) == CONFIRMED_STRUCTURE
    assert table_trust_level({"qualityStatus": "ready", "sourceKind": "page"}) == CANDIDATE_ONLY
    assert table_trust_level({"qualityStatus": "needs_review", "candidateStatus": "candidate"}) == CANDIDATE_ONLY
    assert table_trust_level({"qualityStatus": "needs_review", "parserValidity": "invalid"}) == UNUSABLE


def test_invalid_file_insight_reaches_failed_terminal_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    (source / "note.txt").write_text("offline file insight fixture", encoding="utf-8")
    catalog = _processed(source, workspace)
    file_id = _file_id(catalog, "note.txt")
    service = FileInsightService(catalog=catalog, workspace_root=workspace)
    assert service.enqueue_file(file_id)
    provider = SlowInvalidFileInsightProvider(delay=0.01)
    manager = ProcessTaskManager()
    try:
        task = manager.submit_file_insight(file_id, "note.txt", service, provider=provider)
        result = _wait_terminal(manager, task.task_id)
        assert result.status == "failed"
        assert service.status(file_id)["status"] == "failed"
        assert service.queue.entry(file_id)["status"] == "failed"
        assert service.queue.entry(file_id)["error_code"] == "INVALID_RESPONSE_SHAPE"
    finally:
        manager.shutdown(timeout=2)


def test_file_insight_batch_uses_two_workers_and_minimal_chinese_contract(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    for index in range(15):
        (source / f"file-{index}.txt").write_text(f"本地研究资料 {index}", encoding="utf-8")
    catalog = _processed(source, workspace)
    service = FileInsightService(catalog=catalog, workspace_root=workspace)
    file_ids = [str(item["fileId"]) for item in catalog.list_files(limit=20)["items"]]
    assert len(service.queue.enqueue_many([{"file_id": file_id, "source_sha256": str(catalog.file_detail(file_id)["source"]["sha256"])} for file_id in file_ids])) == 15
    provider = BoundedChineseFileInsightProvider()
    manager = ProcessTaskManager()
    try:
        task = manager.submit_file_insight_batch(service, provider=provider, total=15)
        result = _wait_terminal(manager, task.task_id, timeout=10)
        assert result.status == "succeeded"
        assert result.counts["completed"] == 15
        assert result.counts["failed"] == 0
        assert provider.calls == 15
        assert provider.max_active == 2
        record = service.store.read_current(file_ids[0], catalog.file_detail(file_ids[0])["source"]["sha256"])
        assert record is not None and record["insight"]["file_type"] == "txt"
        assert record["insight"]["document_kind"] == "document"
    finally:
        manager.shutdown(timeout=2)


def test_report_cancel_discards_late_provider_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    from dongjian.services.analysis import AnalysisRunStore

    runs = AnalysisRunStore(workspace)
    runs.write(_run("analysis_1"))
    provider = SlowReportProvider(delay=10.0)
    store = ReportRunStore(workspace)
    manager = ProcessTaskManager()

    def runner(progress, cancel_event):
        return ReportComposer(runs, workspace, provider=provider, report_store=store).compose(
            ["analysis_1"], report_id="report_cancelled", cancel_event=cancel_event, progress_callback=progress
        )

    try:
        task = manager.submit_report("report_cancelled", runner, provider=provider)
        assert provider.started.wait(2.0)
        started = time.monotonic()
        cancelled = manager.cancel(task.task_id)
        assert cancelled is not None and cancelled.status == "cancelled"
        assert time.monotonic() - started < 1.0
        provider.release.set()
        _wait_terminal(manager, task.task_id)
        assert store.read("report_cancelled") is None
    finally:
        provider.release.set()
        manager.shutdown(timeout=2)
