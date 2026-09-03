"""RC3 product-hardening regression coverage.

These tests deliberately exercise the frozen hardening seams without adding a
new runtime or browser-test dependency.  Process-tree visibility and Windows
Job Object assignment remain release/manual gates; the ownership and state
contracts that can be made deterministic are covered here.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import chongzu.extract.pdf.table_runner as table_runner
import chongzu.registry as registry_module
import chongzu.services.process as process_service
from chongzu.api.app import ApiError, BackendApp
from chongzu.api.lifecycle import stop_server
from chongzu.api.ownership import (
    acquire_server_lock,
    command_fingerprint,
    job_name,
    process_identity,
    project_root_fingerprint,
)
from chongzu.extract.pdf.runner import extract_pdf
from chongzu.extract.pdf.table_runner import extract_pdf_tables
from chongzu.extract.unified import UnifiedExtractionSummary
from chongzu.clean.models import CleaningSummary
from chongzu.locking import LockUnavailable
from chongzu.registry import Registry, RegistryError
from chongzu.search import SearchQuery
from chongzu.semantic.models import SemanticRequest, SemanticResponse
from chongzu.semantic.provider import SemanticProviderError
from chongzu.semantic.settings import AISettingsStore, load_runtime_ai_settings
from chongzu.services.process import ProcessTask, ProcessTaskManager
from chongzu.worker_runtime import configure_hidden_worker_executable

from .pdf_factory import write_pdf


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry_path(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


def _rows(tmp_path: Path, statement: str, parameters: list[object] | None = None) -> list[tuple]:
    registry = Registry.open(_registry_path(tmp_path))
    try:
        return registry.connection.execute(statement, parameters or []).fetchall()
    finally:
        registry.close()


def test_registry_reader_never_runs_schema_ddl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _registry_path(tmp_path)
    initialized = Registry.open(database)
    initialized.close()

    def ddl_must_not_run(_connection) -> None:
        raise AssertionError("operational reader executed schema initialization")

    monkeypatch.setattr(registry_module, "initialize_schema", ddl_must_not_run)
    reader = Registry.open_reader(database)
    try:
        assert reader.schema_version() == 5
        assert reader.connection.execute("SELECT COUNT(*) FROM catalog_assets").fetchone()[0] == 0
    finally:
        reader.close()


def test_process_writer_and_readers_have_no_registry_conflict(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "writer source"
    source.mkdir(parents=True)
    for index in range(16):
        (source / f"note-{index:02d}.txt").write_text(
            (f"alpha research note {index}\n" * 80),
            encoding="utf-8",
        )

    registry_path = project / "workspace" / "state" / "registry.duckdb"
    app = BackendApp(
        project_root=project,
        registry_path=registry_path,
        workspace_root=project / "workspace",
    )
    errors: list[str] = []
    reader_stop = threading.Event()
    reader_deadline = time.monotonic() + 20

    def read_loop() -> None:
        while not reader_stop.is_set() and time.monotonic() < reader_deadline:
            try:
                app.catalog.overview()
                app.catalog.list_assets(limit=10)
                app.quality.list_issues(limit=10)
                app.search.search(SearchQuery(query="alpha", limit=5))
            except Exception as exc:  # the assertion below reports the exact reader failure
                errors.append(f"{type(exc).__name__}: {exc}")
                reader_stop.set()

    readers = [threading.Thread(target=read_loop, daemon=True) for _ in range(4)]
    try:
        for reader in readers:
            reader.start()
        task = app.tasks.submit(source, request_id="req_registry_stress")
        deadline = time.monotonic() + 30
        while task.status in {"queued", "running", "cancelling"} and time.monotonic() < deadline:
            time.sleep(0.05)
        assert task.status == "succeeded", task.error.get("technicalDetail") if task.error else task.public_dict()
    finally:
        reader_stop.set()
        for reader in readers:
            reader.join(timeout=5)
        app.close(timeout=15)
    assert errors == []


def test_second_server_instance_is_blocked_by_os_lifetime_lock(tmp_path: Path) -> None:
    project = tmp_path / "project"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    source_root = Path(__file__).parents[1].resolve()
    child_code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from chongzu.api.ownership import acquire_server_lock\n"
        "project = Path(sys.argv[1])\n"
        "ready = Path(sys.argv[2])\n"
        "release = Path(sys.argv[3])\n"
        "lock = acquire_server_lock(project)\n"
        "ready.write_text('ready', encoding='ascii')\n"
        "while not release.exists(): time.sleep(0.03)\n"
        "lock.release()\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(source_root / "src"), environment.get("PYTHONPATH", "")) if item
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(project), str(ready), str(release)],
        cwd=str(source_root),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                stdout, stderr = child.communicate(timeout=2)
                raise AssertionError(f"lock holder exited: {stdout}\n{stderr}")
            time.sleep(0.03)
        assert ready.is_file(), "child did not acquire the project lifetime lock"
        with pytest.raises(LockUnavailable):
            acquire_server_lock(project)
    finally:
        release.write_text("release", encoding="ascii")
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_second_process_task_is_queued_behind_single_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    (first_source / "one.txt").write_text("one", encoding="utf-8")
    (second_source / "two.txt").write_text("two", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    calls: list[Path] = []

    def fake_process(source: Path, **kwargs) -> UnifiedExtractionSummary:
        source = Path(source).resolve()
        calls.append(source)
        callback = kwargs["progress_callback"]
        callback("scan", 0.05, current_file="synthetic.txt", completed=0, total=1)
        if source == first_source.resolve():
            entered.set()
            assert release.wait(10), "synthetic writer was not released"
        return CleaningSummary(source_root=str(source))

    monkeypatch.setattr(process_service, "process_source", fake_process)
    monkeypatch.setattr(process_service, "configure_hidden_worker_executable", lambda: None)
    manager = ProcessTaskManager.for_paths(
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    try:
        first = manager.submit(first_source, request_id="req_first")
        assert entered.wait(5)
        second = manager.submit(second_source, request_id="req_second")
        time.sleep(0.15)
        assert first.status == "running"
        assert second.status == "queued"
        assert calls == [first_source.resolve()]
        release.set()
        deadline = time.monotonic() + 10
        while (first.status not in {"succeeded", "failed", "cancelled", "interrupted"}
               or second.status not in {"succeeded", "failed", "cancelled", "interrupted"}) and time.monotonic() < deadline:
            time.sleep(0.03)
        assert first.status == "succeeded", first.error
        assert second.status == "succeeded", second.error
        assert calls == [first_source.resolve(), second_source.resolve()]
    finally:
        release.set()
        manager.shutdown(timeout=5)


def test_task_registry_busy_error_is_structured_and_retryable() -> None:
    manager = ProcessTaskManager()
    try:
        task = ProcessTask(task_id="task_test", source="C:/synthetic", request_id="req_task_error")
        task.current_stage = "registry_init"
        error = manager._error_for(
            task,
            RegistryError(
                "Unable to initialize registry: Connection Error: Can't open a connection to same "
                "database file with a different configuration than existing connections"
            ),
        )
    finally:
        manager.shutdown(timeout=1)
    assert error == {
        "code": "REGISTRY_BUSY",
        "stage": "registry_init",
        "message": error["message"],
        "retryable": True,
        "affectedFile": None,
        "runId": None,
        "requestId": "req_task_error",
        "scope": "directory",
        "technicalDetail": "RegistryError: Unable to initialize registry: Connection Error: Can't open a connection to same database file with a different configuration than existing connections",
    }
    assert error["message"]
    assert "different configuration" not in error["message"]


def test_api_maps_registry_configuration_conflict_to_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = BackendApp(
        project_root=tmp_path / "project",
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    try:
        def fail_overview() -> dict[str, object]:
            raise RegistryError(
                "Unable to initialize registry: Can't open a connection to same database file "
                "with a different configuration than existing connections"
            )

        monkeypatch.setattr(app.catalog, "overview", fail_overview)
        with pytest.raises(ApiError) as failure:
            app.handle_api("GET", "/api/v1/overview", {}, request_id="req_busy_mapping")
        assert failure.value.code == "REGISTRY_BUSY"
        assert failure.value.status == 503
        assert failure.value.retryable is True
    finally:
        app.close(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="production worker executable contract is Windows-only")
def test_windows_workers_use_bundled_windowless_python() -> None:
    executable = Path(__import__("chongzu.paths", fromlist=["PYTHONW_EXE"]).PYTHONW_EXE).resolve()
    if not executable.is_file():
        pytest.skip("bundled pythonw.exe is not provisioned in this development checkout")
    from multiprocessing.spawn import get_executable, set_executable

    previous = get_executable()
    try:
        selected = configure_hidden_worker_executable()
        assert selected == executable
        assert Path(get_executable()).resolve() == executable
    finally:
        set_executable(previous)


def test_stale_pid_state_never_terminates_unrelated_python(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("PID identity contract is Windows-only")
    project = tmp_path / "project"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(project.parent))
    try:
        deadline = time.monotonic() + 5
        identity = None
        while identity is None and time.monotonic() < deadline:
            identity = process_identity(child.pid)
            time.sleep(0.03)
        assert identity is not None
        state_path = project / "workspace" / "state" / "server.pid"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "pid": child.pid,
                    "pidCreationTime": identity["creationTime"],
                    "executable": identity["executable"],
                    "projectRootFingerprint": project_root_fingerprint(project),
                    "commandFingerprint": command_fingerprint(project, 18765),
                    "instanceId": "stale-instance",
                    "jobName": job_name(project, "stale-instance"),
                    "port": 18765,
                    "controlToken": "stale-token",
                }
            ),
            encoding="utf-8",
        )
        assert stop_server(project) == 2
        assert child.poll() is None
        assert state_path.is_file()
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_pdf_without_table_evidence_does_not_submit_img2table_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pure text pdf"
    source.mkdir()
    write_pdf(
        source / "body.pdf",
        [{"textboxes": [(72, 72, 520, 700, "Plain research prose without a table.\n" * 14)]} for _ in range(8)],
    )
    called = False

    def unexpected_candidate(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("img2table candidate worker was submitted for a no-table PDF")

    monkeypatch.setattr(table_runner, "_extract_one", unexpected_candidate)
    summary = extract_pdf_tables(
        source,
        workers=1,
        force=True,
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.extraction_failures == 0
    assert summary.table_assets == 0
    assert summary.pages == 8
    assert summary.pages_attempted == 0
    assert called is False
    status, route_reason, warnings = _rows(
        tmp_path,
        "SELECT status, route_reason, warnings_json FROM extraction_runs WHERE attempted_route='pdf_table_candidate'",
    )[0]
    assert (status, route_reason) == ("successful", "no_table_evidence")
    assert json.loads(warnings)["warnings"][0]["reason"] == "no_table_evidence"


def test_pdf_page_assets_and_block_provenance_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "page granularity"
    source.mkdir()
    write_pdf(
        source / "pages.pdf",
        [
            {"texts": [(72, 72, "page one heading"), (72, 110, "page one body")]},
            {"texts": [(72, 72, "page two heading"), (72, 110, "page two body")]},
            {"texts": [(72, 72, "page three heading"), (72, 110, "page three body")]},
        ],
    )
    summary = extract_pdf(
        source,
        workers=1,
        force=True,
        registry_path=_registry_path(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.text_assets_produced == 3
    assets = _rows(
        tmp_path,
        """
        SELECT text_asset_id, page_number, text, raw_artifact_path,
               normalized_artifact_path, metadata_artifact_path
        FROM text_assets WHERE is_current=TRUE ORDER BY page_number
        """,
    )
    assert len(assets) == 3
    assert [row[1] for row in assets] == [1, 2, 3]
    assert len({row[0] for row in assets}) == 3
    for _asset_id, page_number, text, raw_path, normalized_path, metadata_path in assets:
        assert f"page {['one', 'two', 'three'][page_number - 1]}" in text
        assert (_workspace(tmp_path) / raw_path).is_file()
        assert (_workspace(tmp_path) / normalized_path).is_file()
        metadata = json.loads((_workspace(tmp_path) / metadata_path).read_text(encoding="utf-8"))
        assert metadata["page_number"] == page_number
        assert metadata["block_count"] >= 2
        assert len(metadata["blocks"]) == metadata["block_count"]
        assert all(
            {"block_index", "bbox", "raw_text", "normalized_text", "normalized_char_start", "normalized_char_end"}
            <= set(block)
            for block in metadata["blocks"]
        )
        assert all(set(block["bbox"]) >= {"x0", "y0", "x1", "y1"} for block in metadata["blocks"])


class _ConnectionTestProvider:
    name = "synthetic-connection-provider"
    model = "synthetic-connection-model"

    def __init__(self) -> None:
        self.fail = False
        self.requests: list[SemanticRequest] = []

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.requests.append(request)
        if self.fail:
            raise SemanticProviderError("synthetic connection failure", code="connection_error", retryable=True)
        return SemanticResponse(payload={"ok": True}, provider=self.name, model=request.model)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI settings contract is Windows-only")
def test_ai_settings_state_machine_and_dpapi_secret_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    registry_path = project / "workspace" / "state" / "registry.duckdb"
    provider = _ConnectionTestProvider()
    app = BackendApp(
        project_root=project,
        registry_path=registry_path,
        workspace_root=project / "workspace",
        semantic_provider=provider,
    )
    try:
        initial = app.handle_api("GET", "/api/v1/settings/ai", {}, request_id="req_initial").payload["settings"]
        assert initial["status"] == "NOT_CONFIGURED"
        assert initial["enabled"] is False

        incomplete = app.handle_api(
            "PUT",
            "/api/v1/settings/ai",
            {},
            json.dumps({"baseUrl": "http://127.0.0.1:1/v1", "model": "", "timeout": 2}).encode(),
            request_id="req_incomplete",
        ).payload["settings"]
        assert incomplete["status"] == "INCOMPLETE"
        assert incomplete["enabled"] is False

        secret = "synthetic-dpapi-key"
        saved = app.handle_api(
            "PUT",
            "/api/v1/settings/ai",
            {},
            json.dumps(
                {
                    "baseUrl": "http://127.0.0.1:1/v1",
                    "apiKey": secret,
                    "model": "synthetic-model",
                    "timeout": 2,
                }
            ).encode(),
            request_id="req_save",
        ).payload["settings"]
        assert saved["status"] == "CONFIGURED"
        assert saved["enabled"] is True
        assert saved["visionEnabled"] is False
        raw_settings = (project / "config" / "llm.json").read_text(encoding="utf-8")
        assert secret in raw_settings
        assert not AISettingsStore(project).exists

        tested = app.handle_api("POST", "/api/v1/settings/ai/test", {}, request_id="req_test_ok")
        assert tested.status == 200
        assert tested.payload["settings"]["status"] == "CONFIGURED"
        assert tested.payload["settings"]["enabled"] is True
        assert len(provider.requests) == 1
        assert provider.requests[0].asset_id == "chongzu-settings-connection-test"
        assert secret not in json.dumps(provider.requests[0].payload, ensure_ascii=False)

        changed = app.handle_api(
            "PUT",
            "/api/v1/settings/ai",
            {},
            json.dumps(
                {
                    "baseUrl": "http://127.0.0.1:1/v1",
                    "model": "changed-model",
                    "timeout": 2,
                }
            ).encode(),
            request_id="req_changed",
        ).payload["settings"]
        assert changed["status"] == "CONFIGURED"
        assert changed["enabled"] is True
        provider.fail = True
        with pytest.raises(ApiError) as failure:
            app.handle_api("POST", "/api/v1/settings/ai/test", {}, request_id="req_test_fail")
        assert failure.value.code == "AI_CONNECTION_FAILED"
        failed = load_runtime_ai_settings(project)
        assert failed.status == "CONFIGURED"
        assert failed.enabled is True
    finally:
        app.close(timeout=5)


def test_ui_settings_presence_suppresses_env_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env_secret = "synthetic-env-key"
    (project / ".env").write_text(
        "LLM_BASE_URL=http://127.0.0.1:1/v1\n"
        f"LLM_API_KEY={env_secret}\n"
        "LLM_MODEL=env-model\n",
        encoding="utf-8",
    )
    fallback = load_runtime_ai_settings(project)
    assert fallback.source == "env"
    assert fallback.status == "CONFIGURED"
    assert fallback.enabled is True

    AISettingsStore(project).save(base_url="http://127.0.0.1:2/v1", model="", timeout=2)
    ui = load_runtime_ai_settings(project)
    assert ui.source == "ui"
    assert ui.status == "INCOMPLETE"
    assert ui.enabled is False
    assert ui.api_key_configured is False
    assert ui.config.api_key == ""


def test_frontend_terminal_refresh_and_quality_rollback_contract() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert '["succeeded", "failed", "cancelled", "interrupted"]' in source
    assert "await Promise.all([refreshOverview(), refreshCatalog(), refreshIssues()])" in source
    assert "knownTaskStatuses.current[result.taskId] = result.task.status" in source
    assert "const previousIssues = issues" in source
    assert "setIssues(previousIssues)" in source
    assert "pendingIssueUpdates.current" in source
