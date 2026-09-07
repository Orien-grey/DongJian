from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from dongjian.api.app import BackendApp
from dongjian.semantic.models import SemanticRequest, SemanticResponse
from dongjian.semantic.provider import SemanticProviderError
from dongjian.services.analysis import AnalysisRunStore
from dongjian.services.process import ProcessTaskManager
from dongjian.services.report import (
    ReportComposer,
    ReportExecutionError,
    ReportRunStore,
    render_report_html,
    render_report_markdown,
    validate_report_payload,
)


class ReportProvider:
    name = "fake-report"
    model = "fake-report-v1"

    def __init__(self, payload: object = None, *, error: BaseException | None = None, api_key: str = "") -> None:
        self.payload = payload if payload is not None else {
            "title": "AI report",
            "executive_summary": "The selected data supports a bounded result.",
            "sections": [{"heading": "Data analysis results", "content": "The saved result was reviewed.", "evidence_ids": ["sql:analysis_1:1"]}],
            "key_findings": [{"statement": "The selected rows support the result.", "evidence_ids": ["sql:analysis_1:1"]}],
            "limitations": [],
            "items_to_verify": [],
        }
        self.error = error
        self.requests: list[SemanticRequest] = []
        self.config = SimpleNamespace(api_key=api_key)

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SemanticResponse(payload=self.payload, provider=self.name, model=self.model)


def _evidence(kind: str, evidence_id: str, *, asset_id: str = "asset_text_1", text: str = "bounded evidence") -> dict[str, object]:
    value: dict[str, object] = {
        "kind": kind,
        "asset_id": asset_id,
        "asset_type": "text" if kind != "sql_result" else "table",
        "display_name": "Evidence asset",
        "source": {
            "fileId": "file_1",
            "relativePath": "reports/source.pdf" if kind != "sql_result" else "data.xlsx",
            "format": "pdf" if kind != "sql_result" else "xlsx",
            "sha256": "a" * 64,
            "pageNumber": 6 if kind == "text_chunk" else None,
            "sheetName": "Sheet2" if kind == "sql_result" else None,
        },
        "snippet": text,
    }
    if kind == "sql_result":
        value.update({"columns": ["region", "total"], "rows": [{"region": "北京", "total": 3}], "row_count": 1, "truncated": False, "sql": "SELECT region, total FROM t1"})
    return value


def _run(run_id: str, *, findings: list[dict[str, object]] | None = None, status: str = "completed", evidence: dict[str, object] | None = None, answer: str = "A bounded analysis answer.") -> dict[str, object]:
    manifest = evidence or {
        "sql:analysis_1:1": _evidence("sql_result", "sql:analysis_1:1"),
        "text:asset_text_1:chunk-0": _evidence("text_chunk", "text:asset_text_1:chunk-0", text="Text evidence from page 6."),
    }
    actual_findings = findings if findings is not None else [{"statement": "The query supports three records.", "evidence_ids": ["sql:analysis_1:1"], "support_level": "direct"}]
    return {
        "analysis_run_id": run_id,
        "created_at": "2026-09-03T08:00:00+00:00",
        "finished_at": "2026-09-03T08:00:01+00:00",
        "status": status,
        "question": "What changed in the selected data?",
        "scope": {"kind": "selected", "asset_ids": ["asset_table_1", "asset_text_1"]},
        "scope_asset_ids": ["asset_table_1", "asset_text_1"],
        "model_identity": {"provider": "fake-analysis", "model": "fake-analysis-v1"},
        "answer": answer,
        "findings": actual_findings,
        "unverified_findings": [{"statement": "The source may contain an unverified exception.", "evidence_ids": ["invented"]}],
        "limitations": ["Only bounded samples were used."],
        "grounding_summary": {"grounded": len(actual_findings), "unverified": 1},
        "evidence_manifest": manifest,
        "executed_safe_sql": [{"step": 1, "sql": "SELECT region, total FROM t1", "row_count": 1, "columns": ["region", "total"]}],
        "source_asset_ids": ["asset_table_1", "asset_text_1"],
        "steps_used": 2,
        "max_steps": 6,
        "steps": [],
        "provider_calls": 2,
        "error": None,
    }


@pytest.fixture()
def report_env(tmp_path: Path) -> tuple[Path, AnalysisRunStore, ReportRunStore]:
    workspace = tmp_path / "workspace"
    analysis = AnalysisRunStore(workspace)
    analysis.write(_run("analysis_1"))
    return workspace, analysis, ReportRunStore(workspace)


def _compose(analysis: AnalysisRunStore, workspace: Path, provider: object | None = None, run_ids: list[str] | None = None, **kwargs: object) -> dict[str, object]:
    return ReportComposer(analysis, workspace, provider=provider).compose(run_ids or ["analysis_1"], **kwargs)


