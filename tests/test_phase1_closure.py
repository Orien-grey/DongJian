"""Temporary-directory acceptance checks for Convergence Closure Phase 1."""

from __future__ import annotations

from pathlib import Path
import threading
import time

import chongzu.services.process as process_module
import chongzu.services.reset as reset_module
from chongzu.api.app import BackendApp
from chongzu.cancellation import CancellationRequested
from chongzu.clean import process_source
from chongzu.registry import Registry
from chongzu.services.file_insight import FileInsightError
from chongzu.services.reset import WorkspaceResetJournal, WorkspaceResetService
from tests.fixtures.stability_factory import write_unsupported_layout_docx


def _wait_terminal(app: BackendApp, task_id: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = app.tasks.get(task_id)
        if task is not None and task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task did not reach a terminal state: {task_id}")


def _file_id(app: BackendApp, name: str) -> str:
    for item in app.catalog.list_files(limit=100)["items"]:
        if item["displayName"] == name:
            return str(item["fileId"])
    raise AssertionError(name)


def test_phase1_cache_invalidation_no_evidence_and_single_file_reprocess(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "source"
    workspace = project / "workspace"
    source.mkdir(parents=True)
    (source / "target.txt").write_text("old local evidence", encoding="utf-8")
    (source / "unrelated.txt").write_text("unrelated local evidence", encoding="utf-8")
    process_source(source, workers=1, force=True, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)

    app = BackendApp(project_root=project, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    try:
        target_id = _file_id(app, "target.txt")
        unrelated_id = _file_id(app, "unrelated.txt")
        initial = app.catalog.file_content(target_id)
        assert initial is not None
        assert "old local evidence" in str(initial)

        (source / "target.txt").write_text("new local evidence", encoding="utf-8")
        task = app.tasks.submit(source, force=True)
        assert _wait_terminal(app, task.task_id).status == "succeeded"
        refreshed = app.catalog.file_content(target_id)
        assert refreshed is not None
        assert "new local evidence" in str(refreshed)
        assert app.workspace_snapshot()["workspaceVersion"] > 0

        registry = Registry.open(workspace / "state" / "registry.duckdb")
        try:
            before_target = registry.connection.execute(
                "SELECT COUNT(*) FROM extraction_runs WHERE file_id=?", [target_id]
            ).fetchone()[0]
            before_unrelated = registry.connection.execute(
                "SELECT COUNT(*) FROM extraction_runs WHERE file_id=?", [unrelated_id]
            ).fetchone()[0]
        finally:
            registry.close()
    finally:
        app.close(timeout=5)

    reprocess = BackendApp(project_root=project, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    try:
        response = reprocess.handle_api("POST", f"/api/v1/files/{target_id}/reprocess", {}, b"{}")
        assert response.status == 202
        assert _wait_terminal(reprocess, str(response.payload["taskId"])).status == "succeeded"
    finally:
        reprocess.close(timeout=5)
    registry = Registry.open(workspace / "state" / "registry.duckdb")
    try:
        after_target = registry.connection.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE file_id=?", [target_id]
        ).fetchone()[0]
        after_unrelated = registry.connection.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE file_id=?", [unrelated_id]
        ).fetchone()[0]
    finally:
        registry.close()
    assert after_target == before_target + 1
    assert after_unrelated == before_unrelated

    empty_project = tmp_path / "empty-project"
    empty_source = empty_project / "source"
    empty_workspace = empty_project / "workspace"
    write_unsupported_layout_docx(empty_source / "layout.docx")
    process_source(
        empty_source,
        workers=1,
        force=True,
        registry_path=empty_workspace / "state" / "registry.duckdb",
        workspace_root=empty_workspace,
    )
    empty_app = BackendApp(
        project_root=empty_project,
        registry_path=empty_workspace / "state" / "registry.duckdb",
        workspace_root=empty_workspace,
    )
    try:
        empty_id = _file_id(empty_app, "layout.docx")
        detail = empty_app.catalog.file_detail(empty_id)
        assert detail is not None
        assert detail["file"]["processingStatus"] == "no_evidence"
        assert empty_app.file_insights.status(empty_id)["status"] == "no_evidence"
        try:
            empty_app.file_insights.enqueue_file(empty_id)
        except FileInsightError as exc:
            assert exc.code == "FILE_INSIGHT_NOT_READY"
        else:
            raise AssertionError("no-evidence file was admitted to FileInsight")
        assert empty_app.file_insights.queue.entry(empty_id) is None
    finally:
        empty_app.close(timeout=5)


def test_phase1_reset_drains_active_tasks_and_handles_win145_writer(tmp_path: Path) -> None:
    local_project = tmp_path / "local-reset"
    local_source = local_project / "workspace" / "input"
    local_source.mkdir(parents=True)
    (local_source / "active.txt").write_text("active local fixture", encoding="utf-8")
    local_started = threading.Event()
    original_process_source = process_module.process_source

    def blocking_local(*_args: object, **kwargs: object) -> object:
        local_started.set()
        cancel_event = kwargs["cancel_event"]
        assert hasattr(cancel_event, "is_set")
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise CancellationRequested()

    process_module.process_source = blocking_local  # type: ignore[assignment]
    local_app = BackendApp(
        project_root=local_project,
        registry_path=local_project / "workspace" / "state" / "registry.duckdb",
        workspace_root=local_project / "workspace",
    )
    try:
        local_task = local_app.tasks.submit(local_source, force=True)
        assert local_started.wait(5)
        local_app.request_shutdown(reset=True)
        assert local_app.close(timeout=3)
        assert local_task.status == "cancelled"
        assert WorkspaceResetService(local_project).reset(preserve_runtime_files=False)["reset"] is True
    finally:
        local_app.close(timeout=1)
        process_module.process_source = original_process_source

    insight_project = tmp_path / "insight-reset"
    insight_source = insight_project / "workspace" / "input"
    insight_workspace = insight_project / "workspace"
    insight_source.mkdir(parents=True)
    (insight_source / "note.txt").write_text("file insight cancellation fixture", encoding="utf-8")
    process_source(
        insight_source,
        workers=1,
        force=True,
        registry_path=insight_workspace / "state" / "registry.duckdb",
        workspace_root=insight_workspace,
    )
    insight_app = BackendApp(
        project_root=insight_project,
        registry_path=insight_workspace / "state" / "registry.duckdb",
        workspace_root=insight_workspace,
    )
    insight_started = threading.Event()

    class BlockingProvider:
        name = "phase1-blocking-provider"
        model = "phase1-offline"

        def generate_cancellable(self, _request: object, cancel_event: threading.Event) -> object:
            insight_started.set()
            while not cancel_event.is_set():
                time.sleep(0.01)
            raise CancellationRequested()

    try:
        insight_id = _file_id(insight_app, "note.txt")
        assert insight_app.file_insights.enqueue_file(insight_id)
        insight_task = insight_app.tasks.submit_file_insight(
            insight_id,
            "note.txt",
            insight_app.file_insights,
            provider=BlockingProvider(),
        )
        assert insight_started.wait(5)
        insight_app.request_shutdown(reset=True)
        assert insight_app.close(timeout=3)
        assert insight_task.status == "cancelled"
        assert insight_app.file_insights.queue.entry(insight_id)["status"] == "cancelled"
        assert WorkspaceResetService(insight_project).reset(preserve_runtime_files=False)["reset"] is True
    finally:
        insight_app.close(timeout=1)

    writer_project = tmp_path / "writer-reset"
    temp_root = writer_project / "cache" / "temp"
    active_target = temp_root / "active"
    active_target.mkdir(parents=True)
    (active_target / "seed.bin").write_bytes(b"seed")
    writer_stop = threading.Event()

    def writer() -> None:
        while not writer_stop.is_set():
            try:
                (active_target / "writer.bin").write_bytes(b"concurrent writer")
            except OSError:
                pass

    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    original_rmtree = reset_module.shutil.rmtree
    first_attempt = True

    def raise_win145_once(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            error = OSError("directory is not empty")
            error.winerror = 145  # type: ignore[attr-defined]
            writer_stop.set()
            writer_thread.join(timeout=3)
            raise error
        return original_rmtree(path, *args, **kwargs)

    reset_module.shutil.rmtree = raise_win145_once  # type: ignore[assignment]
    try:
        result = WorkspaceResetService(writer_project)._clear_directory(
            temp_root,
            stage="cache_output_clear",
            target_category="cache",
        )
    finally:
        reset_module.shutil.rmtree = original_rmtree
        writer_stop.set()
        writer_thread.join(timeout=3)
    assert result["directories"] >= 1
    assert not active_target.exists()

    journal = WorkspaceResetJournal(writer_project / "workspace")
    request_id = "reset_phase1_fixture"
    for phase in ("draining", "server_stopped", "cleanup", "restart", "health_restored"):
        journal.write(request_id=request_id, phase=phase)
    history = journal.read(request_id)["phase_history"]
    assert [entry["phase"] for entry in history[-5:]] == [
        "draining",
        "server_stopped",
        "cleanup",
        "restart",
        "health_restored",
    ]
