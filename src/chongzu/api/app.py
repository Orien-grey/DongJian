"""HTTP-independent API application and route orchestration.

The request handler is intentionally thin: this module validates the stable
HTTP contract and delegates catalog, quality, and process work to services.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import html
import json
import logging
from pathlib import Path
import re
import socket
import threading
import traceback
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from chongzu import paths
from chongzu.locking import registry_write_mutex
from chongzu.registry import Registry, RegistryError, is_registry_busy_error
from chongzu.search import SearchQuery, SearchService, SearchValidationError
from chongzu.semantic.config import SemanticConfig
from chongzu.semantic.models import SemanticRequest, SemanticResponse
from chongzu.semantic.provider import SemanticProviderError
from chongzu.semantic.settings import AISettingsError, ProjectAIConfigStore, load_runtime_ai_settings
from chongzu.services import (
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
    new_report_id,
    render_report_html,
    render_report_markdown,
    WorkspaceResetError,
    WorkspaceResetService,
)
from chongzu.services.analysis import normalize_analysis_request, new_analysis_run_id, MAX_ANALYSIS_HISTORY_LIMIT, MAX_ANALYSIS_STEPS
from chongzu.semantic.runner import SemanticRunner, provider_for_name
from chongzu.vision.openai_compatible import OpenAICompatibleVisionProvider
from chongzu.vision.runner import normalize_vision_mode


APP_NAME = "ChongZu"
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
        # The optional provider is a test seam only. Production constructs the
        # configured provider for an explicit POST request; no process task
        # or catalog read can invoke semantic enrichment implicitly.
        self._semantic_provider_override = semantic_provider
        self._semantic_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self.instance_id = instance_id
        self.server_pid = server_pid
        self.logger = logger or logging.getLogger("chongzu.api")
        recovered_total = sum(recovered.values())
        if recovered_total:
            self.logger.info("recovered %d incomplete registry runs: %s", recovered_total, recovered)

    def request_shutdown(self) -> None:
        analysis_tasks = [item for item in self.tasks.list(limit=50) if item.task_type == "ai_analysis"]
        self.tasks.request_shutdown()
        for task in analysis_tasks:
            if task.analysis_run_id and task.status in {"cancelled", "cancelling", "interrupted"}:
                try:
                    self.analysis_runs.mark_cancelled(task.analysis_run_id)
                except Exception:
                    pass

    def close(self, *, timeout: float = 5.0) -> bool:
        return self.tasks.shutdown(timeout=timeout)

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
        return ApiResponse(200, {"settings": load_runtime_ai_settings(self.project_root).public()})

    def _reset_workspace(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        confirmation = value.get("confirmation")
        if not isinstance(confirmation, str) or confirmation.strip() != "清空":
            raise ApiError("RESET_CONFIRMATION_REQUIRED", "请输入“清空”确认此危险操作。", 400)
        begin_reset = getattr(self.tasks, "begin_reset", None)
        end_reset = getattr(self.tasks, "end_reset", None)
        barrier_acquired = False

        def active_task_ids() -> list[str]:
            return [
                task.task_id
                for task in self.tasks.list(limit=100_000)
                if task.status in {"queued", "running", "cancelling"}
            ]

        # A cheap pre-admission check gives the user an immediate active-task
        # answer.  It is intentionally repeated after begin_reset because a
        # task may be admitted between this check and the barrier.
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
        return ApiResponse(200, {"settings": load_runtime_ai_settings(self.project_root).public(), "saved": True})

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
                asset_id="chongzu-settings-connection-test",
                asset_type="text",
                model=probe_config.model,
                prompt_version="settings-connection-v2",
                config_version=paths.SEMANTIC_CONFIG_VERSION,
                normalized_artifact_identity="0" * 64,
                instructions="Reply briefly to confirm that this OpenAI-compatible chat completion endpoint is reachable. Do not require JSON formatting.",
                reference_data={"test": "ChongZu connection check"},
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
            return provider_for_name("openai-compatible", runtime.config, allow_real_provider=True)
        except Exception:
            # Report generation remains useful offline.  The sanitized report
            # artifact records the fallback mode without configuration data.
            return None

    def _start_report(self, body: bytes, *, request_id: str) -> ApiResponse:
        value = self._body_object(body)
        run_ids = value.get("analysisRunIds", value.get("sourceAnalysisRunIds", value.get("analysis_run_ids")))
        title = value.get("title", "")
        purpose = value.get("purpose", value.get("description", ""))
        provider = self._report_provider()
        composer = ReportComposer(
            self.analysis_runs,
            self.workspace_root,
            provider=provider,
            report_store=self.reports,
        )
        try:
            composer.validate_inputs(run_ids)
        except ReportExecutionError as exc:
            raise ApiError(exc.code, exc.message, 400, retryable=exc.retryable) from exc
        report_id = new_report_id()

        def runner(progress: object, cancel_event: object) -> dict[str, object]:
            return composer.compose(
                run_ids,
                report_id=report_id,
                title=title,
                purpose=purpose,
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

    def handle_api(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        body: bytes = b"",
        *,
        request_id: str | None = None,
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
            if path == "/api/v1/health" and method == "GET":
                return ApiResponse(200, self._health())
            if path == "/api/v1/overview" and method == "GET":
                return ApiResponse(200, self.catalog.overview())
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
                    ),
                )
            if path == "/api/v1/search" and method == "GET":
                request = SearchQuery(
                    query=query.get("q", [""])[0],
                    asset_type=query.get("type", ["all"])[0],
                    source_format=query.get("format", [None])[0],
                    quality_status=query.get("quality", [None])[0],
                    limit=_int_param(query, "limit", 30, maximum=100),
                    offset=_int_param(query, "offset", 0, maximum=10_000_000),
                    match=query.get("match", ["all"])[0],
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
                try:
                    task = self.tasks.submit(
                        source,
                        force=force,
                        request_id=request_id,
                        vision_mode=vision_mode,
                        vision_provider=vision_provider,
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
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "preview" and method == "GET":
                page = _int_param(query, "page", 1, maximum=10_000)
                try:
                    content, content_type = self.catalog.file_preview(parts[3], page=page)
                except FileNotFoundError as exc:
                    raise ApiError("source_preview_unavailable", "source preview is unavailable", 404) from exc
                except ValueError as exc:
                    raise ApiError("source_preview_invalid", str(exc), 400) from exc
                return ApiResponse(200, {}, raw_body=content, content_type=content_type)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "content" and method == "GET":
                content = self.catalog.file_content(parts[3])
                if content is None:
                    raise ApiError("file_not_found", "file was not found", 404)
                return ApiResponse(200, content)
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