def test_one_analysis_run_deterministic_report(report_env) -> None:
    workspace, analysis, _store = report_env
    report = _compose(analysis, workspace)
    assert report["generation_mode"] == "deterministic_fallback"
    assert report["structured_report"]["key_findings"][0]["evidence_ids"] == ["sql:analysis_1:1"]
    assert "Safe SQL" in report["structured_report"]["sections"][1]["content"]


def test_multiple_analysis_runs_are_combined_without_unselected_reads(report_env) -> None:
    workspace, analysis, _store = report_env
    analysis.write(_run("analysis_2", answer="Second bounded answer."))
    report = _compose(analysis, workspace, run_ids=["analysis_2", "analysis_1"], title="Combined")
    assert report["title"] == "Combined"
    assert report["source_analysis_run_ids"] == ["analysis_2", "analysis_1"]
    assert "Second bounded answer." in report["structured_report"]["executive_summary"]


def test_ai_enhanced_report_uses_strict_json_and_selected_context(report_env) -> None:
    workspace, analysis, _store = report_env
    provider = ReportProvider()
    report = _compose(analysis, workspace, provider=provider)
    assert report["generation_mode"] == "ai_enhanced"
    assert provider.requests and provider.requests[0].reference_data["runs"][0]["question"]
    assert "api_key" not in json.dumps(provider.requests[0].payload).casefold()


@pytest.mark.parametrize("failure", [
    ValueError("invalid JSON"),
    SemanticProviderError("timed out", code="timeout", retryable=True),
])
def test_ai_invalid_json_or_timeout_uses_explicit_deterministic_fallback(report_env, failure: BaseException) -> None:
    workspace, analysis, _store = report_env
    provider = ReportProvider(error=failure)
    report = _compose(analysis, workspace, provider=provider)
    assert report["generation_mode"] == "deterministic_fallback"
    assert report["generation_error"]["code"] in {"MODEL_PROTOCOL_ERROR", "MODEL_TIMEOUT"}


def test_fabricated_report_evidence_is_rejected() -> None:
    payload = {"title": "x", "executive_summary": "y", "sections": [], "key_findings": [{"statement": "bad", "evidence_ids": ["fake"]}], "limitations": [], "items_to_verify": []}
    with pytest.raises(ReportExecutionError, match="outside"):
        validate_report_payload(payload, {"real"})


def test_grounded_report_evidence_is_accepted() -> None:
    payload = {"title": "x", "executive_summary": "y", "sections": [], "key_findings": [{"statement": "good", "evidence_ids": ["real"]}], "limitations": [], "items_to_verify": []}
    assert validate_report_payload(payload, {"real"})["key_findings"][0]["evidence_ids"] == ["real"]


def test_ai_fabricated_evidence_falls_back_without_formal_finding(report_env) -> None:
    workspace, analysis, _store = report_env
    provider = ReportProvider(payload={"title": "bad", "executive_summary": "bad", "sections": [], "key_findings": [{"statement": "not grounded", "evidence_ids": ["invented"]}], "limitations": [], "items_to_verify": []})
    report = _compose(analysis, workspace, provider=provider)
    assert report["generation_mode"] == "deterministic_fallback"
    assert report["generation_error"]["code"] == "REPORT_UNGROUNDED"
    assert report["structured_report"]["key_findings"]


def test_report_accepts_insufficient_evidence_run_as_limited_input(report_env) -> None:
    workspace, analysis, _store = report_env
    analysis.write(_run("analysis_weak", status="insufficient_evidence", findings=[]))
    report = _compose(analysis, workspace, run_ids=["analysis_weak"])
    assert "当前数据不足以支持该结论。" in report["structured_report"]["limitations"]


def test_running_analysis_run_is_not_a_report_input(report_env) -> None:
    workspace, analysis, _store = report_env
    analysis.write(_run("analysis_running", status="running"))
    with pytest.raises(ReportExecutionError) as failure:
        _compose(analysis, workspace, run_ids=["analysis_running"])
    assert failure.value.code == "ANALYSIS_RUN_NOT_COMPLETED"


def test_report_selection_has_eight_run_guard(report_env) -> None:
    workspace, analysis, _store = report_env
    for index in range(2, 11):
        analysis.write(_run(f"analysis_{index}"))
    with pytest.raises(ReportExecutionError) as failure:
        _compose(analysis, workspace, run_ids=[f"analysis_{index}" for index in range(1, 10)])
    assert failure.value.code == "REPORT_INPUT_INVALID"


