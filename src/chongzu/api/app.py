"""HTTP-independent API application and route orchestration.

The request handler is intentionally thin: this module validates the stable
HTTP contract and delegates catalog, quality, and process work to services.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import traceback
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from chongzu import paths
from chongzu.semantic.config import load_semantic_config
from chongzu.services import (
    CatalogService,
    ProcessTaskManager,
    QualityService,
    SourceValidationError,
)


APP_NAME = "ChongZu"
APP_VERSION = "0.1.0.dev0"
API_VERSION = "v1"
MAX_REQUEST_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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
        logger: logging.Logger | None = None,
    ) -> None:
        self.project_root = Path(project_root or paths.PROJECT_ROOT).resolve()
        self.registry_path = Path(registry_path or self.project_root / "workspace" / "state" / "registry.duckdb").resolve()
        self.workspace_root = Path(workspace_root or self.project_root / "workspace").resolve()
        self.frontend_dist = Path(frontend_dist or self.project_root / "frontend" / "dist").resolve()
        self.catalog = CatalogService(registry_path=self.registry_path, workspace_root=self.workspace_root)
        self.quality = QualityService(registry_path=self.registry_path)
        self.tasks = task_manager or ProcessTaskManager.for_paths(
            registry_path=self.registry_path,
            workspace_root=self.workspace_root,
        )
        self.logger = logger or logging.getLogger("chongzu.api")

    def close(self) -> None:
        self.tasks.shutdown()

    def _health(self) -> dict[str, Any]:
        try:
            config = load_semantic_config(self.project_root)
            llm = {
                "status": config.status,
                "configured": config.configured,
                "optional": True,
                "networkCalls": "disabled",
            }
        except Exception:
            llm = {
                "status": "NOT_CONFIGURED",
                "configured": False,
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
            "llm": llm,
        }

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
    ) -> ApiResponse:
        request_id = f"req_{uuid4().hex}"
        try:
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
                    task = self.tasks.submit(source, force=force)
                except SourceValidationError as exc:
                    raise ApiError("invalid_source", str(exc)) from exc
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
        except Exception as exc:  # no traceback is sent to the browser
            self.logger.error("request %s failed: %s\n%s", request_id, exc, traceback.format_exc())
            raise ApiError("internal_error", "the local server could not complete the request", 500) from exc

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
