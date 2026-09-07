"""HTTP-independent API application and route orchestration.

The request handler is intentionally thin: this module validates the stable
HTTP contract and delegates catalog, quality, and process work to services.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import html
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import traceback
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4

from dongjian import paths
from dongjian.locking import registry_write_mutex
from dongjian.registry import Registry, RegistryError, is_registry_busy_error
from dongjian.search import SearchQuery, SearchService, SearchValidationError
from dongjian.semantic.config import SemanticConfig
from dongjian.semantic.models import SemanticRequest, SemanticResponse
from dongjian.semantic.provider import SemanticProviderError
from dongjian.semantic.settings import AISettingsError, ProjectAIConfigStore, load_runtime_ai_settings
from dongjian.services import (
    AnalysisService,
    AnalysisExecutionError,
    AnalysisOrchestrator,
    AnalysisRunStore,
    AnalysisServiceError,
    CatalogService,
    ProcessTaskManager,
    QualityService,
    SqlQueryService,
    SqlServiceError,
    SqlTimeoutError,
    SourceValidationError,
    TaskAdmissionError,
    ReportComposer,
    ReportExecutionError,
    ReportRunStore,
    FileInsightPolicyStore,
    FileInsightError,
    FileInsightService,
    configured_file_insight_provider,
    new_report_id,
    render_report_html,
    render_report_markdown,
    WorkspaceResetError,
    WorkspaceResetJournal,
    WorkspaceResetService,
    is_table_trusted_for_analysis,
)
from dongjian.services.analysis import normalize_analysis_request, new_analysis_run_id, MAX_ANALYSIS_HISTORY_LIMIT, MAX_ANALYSIS_STEPS
from dongjian.semantic.runner import SemanticRunner, provider_for_name
from dongjian.vision.openai_compatible import OpenAICompatibleVisionProvider
from dongjian.vision.runner import normalize_vision_mode


APP_NAME = "DongJian"
APP_VERSION = "0.1.0.dev0"
API_VERSION = "v1"
MAX_REQUEST_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        retryable: bool = False,
        diagnostic: str | None = None,
        category: str | None = None,
        stage: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.diagnostic = diagnostic
        self.category = category
        self.stage = stage
        self.details = dict(details or {})


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any]
    raw_body: bytes | None = None
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    after_response: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    on_response_failure: Callable[[], None] | None = field(default=None, repr=False, compare=False)


def _int_param(params: Mapping[str, list[str]], name: str, default: int, *, maximum: int) -> int:
    raw = params.get(name, [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError("invalid_parameter", f"{name} must be an integer") from exc
    if value < 0 or value > maximum:
        raise ApiError("invalid_parameter", f"{name} must be between 0 and {maximum}")
    return value


def _safe_exception_detail(value: object, secret: object = "") -> str:
    """Return bounded technical detail without persisting a provider key."""

    detail = html.unescape(str(value).strip())
    if isinstance(secret, str) and secret:
        detail = detail.replace(secret, "[REDACTED]")
    detail = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", detail)
    detail = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*[\"']?)[^\s,;\"']+", r"\1[REDACTED]", detail)
    detail = re.sub(r"<[^>]*>", " ", detail)
    detail = "".join(character if character in "\t\n\r" or ord(character) >= 32 else " " for character in detail)
    return re.sub(r"\s+", " ", detail).strip()[:512]


class BackendApp:
    """A local application object that can be tested without opening a port."""

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
        frontend_dist: Path | str | None = None,
        task_manager: ProcessTaskManager | None = None,
        semantic_provider: object | None = None,
        logger: logging.Logger | None = None,
        instance_id: str | None = None,
        server_pid: int | None = None,
    ) -> None:
        self.project_root = Path(project_root or paths.PROJECT_ROOT).resolve()
        self.registry_path = Path(registry_path or self.project_root / "workspace" / "state" / "registry.duckdb").resolve()
        self.workspace_root = Path(workspace_root or self.project_root / "workspace").resolve()
        self.frontend_dist = Path(frontend_dist or self.project_root / "frontend" / "dist").resolve()
        Registry.ensure_initialized(self.registry_path)
        recovery_registry = Registry.open(self.registry_path, initialize=False)
        try:
            with registry_write_mutex(self.registry_path):
                recovered = recovery_registry.recover_all_incomplete_runs()
        finally:
            recovery_registry.close()
        self.catalog = CatalogService(registry_path=self.registry_path, workspace_root=self.workspace_root)
        self.file_insights = FileInsightService(catalog=self.catalog, workspace_root=self.workspace_root)
        self.file_insight_policy = FileInsightPolicyStore(self.workspace_root)
        self.quality = QualityService(registry_path=self.registry_path)
        self.search = SearchService(registry_path=self.registry_path)
        self.sql = SqlQueryService(registry_path=self.registry_path, workspace_root=self.workspace_root)
        self.analysis = AnalysisService(
            registry_path=self.registry_path,
            workspace_root=self.workspace_root,
            catalog=self.catalog,
            search=self.search,
            sql=self.sql,
        )
        self.analysis_runs = AnalysisRunStore(self.workspace_root)
        self.reports = ReportRunStore(self.workspace_root)
        self.tasks = task_manager or ProcessTaskManager.for_paths(
            registry_path=self.registry_path,
            workspace_root=self.workspace_root,
        )
        self._workspace_version_lock = threading.Lock()
        self._workspace_version = 0
        self._workspace_changed_file_id: str | None = None
        self._workspace_changed_file_ids: list[str] = []
        set_workspace_change_callback = getattr(self.tasks, "set_workspace_change_callback", None)
        if callable(set_workspace_change_callback):
            set_workspace_change_callback(self._workspace_changed)
        # The optional provider is a test seam only. Production constructs the
        # configured provider for an explicit POST request; no process task
        # or catalog read can invoke semantic enrichment implicitly.
        self._semantic_provider_override = semantic_provider
        self._semantic_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self.instance_id = instance_id
        self.server_pid = server_pid
        self.logger = logger or logging.getLogger("dongjian.api")
        self._recover_file_insights()
        recovered_total = sum(recovered.values())
        if recovered_total:
            self.logger.info("recovered %d incomplete registry runs: %s", recovered_total, recovered)

    def request_shutdown(self, *, reset: bool = False) -> None:
        analysis_tasks = [item for item in self.tasks.list(limit=50) if getattr(item, "task_type", "") == "ai_analysis"]
        if reset:
            cancel_for_reset = getattr(self.tasks, "cancel_for_reset", None)
            if callable(cancel_for_reset):
                cancel_for_reset()
            else:
                self.tasks.request_shutdown()
            cancel_all = getattr(self.file_insights.queue, "cancel_all", None)
            if callable(cancel_all):
                cancel_all(reason="cancelled_by_reset")
        else:
            self.tasks.request_shutdown()
        for task in analysis_tasks:
            if task.analysis_run_id and task.status in {"cancelled", "cancelling", "interrupted"}:
                try:
                    self.analysis_runs.mark_cancelled(task.analysis_run_id)
                except Exception:
                    pass

    def _recover_file_insights(self) -> None:
        """Keep interrupted queue entries as explicit history after startup."""

        active_entries = self.file_insights.queue.entries()
        for item in active_entries:
            if str(item.get("status") or "") not in {"queued", "running"}:
                continue
            file_id = str(item.get("file_id") or "")
            queued_sha = str(item.get("source_sha256") or "")
            if not file_id:
                continue
            reason = "startup_interrupted"
            try:
                detail = self.catalog.file_detail(file_id)
            except Exception:
                detail = None
            source = detail.get("source") if isinstance(detail, Mapping) and isinstance(detail.get("source"), Mapping) else {}
            current_sha = str(source.get("sha256") or "")
            if detail is None or not current_sha or (queued_sha and queued_sha != current_sha):
                reason = "stale_file"
            self.file_insights.queue.cancel_file(file_id, queued_sha or None, reason=reason)

    def close(self, *, timeout: float = 5.0) -> bool:
        return self.tasks.shutdown(timeout=timeout)

    def _workspace_changed(self, file_id: str | None = None) -> None:
        if file_id:
            self.catalog.invalidate_file(file_id)
        with self._workspace_version_lock:
            self._workspace_version += 1
            self._workspace_changed_file_id = file_id
            if file_id and file_id not in self._workspace_changed_file_ids:
                self._workspace_changed_file_ids.append(file_id)
                self._workspace_changed_file_ids = self._workspace_changed_file_ids[-128:]

    def workspace_snapshot(self) -> dict[str, Any]:
        with self._workspace_version_lock:
            version = self._workspace_version
            changed_file_id = self._workspace_changed_file_id
            changed_file_ids = list(self._workspace_changed_file_ids)
            self._workspace_changed_file_ids.clear()
        return {
            "workspaceVersion": version,
            "changedFileId": changed_file_id,
            "changedFileIds": changed_file_ids,
            "tasks": [item.public_dict() for item in self.tasks.list(limit=20)],
        }

    def _health(self) -> dict[str, Any]:
        try:
            runtime = load_runtime_ai_settings(self.project_root)
            llm = {
                "status": runtime.status,
                "configured": runtime.configured,
                "enabled": runtime.enabled,
                "source": runtime.source,
                "apiKeyConfigured": runtime.api_key_configured,
                "visionEnabled": runtime.vision_enabled,
                "configPath": "config/llm.json",
                "optional": True,
                "networkCalls": "disabled",
            }
        except Exception:
            llm = {
                "status": "NOT_CONFIGURED",
                "configured": False,
                "enabled": False,
                "source": "offline",
                "apiKeyConfigured": False,
                "visionEnabled": False,
                "configPath": "config/llm.json",
                "optional": True,
                "networkCalls": "disabled",
            }
        registry_status = "ready" if self.registry_path.is_file() else "not_initialized"
        portable_python = self.project_root / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"
        python_status = "ready" if portable_python.is_file() else "unavailable"
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION, "apiVersion": API_VERSION},
            "portableRuntime": {"status": python_status, "projectRoot": str(self.project_root)},
            "registry": {"status": registry_status, "path": "workspace/state/registry.duckdb"},
            "server": {"pid": self.server_pid, "instanceId": self.instance_id},
            "llm": llm,
        }

    def _ai_settings(self) -> ApiResponse:
        settings = load_runtime_ai_settings(self.project_root).public()
        settings["fileInsightEnabled"] = self.file_insight_policy.enabled()
        return ApiResponse(200, {"settings": settings})

    def _legacy_reset_workspace(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        confirmation = value.get("confirmation")
        if not isinstance(confirmation, str) or confirmation.strip() != "清空":
            raise ApiError("RESET_CONFIRMATION_REQUIRED", "请输入“清空”确认此危险操作。", 400)
        begin_reset = getattr(self.tasks, "begin_reset", None)
        end_reset = getattr(self.tasks, "end_reset", None)
        barrier_acquired = False

        def active_task_ids() -> list[str]:
            return []

        # Legacy synchronous callers use the same cancellation contract as the
        # live reset path below.
        if active_task_ids():
            raise ApiError(
                "ACTIVE_TASKS",
                "当前仍有处理任务运行，请先取消并等待任务结束。",
                409,
                retryable=True,
                category="RESET",
                stage="prepare",
            )
        try:
            if callable(begin_reset):
                try:
                    begin_reset()
                except TaskAdmissionError as exc:
                    raise ApiError(
                        "RESET_IN_PROGRESS",
                        "已有清空操作正在进行，请稍后重试。",
                        409,
                        retryable=True,
                        category="RESET",
                        stage="prepare",
                    ) from exc
                barrier_acquired = True
            self.request_shutdown(reset=True)
            # Check after the barrier as well as before entering the reset
            # service.  New process/analysis/report submissions now fail under
            # the same manager lock and cannot race this snapshot.
            active_tasks = active_task_ids()
            if active_tasks:
                raise WorkspaceResetError(
                    "ACTIVE_TASKS",
                    "当前仍有处理任务运行，请先取消并等待任务结束。",
                    stage="prepare",
                    diagnostic=f"active_tasks={len(active_tasks)}",
                )
            service = WorkspaceResetService(self.project_root, self.registry_path)
            # Do not close or recreate logger handlers here.  The running
            # server child owns the stdout file handle independently of Python
            # logging; the reset service preserves server.log for that reason.
            result = service.reset(active_task_ids=active_tasks)
            clear_finished = getattr(self.tasks, "clear_finished", None)
            if callable(clear_finished):
                result.setdefault("removed", {})["tasks"] = {"items": int(clear_finished())}
        except WorkspaceResetError as exc:
            diagnostic = _safe_exception_detail(exc.diagnostic or type(exc).__name__)
            self.logger.warning(
                "request %s workspace reset failed stage=%s code=%s diagnostic=%s completed=%s",
                request_id,
                exc.stage,
                exc.code,
                diagnostic,
                exc.completed_phases,
            )
            raise ApiError(
                exc.code,
                exc.message,
                409 if exc.code == "ACTIVE_TASKS" else 500,
                retryable=exc.code == "ACTIVE_TASKS",
                diagnostic=diagnostic or None,
                category="RESET",
                stage=exc.stage,
                details={"completedPhases": exc.completed_phases, "removed": exc.removed},
            ) from exc
        finally:
            if barrier_acquired and callable(end_reset):
                end_reset()
        return ApiResponse(200, result)

    def _reset_workspace(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        confirmation = value.get("confirmation")
        if not isinstance(confirmation, str) or confirmation.strip() != "\u6e05\u7a7a":
            raise ApiError("RESET_CONFIRMATION_REQUIRED", "Please enter 清空 to confirm this operation", 400)

        journal = WorkspaceResetJournal(self.workspace_root)
        current = journal.read()
        if current and str(current.get("result") or "") == "running":
            raise ApiError("RESET_IN_PROGRESS", "workspace reset is already in progress", 409, retryable=True, category="RESET", stage="prepare")
        begin_reset = getattr(self.tasks, "begin_reset", None)
        end_reset = getattr(self.tasks, "end_reset", None)
        deferred = False
        try:
            if callable(begin_reset):
                begin_reset()
            if self.server_pid is None and self.instance_id is None:
                self.request_shutdown(reset=True)
                result = WorkspaceResetService(self.project_root, self.registry_path).reset(active_task_ids=())
                clear_finished = getattr(self.tasks, "clear_finished", None)
                if callable(clear_finished):
                    result.setdefault("removed", {})["tasks"] = {"items": int(clear_finished())}
                return ApiResponse(200, result)
            journal.write(
                request_id=request_id,
                phase="accepted",
                result="running",
                requested_at=journal._now(),
                old_server_pid=self.server_pid,
            )
            command = [
                sys.executable,
                "-m",
                "dongjian.api.reset_helper",
                "--project-root",
                str(self.project_root),
                "--request-id",
                request_id,
            ]
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)

            def after_response() -> None:
                try:
                    handler_completed_at = journal._now()
                    journal.write(
                        request_id=request_id,
                        phase="handler_completed",
                        result="running",
                        handler_completed_at=handler_completed_at,
                        stage="supervisor_start",
                    )
                    helper = subprocess.Popen(
                        command,
                        cwd=str(self.project_root),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
                except OSError as exc:
                    detail = f"{type(exc).__name__}:helper_start_failed"
                    self.logger.error("request %s reset helper start failed: %s", request_id, detail)
                    journal.write(
                        request_id=request_id,
                        phase="failed",
                        result="failed",
                        finished_at=journal._now(),
                        stage="after_response_flush",
                        target_category="state",
                        exception_type=type(exc).__name__,
                        safe_error="helper_start_failed",
                        safe_detail=detail,
                        old_server_pid=self.server_pid,
                    )
                    if callable(end_reset):
                        end_reset()

            def on_response_failure() -> None:
                journal.write(
                    request_id=request_id,
                    phase="failed",
                    result="failed",
                    finished_at=journal._now(),
                    stage="response_flush",
                    target_category="state",
                    exception_type="ResponseNotSent",
                    safe_error="response_not_sent",
                    safe_detail="response_not_sent",
                )
                if callable(end_reset):
                    end_reset()

            deferred = True
            return ApiResponse(
                202,
                {
                    "accepted": True,
                    "restarting": True,
                    "requestId": request_id,
                    "statusUrl": f"/api/v1/workspace/reset?requestId={request_id}",
                },
                after_response=after_response,
                on_response_failure=on_response_failure,
            )
        except TaskAdmissionError as exc:
            raise ApiError("RESET_IN_PROGRESS", "workspace reset is already in progress", 409, retryable=True, category="RESET", stage="prepare") from exc
        except WorkspaceResetError as exc:
            raise ApiError(
                exc.code,
                exc.message,
                409 if exc.code == "ACTIVE_TASKS" else 500,
                retryable=exc.code == "ACTIVE_TASKS",
                diagnostic=exc.diagnostic,
                category="RESET",
                stage=exc.stage,
                details={"completedPhases": exc.completed_phases, "removed": exc.removed},
            ) from exc
        except OSError as exc:
            journal.write(
                request_id=request_id,
                phase="failed",
                result="failed",
                finished_at=journal._now(),
                stage="prepare",
                target_category="state",
                exception_type=type(exc).__name__,
                safe_error="helper_start_failed",
                safe_detail="helper_start_failed",
            )
            raise ApiError("RESET_HELPER_START_FAILED", "reset helper could not be started", 500, category="RESET", stage="prepare") from exc
        finally:
            if callable(end_reset) and not deferred:
                end_reset()

    def _reset_status(self, query: Mapping[str, list[str]]) -> ApiResponse:
        request_id = query.get("requestId", [None])[0]
        value = WorkspaceResetJournal(self.workspace_root).read(request_id)
        if value is None:
            raise ApiError("RESET_NOT_FOUND", "reset request was not found", 404)
        return ApiResponse(200, value)

    def _save_ai_settings(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        with self._settings_lock:
            store = ProjectAIConfigStore(self.project_root)
            try:
                saved = store.save(
                    base_url=value.get("baseUrl", value.get("base_url", "")),
                    model=value.get("model", ""),
                    timeout=value.get("timeout", value.get("timeoutSeconds", 120)),
                    api_key=value.get("apiKey", value.get("api_key")),
                    vision_enabled=value.get("visionEnabled", value.get("vision_enabled", False)),
                    clear_api_key=bool(value.get("clearApiKey", False)),
                )
            except AISettingsError as exc:
                diagnostic = _safe_exception_detail(getattr(exc, "technical_detail", None) or type(exc).__name__)
                self.logger.warning(
                    "request %s AI settings save failed root=%s path=%s stage=%s code=%s exception=%s",
                    request_id,
                    self.project_root,
                    store.path,
                    getattr(exc, "stage", "validation"),
                    getattr(exc, "code", "CONFIG_INVALID"),
                    diagnostic,
                )
                code = getattr(exc, "code", "CONFIG_INVALID")
                user_messages = {
                    "CONFIG_DIRECTORY_UNAVAILABLE": "配置目录不可用，无法保存 AI 配置。",
                    "CONFIG_PERMISSION_DENIED": "没有权限写入 AI 配置，请检查项目目录权限。",
                    "CONFIG_REPLACE_FAILED": "配置文件替换失败，原有配置仍保留。",
                    "CONFIG_INVALID": "AI 模型配置不完整或无效，请检查后保存。",
                    "PROJECT_ROOT_MISMATCH": "项目路径校验失败，无法保存 AI 配置。",
                    "CONFIG_WRITE_FAILED": "AI 配置写入失败，原有配置仍保留。",
                }
                raise ApiError(
                    code,
                    user_messages.get(code, "AI 配置保存失败，原有配置仍保留。"),
                    400 if code == "CONFIG_INVALID" else 500,
                    diagnostic=diagnostic or None,
                    category=code,
                    stage=getattr(exc, "stage", "validation"),
                ) from exc
            except OSError as exc:
                diagnostic = _safe_exception_detail(type(exc).__name__)
                self.logger.warning(
                    "request %s AI settings save failed root=%s path=%s stage=write exception=%s",
                    request_id,
                    self.project_root,
                    store.path,
                    diagnostic,
                )
                raise ApiError(
                    "CONFIG_WRITE_FAILED",
                    "AI 配置写入失败，原有配置仍保留。",
                    500,
                    diagnostic=diagnostic,
                    category="CONFIG_WRITE_FAILED",
                    stage="write",
                ) from exc
        if "fileInsightEnabled" in value or "autoFileInsight" in value:
            self.file_insight_policy.save(bool(value.get("fileInsightEnabled", value.get("autoFileInsight", False))))
        settings = load_runtime_ai_settings(self.project_root).public()
        settings["fileInsightEnabled"] = self.file_insight_policy.enabled()
        return ApiResponse(200, {"settings": settings, "saved": True})

    @staticmethod
    def _temporary_ai_config(runtime: Any, value: Mapping[str, Any]) -> SemanticConfig:
        """Overlay a draft on runtime settings without writing any store."""

        def text(name: str, fallback: str) -> str:
            candidate = value.get(name, fallback)
            if not isinstance(candidate, str):
                raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409)
            return candidate.strip()

        base_url = text("baseUrl", text("base_url", runtime.config.base_url))
        model = text("model", runtime.config.model)
        clear_api_key = bool(value.get("clearApiKey", False))
        api_key_value = value.get("apiKey", value.get("api_key"))
        if clear_api_key:
            api_key = ""
        elif isinstance(api_key_value, str) and api_key_value.strip():
            api_key = api_key_value.strip()
        else:
            # The browser deliberately never receives the persisted secret.
            # An empty draft key therefore means "use the existing key".
            api_key = runtime.config.api_key
        timeout_value = value.get("timeout", value.get("timeoutSeconds", runtime.config.timeout_seconds))
        try:
            timeout = int(timeout_value)
        except (TypeError, ValueError) as exc:
            raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409) from exc
        if timeout < 1 or timeout > 600:
            raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409)
        vision_enabled = value.get("visionEnabled", value.get("vision_enabled", runtime.config.vision_enabled))
        if not isinstance(vision_enabled, bool):
            raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409)
        config = SemanticConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout,
            max_retries=0,
            vision_enabled=vision_enabled,
            config_version=runtime.config.config_version,
        )
        try:
            config.validate_for_use()
        except ValueError as exc:
            raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409) from exc
        return config

    def _test_ai_connection(self, body: bytes, request_id: str) -> ApiResponse:
        value = self._body_object(body) if body else {}
        with self._settings_lock:
            runtime = load_runtime_ai_settings(self.project_root)
            try:
                config = self._temporary_ai_config(runtime, value)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409) from exc
            # Connection testing has a deliberately short, single-attempt
            # policy. It must not inherit semantic/analysis retry budgets.
            # Respect an explicitly configured short timeout for deterministic
            # local tests, but cap a connection probe independently from the
            # normal 120-second semantic/analysis budget.
            probe_config = replace(config, timeout_seconds=min(max(config.timeout_seconds, 1), 20), max_retries=0)
            provider = self._semantic_provider_override
            if provider is None:
                provider = provider_for_name("openai-compatible", probe_config, allow_real_provider=True)
            test_request = SemanticRequest(
                asset_id="dongjian-settings-connection-test",
                asset_type="text",
                model=probe_config.model,
                prompt_version="settings-connection-v2",
                config_version=paths.SEMANTIC_CONFIG_VERSION,
                normalized_artifact_identity="0" * 64,
                instructions="Reply briefly to confirm that this OpenAI-compatible chat completion endpoint is reachable. Do not require JSON formatting.",
                reference_data={"test": "DongJian connection check"},
                output_contract="connection probe; no project data",
                structured_output_required=False,
            )
            try:
                response = provider.generate(test_request)
                if not isinstance(response, SemanticResponse):
                    raise SemanticProviderError("AI provider returned an invalid response", code="invalid_response")
            except SemanticProviderError as exc:
                retryable = bool(getattr(exc, "retryable", False))
                provider_code = str(getattr(exc, "code", "provider_error"))
                code, message, status = self._connection_failure(provider_code)
                category = self._connection_category(provider_code)
                diagnostic = _safe_exception_detail(getattr(exc, "diagnostic", str(exc)), config.api_key)
                raise ApiError(
                    code,
                    message,
                    status,
                    retryable=retryable,
                    diagnostic=diagnostic or None,
                    category=category,
                ) from exc
            except (TimeoutError, socket.timeout) as exc:
                raise ApiError(
                    "TIMEOUT",
                    "连接超时，请稍后重试。",
                    504,
                    retryable=True,
                    diagnostic="connection probe timed out",
                    category="TIMEOUT",
                ) from exc
            except Exception as exc:
                diagnostic = _safe_exception_detail(exc, config.api_key)
                raise ApiError(
                    "CONNECTION_FAILED",
                    "无法连接到模型服务，请检查地址和本机网络。",
                    503,
                    retryable=True,
                    diagnostic=diagnostic or None,
                    category="CONNECTION_FAILED",
                ) from exc
        structured_output_ok = getattr(response, "structured_output_ok", None)
        if structured_output_ok is None:
            structured_output_ok = isinstance(response.payload, Mapping)
        return ApiResponse(
            200,
            {
                "settings": runtime.public(),
                "status": "configured",
                "connectionStatus": "CONNECTED",
                "connectionOk": True,
                "structuredOutputOk": bool(structured_output_ok),
                "requestId": request_id,
            },
        )

    @staticmethod
    def _connection_failure(error_code: str) -> tuple[str, str, int]:
        """Map provider-neutral transport/protocol errors to Settings UX codes."""

        normalized = error_code.casefold()
        if normalized in {"timeout", "timed_out"}:
            return "TIMEOUT", "连接超时，请稍后重试。", 504
        if normalized in {"http_401", "http_403", "auth_failed", "authentication_failed"}:
            return "AUTH_FAILED", "API Key 无效或未被模型服务接受。", 401
        if normalized in {"http_400", "bad_request", "invalid_request"}:
            return "BAD_REQUEST", "模型服务拒绝了连接测试请求，请检查 Base URL、模型或服务参数。", 400
        if normalized in {"http_429", "rate_limited", "too_many_requests"}:
            return "RATE_LIMITED", "模型服务暂时限流，请稍后重试。", 429
        if normalized in {"http_404", "endpoint_not_found", "not_found"}:
            return "ENDPOINT_NOT_FOUND", "接口地址不存在，请检查 Base URL。", 404
        if normalized in {"model_not_found", "http_400_model", "model_missing"}:
            return "MODEL_NOT_FOUND", "模型名称不存在或当前服务未提供该模型。", 404
        if normalized in {"invalid_response", "malformed_json", "invalid_response_headers", "response_too_large"}:
            return "INVALID_RESPONSE", "模型服务返回格式不兼容。", 502
        if normalized.startswith("http_4"):
            return "CONNECTION_FAILED", "无法完成模型连接，请检查接口地址和配置。", 502
        if normalized in {"connection_error", "provider_error"} or normalized.startswith("http_5"):
            return "CONNECTION_FAILED", "无法连接到模型服务，请检查地址和本机网络。", 503
        return "CONNECTION_FAILED", "无法连接到模型服务，请检查地址和配置。", 503

    @staticmethod
    def _connection_category(error_code: str) -> str:
        normalized = error_code.casefold()
        if normalized in {"timeout", "timed_out"}:
            return "TIMEOUT"
        if normalized in {"http_401", "http_403", "auth_failed", "authentication_failed"}:
            return "AUTH_FAILED"
        if normalized in {"http_400", "bad_request", "invalid_request"}:
            return "BAD_REQUEST"
        if normalized in {"http_429", "rate_limited", "too_many_requests"}:
            return "RATE_LIMITED"
        if normalized in {"http_404", "endpoint_not_found", "not_found", "model_not_found", "http_400_model", "model_missing"}:
            return "ENDPOINT_OR_MODEL_NOT_FOUND"
        if normalized in {"invalid_response", "malformed_json", "invalid_response_headers", "response_too_large"}:
            return "INVALID_RESPONSE"
        if normalized in {"connection_error", "provider_error"} or normalized.startswith("http_5"):
            return "CONNECTION_FAILED"
        return "CONNECTION_FAILED"

    @staticmethod
    def _semantic_failure(error_code: str) -> tuple[str, str, int]:
        normalized = error_code.casefold()
        if normalized == "timeout":
            return "SEMANTIC_TIMEOUT", "AI 请求超时，请稍后重试。", 504
        if normalized in {"connection_error", "http_408", "http_429"} or normalized.startswith("http_5"):
            return "SEMANTIC_UNAVAILABLE", "AI 服务暂时不可用，请稍后重试。", 503
        if normalized in {"http_401", "http_403"}:
            return "SEMANTIC_AUTH_FAILED", "AI 服务未接受当前认证信息，请检查配置。", 502
        if normalized in {"malformed_json", "semantic_validation_failed"}:
            return "SEMANTIC_INVALID_RESPONSE", "AI 服务返回的内容无法识别，请稍后重试。", 502
        return "SEMANTIC_PROVIDER_ERROR", "AI 整理请求未完成，请查看技术详情后重试。", 502

    def _semantic_enrich(self, asset_id: str, body: bytes, *, request_id: str) -> ApiResponse:
        """Run exactly one user-triggered semantic request and publish its result."""

        try:
            runtime = load_runtime_ai_settings(self.project_root)
            config = runtime.config
            config.validate_for_use()
        except Exception:
            # Do not echo configuration details. In particular, this endpoint
            # never returns or logs the API key from .env.
            raise ApiError(
                "SEMANTIC_NOT_CONFIGURED",
                "尚未配置可用的 AI 模型。",
                409,
            )

        if self._semantic_provider_override is None and not runtime.enabled:
            if runtime.status == "UNVERIFIED":
                raise ApiError("SEMANTIC_NOT_VERIFIED", "请先在设置中测试 AI 连接，测试成功后才能整理资产。", 409)
            if runtime.status == "CONNECTION_FAILED":
                raise ApiError("SEMANTIC_NOT_VERIFIED", "AI 连接测试未成功，当前保持离线。", 409)
            raise ApiError("SEMANTIC_NOT_CONFIGURED", "尚未配置可用的 AI 模型。", 409)

        if body:
            value = self._body_object(body)
            if value:
                raise ApiError(
                    "SEMANTIC_FORCE_NOT_ALLOWED",
                    "资产整理不接受额外参数；强制重跑仅保留给命令行。",
                )

        detail = self.catalog.asset_detail(asset_id)
        if detail is None:
            raise ApiError("asset_not_found", "asset was not found", 404)
        asset_type = str(detail.get("assetType") or "")
        if asset_type not in {"table", "text"}:
            raise ApiError(
                "SEMANTIC_ASSET_NOT_SUPPORTED",
                "AI 整理目前只支持表格和文本资产。",
                409,
            )

        with self._semantic_lock, registry_write_mutex(self.registry_path):
            provider = self._semantic_provider_override
            if provider is None:
                provider = provider_for_name(
                    "openai-compatible",
                    config,
                    allow_real_provider=True,
                )
            registry = None
            try:
                registry = Registry.open(self.registry_path, initialize=False)
                summary = SemanticRunner(
                    registry,
                    provider,
                    workspace_root=self.workspace_root,
                    allow_real_provider=True,
                ).enrich(asset_id=asset_id, asset_type=asset_type)
            except ApiError:
                raise
            except Exception as exc:
                # The response must remain a stable, secret-free API error;
                # provider details are retained only in the semantic run.
                self.logger.error(
                    "request %s semantic provider failed: %s",
                    request_id,
                    _safe_exception_detail(exc, getattr(config, "api_key", "")),
                )
                raise ApiError(
                    "SEMANTIC_PROVIDER_ERROR",
                    "AI 整理请求未完成，请稍后重试。",
                    502,
                ) from exc
            finally:
                if registry is not None:
                    registry.close()

        if summary.failed:
            failure = summary.failures[0] if summary.failures else {}
            code, message, status = self._semantic_failure(str(failure.get("error_code") or ""))
            raise ApiError(code, message, status)
        if not summary.enriched and not summary.reused:
            raise ApiError("SEMANTIC_PROVIDER_ERROR", "AI 整理没有产生结果，请稍后重试。", 502)

        refreshed = self.catalog.asset_detail(asset_id)
        if refreshed is None:
            raise ApiError("asset_not_found", "asset was not found", 404)
        return ApiResponse(
            200,
            {
                "assetId": asset_id,
                "status": "reused" if summary.reused else "enriched",
                "reused": bool(summary.reused),
                "providerCalls": summary.provider_calls,
                "asset": refreshed,
            },
        )

    @staticmethod
    def _body_object(body: bytes) -> dict[str, Any]:
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise ApiError("request_too_large", "request body exceeds the local limit", 413)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("invalid_json", "request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ApiError("invalid_json", "request body must be a JSON object")
        return value

    def _analysis_provider(self) -> object:
        """Resolve the one project-local provider without exposing credentials."""

        if self._semantic_provider_override is not None:
            return self._semantic_provider_override
        try:
            runtime = load_runtime_ai_settings(self.project_root)
        except Exception as exc:
            raise ApiError("MODEL_NOT_CONFIGURED", "AI 模型未配置，当前保持完全离线。", 409) from exc
        if runtime.status in {"NOT_CONFIGURED", "INCOMPLETE", "INVALID_CONFIGURATION"} or not runtime.configured:
            raise ApiError("MODEL_NOT_CONFIGURED", "AI 模型未配置，当前保持完全离线。", 409)
        if not runtime.enabled:
            raise ApiError(
                "MODEL_UNVERIFIED",
                "AI 模型已配置但尚未验证可用，请先完成连接测试。",
                409,
                retryable=True,
            )
        try:
            runtime.config.validate_for_use()
            return provider_for_name("openai-compatible", runtime.config, allow_real_provider=True)
        except Exception as exc:
            raise ApiError("MODEL_NOT_CONFIGURED", "AI 模型配置不可用，请检查项目内 config/llm.json。", 409) from exc

    def _start_analysis(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        try:
            question, scope, asset_ids = normalize_analysis_request(
                value.get("question"),
                scope=value.get("scope", "all"),
                asset_ids=value.get("assetIds", value.get("asset_ids")),
            )
        except AnalysisExecutionError as exc:
            raise ApiError(exc.code, exc.message) from exc
        provider = self._analysis_provider()
        run_id = new_analysis_run_id()
        orchestrator = AnalysisOrchestrator(
            self.analysis,
            provider,
            workspace_root=self.workspace_root,
            run_store=self.analysis_runs,
            logger=self.logger,
        )
        pending = orchestrator.new_run_record(question, scope=scope, asset_ids=asset_ids, run_id=run_id)

        def runner(progress: object, cancel_event: object) -> dict[str, object]:
            # Persist only after the task manager has admitted the runner.  A
            # reset barrier can therefore reject this request without first
            # creating an analysis artifact that the reset would race.
            self.analysis_runs.write(pending)
            return orchestrator.run(
                question,
                scope=scope,
                asset_ids=asset_ids,
                run_id=run_id,
                progress_callback=progress if callable(progress) else None,
                cancel_event=cancel_event if hasattr(cancel_event, "is_set") else None,
            )

        try:
            task = self.tasks.submit_analysis(
                run_id,
                runner,
                provider=provider,
                request_id=request_id,
                max_steps=MAX_ANALYSIS_STEPS,
            )
        except TaskAdmissionError as exc:
            raise ApiError("TASKS_STOPPING", "应用正在停止，暂时不能开始 AI 分析。", 503, retryable=True) from exc
        return ApiResponse(
            202,
            {"analysisRunId": run_id, "taskId": task.task_id, "task": task.public_dict()},
        )

    def _report_provider(self) -> object | None:
        """Resolve the configured provider, or None for offline fallback."""

        if self._semantic_provider_override is not None:
            return self._semantic_provider_override
        try:
            runtime = load_runtime_ai_settings(self.project_root)
            if not runtime.configured or not runtime.enabled:
                return None
            runtime.config.validate_for_use()
            report_config = replace(
                runtime.config,
                timeout_seconds=min(max(1, int(runtime.config.timeout_seconds)), 45),
                max_retries=0,
            )
            return provider_for_name("openai-compatible", report_config, allow_real_provider=True)
        except Exception:
            # Report generation remains useful offline.  The sanitized report
            # artifact records the fallback mode without configuration data.
            return None

    def _start_report(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        run_ids = value.get("analysisRunIds", value.get("sourceAnalysisRunIds", value.get("analysis_run_ids")))
        file_ids = value.get("fileIds", value.get("file_ids")) if "fileIds" in value or "file_ids" in value else None
        title = value.get("title", "")
        purpose = value.get("purpose", value.get("description", ""))
        report_type = value.get("reportType", value.get("report_type", "analysis"))
        if report_type not in {"overview", "analysis"}:
            raise ApiError("REPORT_INPUT_INVALID", "reportType must be overview or analysis", 400)
        select_all = bool(value.get("selectAllCurrentFilter", value.get("select_all_current_filter", False)))
        filters = value.get("filters") if isinstance(value.get("filters"), Mapping) else {}
        provider = self._report_provider()
        composer = ReportComposer(
            self.analysis_runs,
            self.workspace_root,
            provider=provider,
            report_store=self.reports,
        )
        if run_ids is None and file_ids is None and not select_all:
            raise ApiError("REPORT_INPUT_INVALID", "请选择资料后再生成报告", 400)
        if run_ids is not None:
            try:
                composer.validate_inputs(run_ids)
            except ReportExecutionError as exc:
                raise ApiError(exc.code, exc.message, 400, retryable=exc.retryable) from exc
        else:
            if select_all:
                processing_filter = str(filters.get("processingStatus") or "")
                insight_filter = str(filters.get("fileInsightStatus") or "")

                def matches_file_filters(item: object) -> bool:
                    if not isinstance(item, Mapping):
                        return False
                    return (
                        (not processing_filter or str(item.get("processingStatus") or "") == processing_filter)
                        and (not insight_filter or str(item.get("fileInsightStatus") or "") == insight_filter)
                    )

                catalog_page = self.catalog.list_files(
                    category=filters.get("category"),
                    quality_status=filters.get("quality"),
                    source_format=filters.get("format"),
                    query=filters.get("query"),
                    limit=100,
                    offset=0,
                )
                selected_all = [item for item in catalog_page.get("items", []) if matches_file_filters(item)] if isinstance(catalog_page, Mapping) and isinstance(catalog_page.get("items"), list) else []
                catalog_offset = len(catalog_page.get("items", [])) if isinstance(catalog_page, Mapping) and isinstance(catalog_page.get("items"), list) else 0
                while isinstance(catalog_page, Mapping) and catalog_page.get("pagination", {}).get("hasNext") and len(selected_all) < 10_000:
                    catalog_page = self.catalog.list_files(
                        category=filters.get("category"),
                        quality_status=filters.get("quality"),
                        source_format=filters.get("format"),
                        query=filters.get("query"),
                        limit=100,
                        offset=catalog_offset,
                    )
                    if isinstance(catalog_page.get("items"), list):
                        selected_all.extend(item for item in catalog_page["items"] if matches_file_filters(item))
                        catalog_offset += len(catalog_page["items"])
                file_ids = [str(item.get("fileId")) for item in selected_all if isinstance(item, Mapping) and item.get("fileId")]
            if not isinstance(file_ids, list) or not file_ids or len(file_ids) > 10_000 or any(not isinstance(item, str) or not item.strip() for item in file_ids):
                raise ApiError("REPORT_INPUT_INVALID", "fileIds is invalid", 400)

        def internal_analysis_run(selected_file_ids: list[str]) -> str:
            registry = Registry.open_reader(self.registry_path)
            try:
                placeholders = ",".join("?" for _ in selected_file_ids)
                cursor = registry.connection.execute(
                    f"SELECT file_id FROM files WHERE current_presence_state='present' AND file_id IN ({placeholders})",
                    selected_file_ids,
                )
                present = [str(row[0]) for row in cursor.fetchall()]
            finally:
                registry.close()
            if not present:
                raise ReportExecutionError("REPORT_INPUT_INVALID", "没有可用于报告的已处理文件", stage="loading_analysis")
            evidence_manifest: dict[str, Any] = {}
            source_assets: list[str] = []
            table_assets: list[str] = []
            for file_id in present[:1_024]:
                content = self.catalog.file_content(file_id)
                if not isinstance(content, Mapping):
                    continue
                source = content.get("source") if isinstance(content.get("source"), Mapping) else {}
                insight = self.file_insights.status(file_id)
                insight_value = insight.get("insight") if isinstance(insight, Mapping) else None
                if isinstance(insight_value, Mapping) and insight_value.get("summary"):
                    evidence_id = f"file-insight:{file_id}"
                    evidence_manifest[evidence_id] = {
                        "kind": "file_insight",
                        "asset_id": file_id,
                        "display_name": source.get("relativePath"),
                        "source": source,
                        "text": str(insight_value.get("summary"))[:2_000],
                    }
                for section in content.get("sections", []) if isinstance(content.get("sections"), list) else []:
                    if not isinstance(section, Mapping):
                        continue
                    for block in section.get("blocks", []) if isinstance(section.get("blocks"), list) else []:
                        if not isinstance(block, Mapping):
                            continue
                        asset_id = str(block.get("assetId") or "")
                        table_block = block.get("type") == "table"
                        trusted_table = not table_block or is_table_trusted_for_analysis(block)
                        if asset_id and asset_id not in source_assets and (not table_block or trusted_table):
                            source_assets.append(asset_id)
                        if table_block and trusted_table and asset_id and asset_id not in table_assets:
                            table_assets.append(asset_id)
                        evidence_id = f"file-content:{file_id}:{asset_id}:{len(evidence_manifest)}"
                        provenance = block.get("provenance") if isinstance(block.get("provenance"), Mapping) else {}
                        preview = block.get("preview") if isinstance(block.get("preview"), Mapping) else {}
                        raw_columns = [str(item) for item in preview.get("columns", [])] if isinstance(preview.get("columns"), list) else []
                        display_columns = [str(item) for item in preview.get("presentationColumns", [])] if isinstance(preview.get("presentationColumns"), list) else []
                        evidence_columns = display_columns if len(display_columns) == len(raw_columns) else raw_columns
                        raw_rows = preview.get("rows", []) if isinstance(preview.get("rows"), list) else []
                        evidence_rows = [
                            {evidence_columns[index]: row.get(raw_columns[index]) for index in range(len(raw_columns))}
                            for row in raw_rows
                            if isinstance(row, Mapping)
                        ] if evidence_columns else []
                        pagination = preview.get("pagination") if isinstance(preview.get("pagination"), Mapping) else {}
                        row_count = pagination.get("total")
                        if not isinstance(row_count, int):
                            row_count = len(evidence_rows)
                        evidence_manifest[evidence_id] = {
                            "kind": "table" if not table_block or trusted_table else "table_candidate",
                            "asset_id": asset_id or file_id,
                            "asset_type": "table" if block.get("type") == "table" else "text",
                            "display_name": source.get("relativePath"),
                            "source": {**dict(source), "pageNumber": block.get("pageNumber"), "sheetName": block.get("sheetName")},
                            "columns": evidence_columns if trusted_table else [],
                            "rows": evidence_rows if trusted_table else [],
                            "row_count": row_count if trusted_table else 0,
                            "truncated": bool(pagination.get("hasNext")) if trusted_table else False,
                            "provenance": provenance,
                            "trust_level": "CONFIRMED_STRUCTURE" if trusted_table else "CANDIDATE_ONLY",
                            "text": block.get("text") if not table_block else (None if trusted_table else "候选表格，需对照原始来源核验。"),
                        }
                        if len(evidence_manifest) >= 128:
                            break
                    if len(evidence_manifest) >= 128:
                        break
            run_id = new_analysis_run_id()
            analysis_steps: list[dict[str, object]] = []
            analysis_sql: list[dict[str, object]] = []
            analysis_findings: list[dict[str, object]] = []
            analysis_limitations: list[str] = []
            analysis_answer = f"已将 {len(present)} 个已处理文件及其可用的 FileInsight/本地内容纳入报告范围。"

            if report_type == "analysis":
                table_asset_ids = table_assets[:8]
                analysis_asset_ids = list(dict.fromkeys(table_asset_ids + source_assets))[:8]
                context_assets: list[Mapping[str, object]] = []
                if analysis_asset_ids:
                    try:
                        context_result = self.analysis.asset_context(analysis_asset_ids)
                    except AnalysisServiceError:
                        analysis_limitations.append("本地资产上下文无法完整读取，因此仅保留文件证据。")
                    else:
                        raw_assets = context_result.get("assets", []) if isinstance(context_result, Mapping) else []
                        context_assets = [item for item in raw_assets if isinstance(item, Mapping)]
                        for context_asset in context_assets:
                            asset_id = str(context_asset.get("assetId") or "")
                            if not asset_id:
                                continue
                            dimensions = context_asset.get("dimensions") if isinstance(context_asset.get("dimensions"), Mapping) else {}
                            row_count = context_asset.get("rowCount", dimensions.get("rows"))
                            display_name = str(context_asset.get("displayName") or asset_id)
                            evidence_manifest[f"analysis-context:{asset_id}"] = {
                                "kind": "analysis_context",
                                "asset_id": asset_id,
                                "asset_type": context_asset.get("assetType"),
                                "display_name": display_name,
                                "source": context_asset.get("source") if isinstance(context_asset.get("source"), Mapping) else {},
                                "profile": context_asset.get("profiling") if isinstance(context_asset.get("profiling"), Mapping) else {},
                                "text": f"本地分析上下文：{display_name}；类型：{context_asset.get('assetType') or 'unknown'}；行数：{row_count if row_count is not None else '未知'}。",
                            }
                        analysis_steps.append({"step": 1, "action": "context", "asset_ids": analysis_asset_ids, "result": len(context_assets)})

                def quote_column(value: object) -> str:
                    return '"' + str(value).replace('"', '""') + '"'

                analysis_step = len(analysis_steps)

                def add_sql_result(sql_text: str, asset_ids: list[str], label: str) -> dict[str, object] | None:
                    nonlocal analysis_step
                    try:
                        raw_result = self.analysis.safe_sql(asset_ids, sql_text)
                    except AnalysisServiceError:
                        analysis_limitations.append(f"{label}未能在本地 Safe SQL 边界内完成。")
                        return None
                    analysis_step += 1
                    evidence_id = f"sql:{run_id}:{analysis_step}"
                    rows = raw_result.get("rows", []) if isinstance(raw_result, Mapping) and isinstance(raw_result.get("rows"), list) else []
                    columns = raw_result.get("columns", []) if isinstance(raw_result, Mapping) and isinstance(raw_result.get("columns"), list) else []
                    value = {
                        "kind": "sql_result",
                        "asset_id": asset_ids[0] if len(asset_ids) == 1 else None,
                        "asset_ids": list(asset_ids),
                        "asset_type": "table",
                        "display_name": label,
                        "source": {"relativePath": label, "format": "local-analysis"},
                        "sql": sql_text,
                        "columns": [str(item) for item in columns[:256]],
                        "rows": rows[:500],
                        "row_count": int(raw_result.get("rowCount", len(rows))) if isinstance(raw_result, Mapping) else len(rows),
                        "truncated": bool(raw_result.get("truncated")) if isinstance(raw_result, Mapping) else False,
                        "execution_ms": raw_result.get("executionMs") if isinstance(raw_result, Mapping) else None,
                    }
                    evidence_manifest[evidence_id] = value
                    result = {
                        "step": analysis_step,
                        "action": "sql",
                        "asset_ids": list(asset_ids),
                        "sql": sql_text,
                        "evidence_id": evidence_id,
                        "columns": value["columns"],
                        "row_count": value["row_count"],
                        "truncated": value["truncated"],
                        "execution_ms": value["execution_ms"],
                    }
                    analysis_sql.append(result)
                    analysis_steps.append({"step": analysis_step, "action": "sql", "asset_ids": list(asset_ids), "sql": sql_text, "evidence_ids": [evidence_id], "result": value["row_count"]})
                    return {"evidence_id": evidence_id, "rows": rows, "columns": value["columns"]}

                if table_asset_ids:
                    count_query = " UNION ALL ".join(
                        f"SELECT {index} AS relation_index, COUNT(*) AS row_count FROM t{index}"
                        for index, _asset_id in enumerate(table_asset_ids, start=1)
                    )
                    count_result = add_sql_result(count_query, table_asset_ids, "表格行数统计")
                    count_rows = count_result.get("rows", []) if isinstance(count_result, Mapping) else []
                    count_evidence_id = str(count_result.get("evidence_id")) if isinstance(count_result, Mapping) else ""
                    table_context_assets = [item for item in context_assets if item.get("assetType") == "table"]
                    relation_names = {
                        f"t{index}": str(item.get("displayName") or item.get("assetId") or table_asset_ids[index - 1])
                        for index, item in enumerate(table_context_assets, start=1)
                        if index <= len(table_asset_ids)
                    }
                    count_fragments: list[str] = []
                    for row in count_rows if isinstance(count_rows, list) else []:
                        if not isinstance(row, Mapping):
                            continue
                        relation_index = row.get("relation_index")
                        try:
                            relation_label = relation_names.get(f"t{int(relation_index)}", f"表 {relation_index}")
                        except (TypeError, ValueError):
                            relation_label = "选定表格"
                        count_value = row.get("row_count")
                        count_fragments.append(f"{relation_label}：{count_value} 行")
                    if count_fragments:
                        analysis_answer = "已通过本地 Safe SQL 完成选定表格的真实行数统计：" + "；".join(count_fragments) + "。"
                        if count_evidence_id:
                            analysis_findings.append({"statement": "；".join(count_fragments), "evidence_ids": [count_evidence_id]})

                    first_table = table_context_assets[0] if table_context_assets else None
                    schema_columns = first_table.get("schema", {}).get("columns", []) if isinstance(first_table, Mapping) and isinstance(first_table.get("schema"), Mapping) else []
                    schema_columns = [item for item in schema_columns if isinstance(item, Mapping) and item.get("name")]
                    first_table_id = table_asset_ids[0]
                    numeric = next((item for item in schema_columns if any(token in str(item.get("physicalType") or "").casefold() for token in ("int", "uint", "float", "double", "decimal", "numeric"))), None)
                    temporal = next((item for item in schema_columns if any(token in str(item.get("physicalType") or "").casefold() for token in ("date", "time"))), None)
                    categorical = next((item for item in schema_columns if "char" in str(item.get("physicalType") or "").casefold() or "string" in str(item.get("physicalType") or "").casefold()), None)
                    if numeric:
                        numeric_name = quote_column(numeric.get("name"))
                        numeric_result = add_sql_result(
                            f"SELECT MIN({numeric_name}) AS minimum, MAX({numeric_name}) AS maximum, AVG({numeric_name}) AS average, STDDEV_SAMP({numeric_name}) AS standard_deviation FROM t1",
                            [first_table_id],
                            f"数值字段 {numeric.get('name')} 统计",
                        )
                        if isinstance(numeric_result, Mapping) and numeric_result.get("rows"):
                            row = numeric_result["rows"][0]
                            if isinstance(row, Mapping):
                                evidence_id = str(numeric_result.get("evidence_id") or "")
                                analysis_findings.append({
                                    "statement": f"字段 {numeric.get('name')} 的最小值为 {row.get('minimum')}，最大值为 {row.get('maximum')}，平均值为 {row.get('average')}。",
                                    "evidence_ids": [evidence_id],
                                }) if evidence_id else None
                        anomaly_result = add_sql_result(
                            f"WITH stats AS (SELECT AVG({numeric_name}) AS mean, STDDEV_SAMP({numeric_name}) AS deviation FROM t1) SELECT COUNT(*) AS anomaly_count FROM t1 CROSS JOIN stats WHERE {numeric_name} IS NOT NULL AND stats.deviation IS NOT NULL AND ABS({numeric_name} - stats.mean) > 3 * stats.deviation",
                            [first_table_id],
                            f"数值字段 {numeric.get('name')} 异常统计",
                        )
                        if isinstance(anomaly_result, Mapping) and anomaly_result.get("rows"):
                            row = anomaly_result["rows"][0]
                            if isinstance(row, Mapping):
                                evidence_id = str(anomaly_result.get("evidence_id") or "")
                                analysis_findings.append({"statement": f"按三倍标准差规则，字段 {numeric.get('name')} 检测到 {row.get('anomaly_count')} 个异常值。", "evidence_ids": [evidence_id]}) if evidence_id else None
                    if categorical:
                        category_name = quote_column(categorical.get("name"))
                        distribution_result = add_sql_result(
                            f"SELECT {category_name} AS value, COUNT(*) AS frequency FROM t1 GROUP BY {category_name} ORDER BY frequency DESC LIMIT 10",
                            [first_table_id],
                            f"字段 {categorical.get('name')} 分布",
                        )
                        if isinstance(distribution_result, Mapping) and distribution_result.get("rows"):
                            first_row = distribution_result["rows"][0]
                            evidence_id = str(distribution_result.get("evidence_id") or "")
                            if isinstance(first_row, Mapping) and evidence_id:
                                analysis_findings.append({"statement": f"字段 {categorical.get('name')} 的最高频值为 {first_row.get('value')}，出现 {first_row.get('frequency')} 次。", "evidence_ids": [evidence_id]})
                    if temporal and numeric:
                        temporal_name = quote_column(temporal.get("name"))
                        trend_result = add_sql_result(
                            f"SELECT {temporal_name} AS period, COUNT(*) AS observations, AVG({numeric_name}) AS average FROM t1 GROUP BY {temporal_name} ORDER BY {temporal_name} LIMIT 50",
                            [first_table_id],
                            f"{temporal.get('name')} 与 {numeric.get('name')} 趋势",
                        )
                        if trend_result is None:
                            analysis_limitations.append("趋势统计未能完成。")
                    if not numeric and not categorical:
                        analysis_limitations.append("选定表格没有可识别的数值或分类字段，因此未生成分布、趋势或异常统计。")
                    elif not temporal:
                        analysis_limitations.append("未发现可识别的时间字段，因此未生成时间趋势统计。")
                else:
                    analysis_limitations.append("选定文件没有可用于 Safe SQL 的数据表，因此本次仅生成文件证据综述。")

            record = {
                "analysis_run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "question": str(purpose or "Workspace file report")[:2_000],
                "scope": {"kind": "selected", "asset_ids": source_assets[:1_024]},
                "scope_asset_ids": source_assets[:1_024],
                "model_identity": {"provider": "local-analysis", "model": "offline", "prompt_version": "report-file-scope-v2"},
                "answer": analysis_answer,
                "findings": analysis_findings, "unverified_findings": [], "limitations": analysis_limitations,
                "grounding_summary": {"grounded": len(analysis_findings), "unverified": 0},
                "evidence_manifest": evidence_manifest,
                "executed_safe_sql": analysis_sql, "source_asset_ids": source_assets[:1_024],
                "steps_used": len(analysis_steps), "max_steps": len(analysis_steps), "steps": analysis_steps, "provider_calls": 0, "error": None,
            }
            self.analysis_runs.write(record)
            return run_id

        report_id = new_report_id()

        def runner(progress: object, cancel_event: object) -> dict[str, object]:
            selected_run_ids = run_ids
            if selected_run_ids is None:
                selected = list(file_ids or [])
                if not selected and select_all:
                    registry = Registry.open_reader(self.registry_path)
                    try:
                        selected = [str(row[0]) for row in registry.connection.execute("SELECT file_id FROM files WHERE current_presence_state='present' ORDER BY relative_path LIMIT 1024").fetchall()]
                    finally:
                        registry.close()
                selected_run_ids = [internal_analysis_run(selected)]
            return composer.compose(
                selected_run_ids,
                report_id=report_id,
                title=title,
                purpose=purpose,
                report_type=str(report_type),
                progress_callback=progress if callable(progress) else None,
                cancel_event=cancel_event if hasattr(cancel_event, "is_set") else None,
            )

        try:
            task = self.tasks.submit_report(
                report_id,
                runner,
                provider=provider,
                request_id=request_id,
            )
        except TaskAdmissionError as exc:
            raise ApiError("TASKS_STOPPING", "应用正在停止，暂时不能生成报告。", 503, retryable=True) from exc
        return ApiResponse(
            202,
            {"reportId": report_id, "taskId": task.task_id, "task": task.public_dict()},
        )

    def _report_export(self, report_id: str, format_name: str) -> ApiResponse:
        try:
            record = self.reports.read(report_id)
        except ReportExecutionError as exc:
            raise ApiError("REPORT_PERSISTENCE_ERROR", exc.message, 500) from exc
        if record is None:
            raise ApiError("report_not_found", "report was not found", 404)
        if format_name == "markdown":
            return ApiResponse(200, {}, raw_body=render_report_markdown(record).encode("utf-8"), content_type="text/markdown; charset=utf-8")
        if format_name == "html":
            return ApiResponse(200, {}, raw_body=render_report_html(record).encode("utf-8"), content_type="text/html; charset=utf-8")
        raise ApiError("REPORT_FORMAT_INVALID", "report export format must be markdown or html")

    def _file_insight(self, file_id: str, method: str, body: bytes = b"", *, request_id: str) -> ApiResponse:
        status = self.file_insights.status(file_id)
        if status.get("status") == "not_found":
            raise ApiError("file_not_found", "file was not found", 404)
        if method == "GET":
            task_lookup = getattr(self.tasks, "file_insight_task", None)
            task = task_lookup(file_id) if callable(task_lookup) else None
            if task is not None and task.status in {"queued", "running", "cancelling"}:
                if task.status == "queued":
                    status["status"] = "queued"
                else:
                    phase = str((status.get("queue") or {}).get("phase") or "requesting_model") if isinstance(status.get("queue"), Mapping) else "requesting_model"
                    status["status"] = phase if phase in {"requesting_model", "validating", "persisting"} else "requesting_model"
                status["taskId"] = task.task_id
            return ApiResponse(200, status)
        provider = configured_file_insight_provider(
            self.project_root,
            override=self._semantic_provider_override,
        )
        if provider is None:
            raise ApiError("FILE_INSIGHT_NOT_CONFIGURED", "AI file understanding is not configured", 409)
        value = self._body_object(body) if body else {}
        force = bool(value.get("force", False))
        try:
            admitted = self.file_insights.enqueue_file(file_id, force=force)
        except FileInsightError as exc:
            raise ApiError("FILE_INSIGHT_NOT_READY", exc.message, 409) from exc
        active_lookup = getattr(self.tasks, "file_insight_task", None)
        active = active_lookup(file_id) if callable(active_lookup) else None
        if active is not None and active.status in {"queued", "running", "cancelling"}:
            return ApiResponse(202, {"fileId": file_id, "taskId": active.task_id, "task": active.public_dict()})
        if not admitted and not force:
            current = self.file_insights.status(file_id)
            return ApiResponse(200, current)
        try:
            detail = self.catalog.file_detail(file_id) or {}
            detail_file = detail.get("file") if isinstance(detail, Mapping) and isinstance(detail.get("file"), Mapping) else {}
            source = detail.get("source") if isinstance(detail, Mapping) and isinstance(detail.get("source"), Mapping) else {}
            display_name = str(detail_file.get("displayName") or source.get("relativePath") or file_id)
            task = self.tasks.submit_file_insight(
                file_id,
                display_name,
                self.file_insights,
                provider=provider,
                request_id=request_id,
                force=force,
            )
        except TaskAdmissionError as exc:
            raise ApiError("TASKS_STOPPING", "应用正在停止，暂时不能开始 AI 文件整理。", 503, retryable=True) from exc
        self._workspace_changed(file_id)
        return ApiResponse(202, {"fileId": file_id, "taskId": task.task_id, "task": task.public_dict()})

    def _reprocess_file(self, file_id: str, *, request_id: str) -> ApiResponse:
        detail = self.catalog.file_detail(file_id)
        if detail is None:
            raise ApiError("file_not_found", "file was not found", 404)
        source = detail.get("source") if isinstance(detail.get("source"), Mapping) else {}
        source_root = str(source.get("root") or "")
        relative_path = str(source.get("relativePath") or "")
        expected_sha256 = str(source.get("sha256") or "")
        if not source_root or not relative_path or not expected_sha256:
            raise ApiError("FILE_REPROCESS_IDENTITY_MISSING", "registered source identity is unavailable", 409)
        try:
            self.catalog.source_file(file_id)
            task = self.tasks.submit_file_reprocess(
                file_id,
                source_root,
                relative_path,
                expected_sha256,
                request_id=request_id,
            )
        except FileNotFoundError as exc:
            raise ApiError("SOURCE_NOT_FOUND", "registered source file is unavailable", 409) from exc
        except TaskAdmissionError as exc:
            raise ApiError("TASKS_STOPPING", "application is stopping and cannot start file reprocessing", 503, retryable=True) from exc
        return ApiResponse(202, {"fileId": file_id, "taskId": task.task_id, "task": task.public_dict()})

    def _file_insight_queue(self) -> ApiResponse:
        summary = self.file_insights.queue_summary()
        configured = configured_file_insight_provider(self.project_root, override=self._semantic_provider_override) is not None
        summary["configured"] = configured
        summary["disabled"] = 0
        summary["notStarted"] = int(summary.get("pendingReady", 0) or 0)
        summary["requestingModel"] = int(summary.get("requesting_model", 0) or 0)
        summary["validating"] = int(summary.get("validating", 0) or 0)
        summary["persisting"] = int(summary.get("persisting", 0) or 0)
        return ApiResponse(200, summary)

    def _bulk_file_insight(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body) if body else {}
        if not bool(value.get("confirmed", value.get("confirm", False))):
            preview = self.file_insights.bulk_preview()
            return ApiResponse(
                200,
                {
                    "confirmationRequired": True,
                    "pending": int(preview.get("pending", 0)),
                    "confirmationMessage": "将向当前配置的 AI 服务发送每个文件的有界整理上下文。",
                },
            )
        provider = configured_file_insight_provider(
            self.project_root,
            override=self._semantic_provider_override,
        )
        if provider is None:
            raise ApiError("FILE_INSIGHT_NOT_CONFIGURED", "AI file understanding is not configured", 409)
        try:
            admitted = self.file_insights.enqueue_ready_files()
        except FileInsightError as exc:
            raise ApiError("FILE_INSIGHT_NOT_READY", exc.message, 409) from exc
        queued = int(admitted.get("queued", 0) or 0)
        if queued <= 0:
            active = getattr(self.tasks, "file_insight_batch_task", lambda: None)()
            if active is not None and active.status in {"queued", "running", "cancelling"}:
                return ApiResponse(202, {**admitted, "taskId": active.task_id, "task": active.public_dict()})
            return ApiResponse(200, admitted)
        try:
            task = self.tasks.submit_file_insight_batch(
                self.file_insights,
                provider=provider,
                request_id=request_id,
                total=queued,
            )
        except TaskAdmissionError as exc:
            raise ApiError("TASKS_STOPPING", "应用正在停止，暂时不能开始 AI 文件整理。", 503, retryable=True) from exc
        for admitted_file_id in admitted.get("fileIds", []) if isinstance(admitted.get("fileIds"), list) else []:
            self._workspace_changed(str(admitted_file_id))
        return ApiResponse(202, {**admitted, "taskId": task.task_id, "task": task.public_dict()})

    def handle_api(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        body: bytes = b"",
        *,
        request_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        request_id = request_id or f"req_{uuid4().hex}"
        try:
            if path == "/api/v1/settings/ai" and method == "GET":
                return self._ai_settings()
            if path == "/api/v1/settings/ai" and method == "PUT":
                return self._save_ai_settings(body, request_id=request_id)
            if path == "/api/v1/settings/ai/test" and method == "POST":
                return self._test_ai_connection(body, request_id)
            if path == "/api/v1/workspace/reset" and method == "POST":
                return self._reset_workspace(body, request_id=request_id)
            if path == "/api/v1/workspace/reset" and method == "GET":
                return self._reset_status(query)
            if path == "/api/v1/health" and method == "GET":
                return ApiResponse(200, self._health())
            if path == "/api/v1/workspace/snapshot" and method == "GET":
                return ApiResponse(200, self.workspace_snapshot())
            if path == "/api/v1/overview" and method == "GET":
                return ApiResponse(200, self.catalog.overview())
            if path == "/api/v1/file-insights/queue" and method == "GET":
                return self._file_insight_queue()
            if path == "/api/v1/file-insights/bulk" and method == "POST":
                return self._bulk_file_insight(body, request_id=request_id)
            if path == "/api/v1/catalog" and method == "GET":
                limit = _int_param(query, "limit", 50, maximum=100)
                offset = _int_param(query, "offset", 0, maximum=10_000_000)
                view = query.get("view", [None])[0]
                # File aggregation is the product default.  Keep the old
                # asset view available to analysis/query screens and existing
                # API clients that explicitly filter by table/text.
                if view == "assets" or (view is None and query.get("type", [None])[0] is not None):
                    return ApiResponse(
                        200,
                        self.catalog.list_assets(
                            asset_type=query.get("type", [None])[0],
                            quality_status=query.get("quality", [None])[0],
                            source_format=query.get("format", [None])[0],
                            query=query.get("q", [None])[0],
                            limit=limit,
                            offset=offset,
                        ),
                    )
                return ApiResponse(
                    200,
                    self.catalog.list_files(
                        category=query.get("category", [None])[0],
                        quality_status=query.get("quality", [None])[0],
                        source_format=query.get("format", [None])[0],
                        query=query.get("q", [None])[0],
                        limit=limit,
                        offset=offset,
                        include_ignored=query.get("includeIgnored", ["false"])[0].casefold() == "true",
                    ),
                )
            if path == "/api/v1/search" and method == "GET":
                request = SearchQuery(
                    query=query.get("q", [""])[0],
                    file_id=query.get("file_id", [None])[0],
                    asset_type=query.get("type", ["all"])[0],
                    source_format=query.get("format", [None])[0],
                    quality_status=query.get("quality", [None])[0],
                    limit=_int_param(query, "limit", 30, maximum=100),
                    offset=_int_param(query, "offset", 0, maximum=10_000_000),
                    match="phrase",
                )
                try:
                    return ApiResponse(200, self.search.search(request).as_dict())
                except SearchValidationError as exc:
                    raise ApiError("invalid_search", str(exc)) from exc
            if path == "/api/v1/analysis/runs" and method == "POST":
                return self._start_analysis(body, request_id=request_id)
            if path == "/api/v1/analysis/runs" and method == "GET":
                limit = _int_param(query, "limit", 20, maximum=MAX_ANALYSIS_HISTORY_LIMIT)
                return ApiResponse(200, self.analysis_runs.list(limit=limit))
            if path == "/api/v1/reports" and method == "POST":
                return self._start_report(body, request_id=request_id)
            if path == "/api/v1/reports" and method == "GET":
                limit = _int_param(query, "limit", 50, maximum=100)
                return ApiResponse(200, self.reports.list(limit=limit))
            if path == "/api/v1/analysis/context" and method == "POST":
                value = self._body_object(body)
                try:
                    return ApiResponse(
                        200,
                        self.analysis.asset_context(value.get("assetIds", value.get("asset_ids"))),
                    )
                except AnalysisServiceError as exc:
                    raise ApiError(exc.code, exc.message) from exc
            if path == "/api/v1/analysis/search" and method == "POST":
                value = self._body_object(body)
                try:
                    return ApiResponse(
                        200,
                        self.analysis.search(
                            value.get("query", ""),
                            asset_type=value.get("type", "all"),
                            source_format=value.get("format"),
                            quality_status=value.get("quality"),
                            limit=int(value.get("limit", 30)),
                            offset=int(value.get("offset", 0)),
                            match=value.get("match", "all"),
                        ),
                    )
                except (AnalysisServiceError, ValueError) as exc:
                    if isinstance(exc, AnalysisServiceError):
                        raise ApiError(exc.code, exc.message) from exc
                    raise ApiError("invalid_search", "analysis search parameters are invalid") from exc
            if path == "/api/v1/analysis/sql" and method == "POST":
                value = self._body_object(body)
                try:
                    return ApiResponse(
                        200,
                        self.analysis.safe_sql(
                            value.get("assetIds", value.get("asset_ids")),
                            value.get("sql"),
                        ),
                    )
                except AnalysisServiceError as exc:
                    status = 408 if exc.code == "query_timeout" else 400
                    raise ApiError(exc.code, exc.message, status) from exc
            if path == "/api/v1/query/schema" and method == "POST":
                value = self._body_object(body)
                asset_ids = value.get("assetIds", value.get("asset_ids"))
                try:
                    return ApiResponse(200, self.sql.schema(asset_ids).as_dict())
                except SqlServiceError as exc:
                    raise ApiError(exc.code, exc.message) from exc
            if path == "/api/v1/query/sql" and method == "POST":
                value = self._body_object(body)
                asset_ids = value.get("assetIds", value.get("asset_ids"))
                sql = value.get("sql")
                if not isinstance(sql, str):
                    raise ApiError("invalid_sql", "sql must be a string")
                try:
                    return ApiResponse(200, self.sql.execute(asset_ids, sql))
                except SqlTimeoutError as exc:
                    raise ApiError(exc.code, exc.message, 408) from exc
                except SqlServiceError as exc:
                    raise ApiError(exc.code, exc.message) from exc
            if path == "/api/v1/quality/issues" and method == "GET":
                limit = _int_param(query, "limit", 50, maximum=100)
                offset = _int_param(query, "offset", 0, maximum=10_000_000)
                return ApiResponse(
                    200,
                    self.quality.list_issues(
                        status=query.get("status", [None])[0],
                        severity=query.get("severity", [None])[0],
                        asset_id=query.get("asset_id", [None])[0],
                        limit=limit,
                        offset=offset,
                    ),
                )
            if path == "/api/v1/process" and method == "POST":
                value = self._body_object(body)
                source = value.get("source")
                force = bool(value.get("force", False))
                policy_key = "autoFileInsight" if "autoFileInsight" in value else "fileInsightEnabled" if "fileInsightEnabled" in value else None
                if policy_key is not None and not isinstance(value.get(policy_key), bool):
                    raise ApiError("FILE_INSIGHT_POLICY_INVALID", "autoFileInsight must be a boolean")
                auto_file_insight = bool(value[policy_key]) if policy_key is not None else self.file_insight_policy.enabled()
                if policy_key is not None:
                    self.file_insight_policy.save(auto_file_insight)
                try:
                    vision_mode = normalize_vision_mode(value.get("visionMode", value.get("vision_mode", "local")))
                except ValueError as exc:
                    raise ApiError("VISION_MODE_INVALID", "图片提取方式无效，请选择本地提取或 AI Vision。") from exc
                vision_provider = None
                if vision_mode == "ai_vision":
                    runtime = load_runtime_ai_settings(self.project_root)
                    if not runtime.configured or not runtime.vision_enabled:
                        if runtime.status == "INCOMPLETE":
                            raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未启动 AI Vision。", 409)
                        if runtime.status == "UNVERIFIED":
                            raise ApiError("AI_VISION_NOT_VERIFIED", "请先在设置中测试 AI 连接，成功后才能启用 AI Vision。", 409)
                        if runtime.status == "CONNECTION_FAILED":
                            raise ApiError("AI_VISION_NOT_VERIFIED", "AI 连接测试失败，AI Vision 保持关闭。", 409, retryable=True)
                        raise ApiError("AI_VISION_NOT_CONFIGURED", "尚未配置可用的 AI Vision 模型。", 409)
                    try:
                        vision_provider = OpenAICompatibleVisionProvider(runtime.config)
                    except ValueError as exc:
                        raise ApiError("AI_VISION_NOT_CONFIGURED", "AI Vision 配置不可用，请检查设置。", 409) from exc
                file_insight_provider = None
                if auto_file_insight:
                    file_insight_provider = configured_file_insight_provider(
                        self.project_root,
                        override=self._semantic_provider_override,
                    )
                    if file_insight_provider is None:
                        raise ApiError("FILE_INSIGHT_NOT_CONFIGURED", "已开启 AI 文件整理，但当前没有可用的 AI 配置。请完成配置或关闭自动整理。", 409)
                try:
                    task = self.tasks.submit(
                        source,
                        force=force,
                        request_id=request_id,
                        vision_mode=vision_mode,
                        vision_provider=vision_provider,
                        file_insight_provider=file_insight_provider,
                        auto_file_insight=auto_file_insight,
                    )
                except SourceValidationError as exc:
                    raise ApiError("invalid_source", str(exc)) from exc
                except TaskAdmissionError as exc:
                    raise ApiError("TASKS_STOPPING", "应用正在停止，暂时不能开始新的处理。", 503, retryable=True) from exc
                return ApiResponse(202, {"taskId": task.task_id, "task": task.public_dict()})
            if path == "/api/v1/tasks" and method == "GET":
                limit = _int_param(query, "limit", 20, maximum=50)
                return ApiResponse(200, {"items": [item.public_dict() for item in self.tasks.list(limit=limit)]})

            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "reprocess" and method == "POST":
                return self._reprocess_file(parts[3], request_id=request_id)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "insight" and method in {"GET", "POST"}:
                return self._file_insight(parts[3], method, body, request_id=request_id)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "preview" and method == "GET":
                page = _int_param(query, "page", 1, maximum=10_000)
                bbox = None
                raw_bbox = query.get("bbox", [""])[0]
                if raw_bbox:
                    try:
                        values = tuple(float(item) for item in raw_bbox.split(","))
                    except (TypeError, ValueError) as exc:
                        raise ApiError("source_preview_invalid", "bbox must contain four numbers", 400) from exc
                    if len(values) != 4:
                        raise ApiError("source_preview_invalid", "bbox must contain four numbers", 400)
                    bbox = values
                try:
                    content, content_type = self.catalog.file_preview(parts[3], page=page, bbox=bbox)
                except FileNotFoundError as exc:
                    raise ApiError("source_preview_unavailable", "source preview is unavailable", 404) from exc
                except ValueError as exc:
                    raise ApiError("source_preview_invalid", str(exc), 400) from exc
                return ApiResponse(200, {}, raw_body=content, content_type=content_type)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "source" and method in {"GET", "HEAD"}:
                try:
                    source_path, content_type, filename = self.catalog.source_file(parts[3])
                    content = source_path.read_bytes()
                except FileNotFoundError as exc:
                    raise ApiError("source_not_found", "source file was not found", 404) from exc
                range_header = ""
                if request_headers:
                    range_header = request_headers.get("Range") or request_headers.get("range") or ""
                header_filename = re.sub(r'[\r\n]', "_", filename)
                safe_filename = re.sub(r'[^A-Za-z0-9._-]', "_", header_filename).strip("._") or "source"
                headers = {
                    "Accept-Ranges": "bytes",
                    "Content-Disposition": (
                        f'inline; filename="{safe_filename}"; '
                        f"filename*=UTF-8''{quote(header_filename, safe='')}"
                    ),
                }
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                    if match is None or (not match.group(1) and not match.group(2)):
                        return ApiResponse(416, {}, raw_body=b"", content_type=content_type, headers={**headers, "Content-Range": f"bytes */{len(content)}"})
                    if match.group(1):
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else len(content) - 1
                    else:
                        suffix = int(match.group(2))
                        start = max(0, len(content) - suffix)
                        end = len(content) - 1
                    if start >= len(content) or start > end:
                        return ApiResponse(416, {}, raw_body=b"", content_type=content_type, headers={**headers, "Content-Range": f"bytes */{len(content)}"})
                    end = min(end, len(content) - 1)
                    return ApiResponse(206, {}, raw_body=content[start : end + 1], content_type=content_type, headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(content)}"})
                return ApiResponse(200, {}, raw_body=content, content_type=content_type, headers=headers)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "content" and method == "GET":
                page_value = query.get("page", [None])[0]
                sheet_value = query.get("sheet", [None])[0]
                page = None
                if page_value not in {None, ""}:
                    page = _int_param(query, "page", 1, maximum=10_000)
                content = self.catalog.file_content(parts[3], page=page, sheet=sheet_value)
                if content is None:
                    raise ApiError("file_not_found", "file was not found", 404)
                return ApiResponse(200, content)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "search" and method == "GET":
                request = SearchQuery(
                    query=query.get("q", [""])[0],
                    file_id=parts[3],
                    asset_type=query.get("type", ["all"])[0],
                    limit=_int_param(query, "limit", 30, maximum=100),
                    offset=_int_param(query, "offset", 0, maximum=10_000_000),
                    match=query.get("match", ["all"])[0],
                )
                try:
                    return ApiResponse(200, self.search.search_file_occurrences(parts[3], request).as_dict())
                except SearchValidationError as exc:
                    raise ApiError("invalid_search", str(exc)) from exc
            if len(parts) == 4 and parts[:3] == ["api", "v1", "files"] and method == "GET":
                detail = self.catalog.file_detail(parts[3])
                if detail is None:
                    raise ApiError("file_not_found", "file was not found", 404)
                return ApiResponse(200, detail)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "analysis"] and parts[3] == "runs" and method == "GET":
                run = self.analysis_runs.read(parts[4])
                if run is None:
                    raise ApiError("analysis_run_not_found", "analysis run was not found", 404)
                return ApiResponse(200, {"run": run})
            if len(parts) == 5 and parts[:3] == ["api", "v1", "reports"] and parts[4] == "export" and method == "GET":
                return self._report_export(parts[3], query.get("format", [""])[0].casefold())
            if len(parts) == 4 and parts[:3] == ["api", "v1", "reports"] and method == "GET":
                try:
                    report = self.reports.read(parts[3])
                except ReportExecutionError as exc:
                    raise ApiError("REPORT_PERSISTENCE_ERROR", exc.message, 500) from exc
                if report is None:
                    raise ApiError("report_not_found", "report was not found", 404)
                return ApiResponse(200, {"report": report})
            if len(parts) == 5 and parts[:3] == ["api", "v1", "tasks"] and parts[4] == "cancel" and method == "POST":
                task = self.tasks.cancel(parts[3])
                if task is None:
                    raise ApiError("task_not_found", "task was not found", 404)
                if task.task_type == "ai_analysis" and task.analysis_run_id and task.status in {"cancelled", "cancelling"}:
                    try:
                        self.analysis_runs.mark_cancelled(task.analysis_run_id)
                    except Exception:
                        pass
                return ApiResponse(200, {"task": task.public_dict()})
            if len(parts) == 4 and parts[:3] == ["api", "v1", "tasks"] and method == "GET":
                task = self.tasks.get(parts[3])
                if task is None:
                    raise ApiError("task_not_found", "task was not found", 404)
                return ApiResponse(200, task.public_dict())
            if len(parts) == 5 and parts[:4] == ["api", "v1", "quality", "issues"] and method == "PATCH":
                value = self._body_object(body)
                status = value.get("status")
                if not isinstance(status, str):
                    raise ApiError("invalid_status", "status is required")
                updated = self.quality.update_status(parts[4], status)
                if updated is None:
                    raise ApiError("issue_not_found", "quality issue was not found", 404)
                return ApiResponse(200, {"item": updated})
            if len(parts) >= 4 and parts[:3] == ["api", "v1", "assets"]:
                asset_id = parts[3]
                if len(parts) == 5 and parts[4] == "semantic-enrich" and method == "POST":
                    return self._semantic_enrich(asset_id, body, request_id=request_id)
                if len(parts) == 4 and method == "GET":
                    detail = self.catalog.asset_detail(asset_id)
                    if detail is None:
                        raise ApiError("asset_not_found", "asset was not found", 404)
                    return ApiResponse(200, detail)
                if len(parts) == 5 and parts[4] == "table-preview" and method == "GET":
                    limit = _int_param(query, "limit", 50, maximum=200)
                    offset = _int_param(query, "offset", 0, maximum=10_000_000)
                    layer = query.get("layer", ["normalized"])[0]
                    try:
                        return ApiResponse(200, self.catalog.table_preview(asset_id, layer=layer, limit=limit, offset=offset))
                    except KeyError as exc:
                        raise ApiError("asset_not_found", "table asset was not found", 404) from exc
                if len(parts) == 5 and parts[4] == "text-preview" and method == "GET":
                    limit = _int_param(query, "limit", 8_000, maximum=20_000)
                    offset = _int_param(query, "offset", 0, maximum=10_000_000)
                    try:
                        return ApiResponse(200, self.catalog.text_preview(asset_id, offset=offset, limit=limit))
                    except KeyError as exc:
                        raise ApiError("asset_not_found", "text asset was not found", 404) from exc

            raise ApiError("not_found", "API route was not found", 404)
        except ApiError:
            raise
        except (ValueError, SourceValidationError) as exc:
            raise ApiError("invalid_request", str(exc)) from exc
        except KeyError as exc:
            raise ApiError("asset_not_found", "asset was not found", 404) from exc
        except FileNotFoundError as exc:
            raise ApiError("artifact_not_found", str(exc), 404) from exc
        except RegistryError as exc:
            if is_registry_busy_error(exc):
                self.logger.warning("request %s registry busy: %s", request_id, exc, exc_info=True)
                raise ApiError(
                    "REGISTRY_BUSY",
                    "数据目录正在更新，请稍后重试。",
                    503,
                    retryable=True,
                ) from exc
            self.logger.error("request %s failed with registry error: %s\n%s", request_id, exc, traceback.format_exc())
            raise ApiError("REGISTRY_UNAVAILABLE", "数据目录暂时不可用，请稍后重试。", 503, retryable=True) from exc
        except Exception as exc:  # no traceback is sent to the browser
            if is_registry_busy_error(exc):
                self.logger.warning("request %s registry busy: %s", request_id, exc, exc_info=True)
                raise ApiError(
                    "REGISTRY_BUSY",
                    "数据目录正在更新，请稍后重试。",
                    503,
                    retryable=True,
                ) from exc
            self.logger.error("request %s failed: %s\n%s", request_id, exc, traceback.format_exc())
            raise ApiError("INTERNAL_ERROR", "请求未完成，请稍后重试。", 500, retryable=True) from exc

    def static_file(self, request_path: str) -> tuple[Path, str]:
        """Resolve only files below frontend/dist; never arbitrary local paths."""

        if not self.frontend_dist.is_dir():
            raise ApiError("frontend_not_built", "frontend/dist is missing; run the frontend production build", 503)
        relative = unquote(urlsplit(request_path).path.lstrip("/")) or "index.html"
        if relative.startswith("api/") or relative == "api":
            raise ApiError("not_found", "resource was not found", 404)
        candidate = (self.frontend_dist / relative).resolve()
        try:
            candidate.relative_to(self.frontend_dist.resolve())
        except ValueError as exc:
            raise ApiError("not_found", "resource was not found", 404) from exc
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            # Client-side routes are served by the one local index document.
            candidate = self.frontend_dist / "index.html"
        if not candidate.is_file():
            raise ApiError("frontend_not_built", "frontend/dist/index.html is missing; run the frontend production build", 503)
        suffix = candidate.suffix.casefold()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(suffix, "application/octet-stream")
        return candidate, content_type