def test_report_title_and_purpose_are_bounded(report_env) -> None:
    workspace, analysis, _store = report_env
    with pytest.raises(ReportExecutionError):
        _compose(analysis, workspace, title="x" * 201)
    with pytest.raises(ReportExecutionError):
        _compose(analysis, workspace, purpose="x" * 2_001)


def test_report_schema_has_stable_sections_and_no_extra_model_fields(report_env) -> None:
    workspace, analysis, _store = report_env
    report = _compose(analysis, workspace)
    assert set(report["structured_report"]) == {"title", "executive_summary", "sections", "key_findings", "limitations", "items_to_verify"}
    assert report["schema_version"] == "report-v1"


def test_markdown_export_includes_limitations_and_unverified_items(report_env) -> None:
    workspace, analysis, _store = report_env
    report = _compose(analysis, workspace)
    output = render_report_markdown(report)
    assert "Only bounded samples were used." in output
    assert "待核实" in output


def test_html_export_renders_sql_snapshot_as_text(report_env) -> None:
    workspace, analysis, _store = report_env
    output = render_report_html(_compose(analysis, workspace))
    assert "SQL 分析结果" in output
    assert "北京" in output


def test_report_artifact_keeps_source_analysis_bytes_unchanged(report_env) -> None:
    workspace, analysis, _store = report_env
    source_path = analysis.path_for("analysis_1")
    before = source_path.read_bytes()
    _compose(analysis, workspace)
    assert source_path.read_bytes() == before


def test_missing_selected_analysis_run_is_rejected(report_env) -> None:
    workspace, analysis, _store = report_env
    with pytest.raises(ReportExecutionError) as failure:
        _compose(analysis, workspace, run_ids=["analysis_missing"])
    assert failure.value.code == "ANALYSIS_RUN_NOT_FOUND"


def test_report_ids_are_path_safe(report_env) -> None:
    workspace, analysis, store = report_env
    with pytest.raises(ValueError):
        store.path_for("../outside")
    report = _compose(analysis, workspace)
    assert store.path_for(report["report_id"]).parent == store.root


def test_model_identity_and_fallback_marker_are_persisted(report_env) -> None:
    workspace, analysis, _store = report_env
    report = _compose(analysis, workspace)
    assert report["model_identity"]["model"] == "offline"
    assert report["generation_mode"] == "deterministic_fallback"


def test_invalid_export_format_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    analysis = AnalysisRunStore(workspace)
    analysis.write(_run("analysis_1"))
    app = BackendApp(project_root=tmp_path, workspace_root=workspace, registry_path=workspace / "state/registry.duckdb")
    try:
        response = app.handle_api("POST", "/api/v1/reports", {}, json.dumps({"analysisRunIds": ["analysis_1"]}).encode())
        report_id = response.payload["reportId"]
        deadline = time.time() + 4
        while time.time() < deadline and not app.reports.path_for(report_id).exists():
            time.sleep(0.03)
        with pytest.raises(Exception):
            app.handle_api("GET", f"/api/v1/reports/{report_id}/export", {"format": ["pdf"]})
    finally:
        app.close(timeout=2)


def test_unverified_findings_become_items_to_verify(report_env) -> None:
    workspace, analysis, _store = report_env
    run = _run("analysis_1", findings=[{"statement": "fabricated fact", "evidence_ids": ["missing"], "support_level": "direct"}])
    analysis.write(run)
    report = _compose(analysis, workspace)
    structured = report["structured_report"]
    assert structured["key_findings"] == []
    assert any("fabricated fact" in item for item in structured["items_to_verify"])


def test_sql_snapshot_and_text_asset_evidence_are_persisted(report_env) -> None:
    workspace, analysis, store = report_env
    report = _compose(analysis, workspace)
    values = {item["kind"]: item for item in report["evidence_snapshot"]}
    assert values["sql_result"]["rows"][0]["region"] == "北京"
    assert values["text_chunk"]["source"]["pageNumber"] == 6
    assert report["executed_safe_sql"][0]["sql"].startswith("SELECT")
    assert store.read(report["report_id"])["evidence_snapshot"]


def test_report_persists_and_lists_after_store_restart(report_env) -> None:
    workspace, analysis, _store = report_env
    report = _compose(analysis, workspace, title="Persistent")
    restarted = ReportRunStore(workspace)
    assert restarted.read(report["report_id"])["title"] == "Persistent"
    assert restarted.list(limit=10)["items"][0]["report_id"] == report["report_id"]


def test_corrupt_report_is_isolated_from_list(report_env) -> None:
    workspace, analysis, store = report_env
    good = _compose(analysis, workspace)
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "report_bad.json").write_text("{bad", encoding="utf-8")
    listed = store.list(limit=10)
    assert [item["report_id"] for item in listed["items"]] == [good["report_id"]]


