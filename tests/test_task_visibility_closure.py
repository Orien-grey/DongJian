"""Focused regression coverage for task visibility and workspace refresh events."""

from __future__ import annotations

from pathlib import Path
import time

from chongzu.api.app import BackendApp
from chongzu.clean import process_source
from tests.fixtures.stability_factory import BoundedChineseFileInsightProvider


def _wait_terminal(app: BackendApp, task_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = app.tasks.get(task_id)
        if task is not None and task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task did not reach a terminal state: {task_id}")


def test_file_insight_terminal_event_advances_workspace_version(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "source"
    workspace = project / "workspace"
    source.mkdir(parents=True)
    (source / "note.txt").write_text("offline task visibility fixture", encoding="utf-8")
    process_source(source, workers=1, force=True, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)

    provider = BoundedChineseFileInsightProvider(delay=0.01)
    app = BackendApp(
        project_root=project,
        registry_path=workspace / "state" / "registry.duckdb",
        workspace_root=workspace,
        semantic_provider=provider,
    )
    try:
        file_id = str(app.catalog.list_files(limit=10)["items"][0]["fileId"])
        before = app.workspace_snapshot()["workspaceVersion"]
        response = app.handle_api("POST", f"/api/v1/files/{file_id}/insight", {}, b"{}")
        assert response.status == 202
        task_id = str(response.payload["taskId"])
        submitted = app.workspace_snapshot()
        assert submitted["workspaceVersion"] > before
        assert file_id in submitted["changedFileIds"]

        assert _wait_terminal(app, task_id).status == "succeeded"
        completed = app.workspace_snapshot()
        assert completed["workspaceVersion"] > submitted["workspaceVersion"]
        assert file_id in completed["changedFileIds"]
    finally:
        app.close(timeout=5)


def test_frontend_task_visibility_contract_is_targeted_and_local() -> None:
    root = Path(__file__).parents[1]
    app = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    styles = (root / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")

    for text in (
        "ResetProgressOverlay",
        "本地服务正在重新启动，请稍候…",
        "resetPhaseLabel",
        "pendingTaskTransitions",
        "已加入整理队列 · 等待模型",
        "正在验证整理结果",
        "整理失败",
        "视觉路径：",
        "已使用 AI 视觉增强",
        "AI 视觉增强失败，已保留本地识别结果",
        "AI 已处理",
        "正在取消…",
        "仍在处理中…",
        "setSelectedFileRefreshVersion(workspaceVersion)",
        "refreshFileInsightQueue",
    ):
        assert text in app
    assert app.count("window.setTimeout(() => void poll(), 1_000)") == 1
    assert ".file-search-box { flex-wrap: wrap; }" in styles
