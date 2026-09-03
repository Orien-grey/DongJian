"""HTTP-independent API application and route orchestration.

The request handler is intentionally thin: this module validates the stable
HTTP contract and delegates catalog, quality, and process work to services.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import threading
import traceback
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from chongzu import paths
from chongzu.locking import registry_write_mutex
from chongzu.registry import Registry, RegistryError, is_registry_busy_error
from chongzu.search import SearchQuery, SearchService, SearchValidationError
from chongzu.semantic.models import SemanticRequest, SemanticResponse
from chongzu.semantic.provider import SemanticProviderError
from chongzu.semantic.settings import (
    AISettingsError,
    AISettingsStore,
    ProjectAIConfigStore,
    load_runtime_ai_settings,
)
from chongzu.services import (
    AnalysisService,
    AnalysisServiceError,
    CatalogService,
    ProcessTaskManager,
    QualityService,
    SqlQueryService,
    SqlServiceError,
    SqlTimeoutError,
    SourceValidationError,
    TaskAdmissionError,
)
from chongzu.semantic.runner import SemanticRunner, provider_for_name
from chongzu.vision.openai_compatible import OpenAICompatibleVisionProvider
from chongzu.vision.runner import normalize_vision_mode


APP_NAME = "ChongZu"
APP_VERSION = "0.1.0.dev0"
API_VERSION = "v1"
MAX_REQUEST_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any]


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

    detail = str(value).strip()
    if isinstance(secret, str) and secret:
        detail = detail.replace(secret, "[REDACTED]")
    return detail[:4_000]


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
        self.tasks.request_shutdown()

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

    def _save_ai_settings(self, body: bytes) -> ApiResponse:
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
                raise ApiError("AI_SETTINGS_INVALID", "AI 模型配置不完整或无效，请检查后保存。") from exc
        return ApiResponse(200, {"settings": load_runtime_ai_settings(self.project_root).public(), "saved": True})

    def _test_ai_connection(self, request_id: str) -> ApiResponse:
        with self._settings_lock:
            store = AISettingsStore(self.project_root)
            project_store = ProjectAIConfigStore(self.project_root)
            if not store.exists and not project_store.exists and not (self.project_root / ".env").is_file():
                raise ApiError("AI_SETTINGS_NOT_SAVED", "请先保存 AI 模型配置。", 409)
            runtime = load_runtime_ai_settings(self.project_root)
            if runtime.status in {"INCOMPLETE", "INVALID_CONFIGURATION", "NOT_CONFIGURED"} or not runtime.configured:
                raise ApiError("AI_SETTINGS_INCOMPLETE", "AI 模型配置未完成，未发起连接测试。", 409)
            provider = self._semantic_provider_override
            if provider is None:
                provider = provider_for_name("openai-compatible", runtime.config, allow_real_provider=True)
            test_request = SemanticRequest(
                asset_id="chongzu-settings-connection-test",
                asset_type="text",
                model=runtime.config.model,
                prompt_version="settings-connection-v1",
                config_version=paths.SEMANTIC_CONFIG_VERSION,
                normalized_artifact_identity="0" * 64,
                instructions="Return a small JSON object confirming that this connection test succeeded.",
                reference_data={"test": "ChongZu connection check", "request_id": request_id},
                output_contract='{"ok":true}',
            )
            try:
                response = provider.generate(test_request)
                if not isinstance(response, SemanticResponse):
                    raise TypeError("AI provider returned an invalid response")
                if not isinstance(response.payload, Mapping) or response.payload.get("ok") is not True:
                    raise SemanticProviderError(
                        "AI connection test returned an invalid response",
                        code="malformed_json",
                    )
            except SemanticProviderError as exc:
                if store.exists:
                    store.mark_test_failure()
                retryable = bool(getattr(exc, "retryable", False))
                raise ApiError("AI_CONNECTION_FAILED", "AI 连接失败，未启用模型；请检查地址、密钥和模型。", 502, retryable=retryable) from exc
            except Exception as exc:
                if store.exists:
                    store.mark_test_failure()
                raise ApiError("AI_CONNECTION_FAILED", "AI 连接失败，未启用模型；请检查地址、密钥和模型。", 502, retryable=True) from exc
            if store.exists:
                store.mark_test_success()
        return ApiResponse(
            200,
            {
                "settings": load_runtime_ai_settings(self.project_root).public(),
                "status": "configured",
                "requestId": request_id,
            },
        )

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
                return self._save_ai_settings(body)
            if path == "/api/v1/settings/ai/test" and method == "POST":
                return self._test_ai_connection(request_id)
            if path == "/api/v1/health" and method == "GET":
                return ApiResponse(200, self._health())
            if path == "/api/v1/overview" and method == "GET":
                return ApiResponse(200, self.catalog.overview())
            if path == "/api/v1/catalog" and method == "GET":
                limit = _int_param(query, "limit", 50, maximum=100)
                offset = _int_param(query, "offset", 0, maximum=10_000_000)
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