def test_markdown_export_contains_sources_and_snapshot(report_env) -> None:
    workspace, analysis, _store = report_env
    report = _compose(analysis, workspace)
    markdown = render_report_markdown(report)
    assert "#" in markdown and "来源：" in markdown and "SQL 分析结果" in markdown


def test_html_export_escapes_untrusted_content_and_has_no_external_resources(report_env) -> None:
    workspace, analysis, _store = report_env
    unsafe = _run("analysis_1", answer="<script>alert(1)</script>", evidence={"text:asset_text_1:chunk-0": _evidence("text_chunk", "text:asset_text_1:chunk-0", text="<img src=x onerror=alert(1)>")})
    analysis.write(unsafe)
    report = _compose(analysis, workspace)
    output = render_report_html(report)
    assert "&lt;script&gt;" in output
    assert "<script" not in output.casefold()
    assert "onerror" not in output.casefold()
    assert "http://" not in output and "https://" not in output and "<link" not in output.casefold()


def test_report_context_is_bounded_and_only_selected_runs_are_visible(report_env) -> None:
    workspace, analysis, _store = report_env
    analysis.write(_run("analysis_2", answer="MUST NOT BE SENT"))
    provider = ReportProvider()
    report = _compose(analysis, workspace, provider=provider, run_ids=["analysis_1"])
    context = json.dumps(provider.requests[0].reference_data, ensure_ascii=False)
    assert "MUST NOT BE SENT" not in context
    assert len(json.dumps(provider.requests[0].payload, ensure_ascii=False).encode()) <= 256 * 1024
    assert report["source_analysis_run_ids"] == ["analysis_1"]


def test_report_does_not_persist_api_key_or_provider_request(report_env) -> None:
    workspace, analysis, _store = report_env
    provider = ReportProvider(api_key="report-secret-key")
    report = _compose(analysis, workspace, provider=provider)
    serialized = json.dumps(report, ensure_ascii=False)
    assert "report-secret-key" not in serialized
    assert "raw_request" not in serialized
    assert "reference_data" not in serialized


def test_report_generation_cancellation_leaves_no_formal_artifact(report_env) -> None:
    workspace, analysis, store = report_env
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider(ReportProvider):
        def generate(self, request: SemanticRequest) -> SemanticResponse:
            entered.set()
            release.wait(3)
            return super().generate(request)

    provider = BlockingProvider()
    manager = ProcessTaskManager()
    report_id = "report_cancelled"

    def runner(progress, cancel_event):
        return ReportComposer(analysis, workspace, provider=provider, report_store=store).compose(
            ["analysis_1"], report_id=report_id, cancel_event=cancel_event, progress_callback=progress
        )

    task = manager.submit_report(report_id, runner, provider=provider)
    assert entered.wait(2)
    manager.cancel(task.task_id)
    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and manager.get(task.task_id).status in {"queued", "running", "cancelling"}:
        time.sleep(0.02)
    assert manager.get(task.task_id).status == "cancelled"
    assert not store.path_for(report_id).exists()
    manager.shutdown(timeout=2)


def test_offline_backend_report_api_and_exports(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    analysis = AnalysisRunStore(workspace)
    analysis.write(_run("analysis_1"))
    app = BackendApp(project_root=tmp_path, workspace_root=workspace, registry_path=workspace / "state/registry.duckdb")
    try:
        response = app.handle_api("POST", "/api/v1/reports", {}, json.dumps({"analysisRunIds": ["analysis_1"], "title": "Offline report"}).encode())
        assert response.status == 202
        task_id = response.payload["taskId"]
        deadline = time.time() + 5
        while time.time() < deadline:
            task = app.tasks.get(task_id)
            if task and task.status not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.03)
        assert task.status == "succeeded"
        report_id = response.payload["reportId"]
        listed = app.handle_api("GET", "/api/v1/reports", {"limit": ["10"]})
        assert listed.payload["items"][0]["report_id"] == report_id
        detail = app.handle_api("GET", f"/api/v1/reports/{report_id}", {})
        assert detail.payload["report"]["generation_mode"] == "deterministic_fallback"
        exported = app.handle_api("GET", f"/api/v1/reports/{report_id}/export", {"format": ["html"]})
        assert exported.content_type.startswith("text/html") and exported.raw_body
    finally:
        app.close(timeout=2)


def test_frontend_report_surface_is_present() -> None:
    root = Path(__file__).parents[1] / "frontend" / "src"
    app = (root / "App.tsx").read_text(encoding="utf-8")
    api = (root / "api.ts").read_text(encoding="utf-8")
    assert "ReportsPage" in app and "导出 Markdown" in app and "AI 辅助整理" in app
    assert "/api/v1/reports" in api and "reportExport" in api
