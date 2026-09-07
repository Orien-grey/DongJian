"""Safe product-level clearing of generated project data."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Iterable

from chongzu import paths
from chongzu.locking import registry_write_mutex
from chongzu.registry import Registry, RegistryError


class WorkspaceResetError(RuntimeError):
    """A reset was rejected or could not be completed safely."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "prepare",
        diagnostic: str | None = None,
        completed_phases: Iterable[str] = (),
        removed: dict[str, Any] | None = None,
        target_category: str | None = None,
        target_path_safe: str | None = None,
        errno_value: int | None = None,
        winerror: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.diagnostic = diagnostic
        self.completed_phases = list(completed_phases)
        self.removed = dict(removed or {})
        self.target_category = target_category
        self.target_path_safe = target_path_safe
        self.errno = errno_value
        self.winerror = winerror


class WorkspaceResetJournal:
    """Small restart-safe journal for the controlled reset lifecycle."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = Path(workspace_root).resolve() / "state" / "reset-journal.json"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def read(self, request_id: str | None = None) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if request_id is not None and str(value.get("request_id") or "") != request_id:
            return None
        return value

    def write(
        self,
        *,
        request_id: str,
        phase: str,
        result: str = "running",
        started_at: str | None = None,
        finished_at: str | None = None,
        safe_error: str | None = None,
        stage: str | None = None,
        target_category: str | None = None,
        exception_type: str | None = None,
        safe_detail: str | None = None,
        target_path_safe: str | None = None,
        errno_value: int | None = None,
        winerror: int | None = None,
        requested_at: str | None = None,
        handler_completed_at: str | None = None,
        supervisor_started_at: str | None = None,
        old_server_pid: int | None = None,
        helper_pid: int | None = None,
        cleanup_started_at: str | None = None,
        restart_pid: int | None = None,
        health_restored_at: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self.read(request_id) or {})
        phase_history = payload.get("phase_history")
        if not isinstance(phase_history, list):
            phase_history = []
        phase_history.append({"phase": phase, "stage": stage or phase, "at": self._now(), "result": result})
        payload["phase_history"] = phase_history[-32:]
        payload.update({
            "request_id": request_id,
            "phase": phase,
            "result": result,
        })
        if started_at is not None or "started_at" not in payload:
            payload["started_at"] = started_at or self._now()
        if finished_at is not None:
            payload["finished_at"] = finished_at
        for key, value in {
            "safe_error": str(safe_error)[:512] if safe_error else None,
            "stage": str(stage or phase)[:80],
            "target_category": str(target_category)[:80] if target_category else None,
            "exception_type": str(exception_type)[:120] if exception_type else None,
            "safe_detail": str(safe_detail or safe_error or "")[:512] or None,
            "target_path_safe": str(target_path_safe)[:240] if target_path_safe else None,
            "errno": errno_value,
            "winerror": winerror,
            "requested_at": requested_at,
            "handler_completed_at": handler_completed_at,
            "supervisor_started_at": supervisor_started_at,
            "old_server_pid": old_server_pid,
            "helper_pid": helper_pid,
            "cleanup_started_at": cleanup_started_at,
            "restart_pid": restart_pid,
            "health_restored_at": health_restored_at,
        }.items():
            if value is not None or key not in payload:
                payload[key] = value
        payload.update({str(key): value for key, value in details.items()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix="reset-journal-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return payload


class WorkspaceResetService:
    """Clear generated state while preserving user sources and application data.

    The registry is generated state. Reset prepares a fresh database through
    the canonical Registry initializer, then swaps it in only after the old
    database and its companion files have been drained and disposed.
    """

    def __init__(self, project_root: Path | str, registry_path: Path | str | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace_root = self.project_root / "workspace"
        self.registry_path = Path(registry_path or self.workspace_root / "state" / "registry.duckdb").resolve()
        expected_registry = (self.workspace_root / "state" / "registry.duckdb").resolve()
        if self.registry_path != expected_registry:
            raise WorkspaceResetError("RESET_PATH_INVALID", "Registry path is outside the current workspace state directory.")

    def _validate_layout(self) -> None:
        protected = (
            self.project_root / "src",
            self.project_root / "runtime",
            self.project_root / "models",
            self.project_root / "config",
            self.project_root / "frontend",
            self.workspace_root / "input",
        )
        generated = (
            self.workspace_root / "artifacts",
            self.workspace_root / "state",
            self.workspace_root / "logs",
            self.workspace_root / "output",
            self.workspace_root / "quarantine",
            self.workspace_root / "staging",
            self.project_root / "cache",
        )
        for path in generated:
            resolved = path.resolve()
            if resolved == self.project_root or not self._is_child_of(resolved, self.project_root):
                raise WorkspaceResetError("RESET_PATH_INVALID", "Generated reset path is outside the current project.")
            if any(resolved == item.resolve() or self._is_child_of(resolved, item.resolve()) for item in protected):
                raise WorkspaceResetError("RESET_PATH_INVALID", "A reset path overlaps protected application or source data.")

    @staticmethod
    def _is_child_of(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
        except ValueError:
            return False
        return child != parent

    @staticmethod
    def _count_entry(path: Path) -> tuple[int, int]:
        if path.is_symlink() or path.is_file():
            return 1, 0
        if not path.is_dir():
            return 0, 0
        files = 0
        directories = 0
        for child in path.rglob("*"):
            if child.is_symlink() or child.is_file():
                files += 1
            elif child.is_dir():
                directories += 1
        return files, directories

    @classmethod
    def _clear_directory(
        cls,
        root: Path,
        *,
        preserve_names: Iterable[str] = (),
        stage: str = "filesystem_clear",
        target_category: str = "state",
    ) -> dict[str, int]:
        root.mkdir(parents=True, exist_ok=True)
        preserved = set(preserve_names)
        files = 0
        directories = 0
        for child in list(root.iterdir()):
            if child.name in preserved:
                continue
            child_files, child_directories = cls._count_entry(child)
            was_directory = child.is_dir()
            for attempt in range(3):
                try:
                    if child.is_symlink() or child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child)
                    else:
                        break
                    break
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    transient = isinstance(exc, PermissionError) or winerror in {5, 32, 33, 145}
                    if not transient or attempt == 2:
                        try:
                            safe_path = str(child.resolve().relative_to(root.resolve())).replace("\\", "/")
                        except ValueError:
                            safe_path = child.name
                        raise WorkspaceResetError(
                            "RESET_FAILED",
                            f"无法清除生成数据：{child.name}",
                            stage=stage,
                            diagnostic=f"{type(exc).__name__}: {child.name}",
                            removed={"failedTarget": child.name, "targetCategory": target_category, "files": files, "directories": directories},
                            target_category=target_category,
                            target_path_safe=f"{root.name}/{safe_path}",
                            errno_value=getattr(exc, "errno", None),
                            winerror=winerror,
                        ) from exc
                    time.sleep(0.05)
            files += child_files
            directories += child_directories + (1 if was_directory else 0)
        return {"files": files, "directories": directories}

    def _registry_companion_paths(self) -> list[Path]:
        """Return the generated Registry file set in its exact state folder."""

        state_root = (self.workspace_root / "state").resolve()
        if self.registry_path.parent.resolve() != state_root:
            raise WorkspaceResetError(
                "RESET_PATH_INVALID",
                "Registry path is outside the current workspace state directory.",
                stage="registry_clear",
                target_category="state",
                target_path_safe="workspace/state/registry.duckdb",
            )
        prefix = self.registry_path.name + "."
        result: list[Path] = []
        for child in state_root.iterdir():
            if child.name != self.registry_path.name and not child.name.startswith(prefix):
                continue
            if child.is_file() or child.is_symlink():
                result.append(child)
        return sorted(result, key=lambda item: item.name.casefold())

    def _safe_registry_path(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.project_root)).replace("\\", "/")

    @staticmethod
    def _registry_diagnostic(operation: str, exc: BaseException) -> str:
        detail = f"{operation}:{type(exc).__name__}"
        errno_value = getattr(exc, "errno", None)
        winerror = getattr(exc, "winerror", None)
        if errno_value is not None:
            detail += f":errno={errno_value}"
        if winerror is not None:
            detail += f":winerror={winerror}"
        return detail

    def _clear_registry(self) -> dict[str, Any]:
        """Replace the generated Registry instead of deleting indexed rows."""

        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        old_paths = self._registry_companion_paths()
        staging: Path | None = None
        backup: Path | None = None
        main_moved = False
        installed = False
        operation = "prepare"
        target_path = self.registry_path
        try:
            if self.registry_path.exists() and not self.registry_path.is_file():
                raise OSError("registry path is not a file")

            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.registry_path.parent, prefix="registry.duckdb.reset-new-", suffix=".tmp", delete=False
            ) as handle:
                staging = Path(handle.name)
            staging.unlink()

            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.registry_path.parent, prefix="registry.duckdb.reset-old-", suffix=".tmp", delete=False
            ) as handle:
                backup = Path(handle.name)
            backup.unlink()

            operation = "initialize_fresh_registry"
            Registry.ensure_initialized(staging)
            staging.with_name(staging.name + ".schema.lock").unlink(missing_ok=True)

            if self.registry_path.exists():
                operation = "dispose_registry"
                target_path = self.registry_path
                os.replace(self.registry_path, backup)
                main_moved = True

            for path in old_paths:
                if path == self.registry_path:
                    continue
                operation = "dispose_registry_companion"
                target_path = path
                path.unlink(missing_ok=True)

            operation = "install_fresh_registry"
            target_path = self.registry_path
            os.replace(staging, self.registry_path)
            installed = True

            if main_moved and backup is not None:
                operation = "dispose_registry_backup"
                target_path = backup
                backup.unlink()
                main_moved = False

            return {"database": "recreated", "files": len(old_paths)}
        except RegistryError as exc:
            cause = exc.__cause__ or exc
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法重新创建项目 Registry。",
                stage="registry_clear",
                diagnostic=self._registry_diagnostic(operation, cause),
                target_category="state",
                target_path_safe=self._safe_registry_path(target_path),
                errno_value=getattr(cause, "errno", None),
                winerror=getattr(cause, "winerror", None),
            ) from exc
        except OSError as exc:
            diagnostic = self._registry_diagnostic(operation, exc)
            if main_moved and backup is not None:
                try:
                    if installed and self.registry_path.exists() and staging is not None:
                        os.replace(self.registry_path, staging)
                    if backup.exists():
                        os.replace(backup, self.registry_path)
                    main_moved = False
                    installed = False
                except OSError as restore_exc:
                    diagnostic += f";restore:{self._registry_diagnostic('registry', restore_exc)}"
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法重新创建项目 Registry。",
                stage="registry_clear",
                diagnostic=diagnostic,
                target_category="state",
                target_path_safe=self._safe_registry_path(target_path),
                errno_value=getattr(exc, "errno", None),
                winerror=getattr(exc, "winerror", None),
            ) from exc
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
                staging.with_name(staging.name + ".schema.lock").unlink(missing_ok=True)

    def _recreate_empty_layout(self) -> None:
        """Restore the empty generated-directory contract after a reset.

        Clearing a directory removes its tracked ``.gitkeep`` as well as
        runtime artifacts.  The server can recreate most of these lazily, but
        the project doctor and the next processing run expect the canonical
        workspace/cache roots to exist immediately after a successful reset.
        This phase only creates directories; it never recreates user data or
        an active log file.
        """
        required = (
            self.workspace_root / "artifacts",
            self.workspace_root / "artifacts" / "tables",
            self.workspace_root / "artifacts" / "sheets",
            self.workspace_root / "artifacts" / "cleaning",
            self.workspace_root / "artifacts" / "cleaning" / "tables",
            self.workspace_root / "artifacts" / "cleaning" / "text",
            self.workspace_root / "state",
            self.workspace_root / "logs",
            self.workspace_root / "output",
            self.workspace_root / "quarantine",
            self.workspace_root / "staging",
            self.project_root / "cache",
            self.project_root / "cache" / "uv",
            self.project_root / "cache" / "pip",
            self.project_root / "cache" / "huggingface",
            self.project_root / "cache" / "huggingface" / "hub",
            self.project_root / "cache" / "docling",
            self.project_root / "cache" / "ocr",
            self.project_root / "cache" / "tika",
            self.project_root / "cache" / "temp",
            self.project_root / "cache" / "temp" / "pycache",
        )
        for path in required:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WorkspaceResetError(
                    "RESET_FAILED",
                    "无法重新初始化清空后的项目目录。",
                    stage="finalize",
                    diagnostic=f"{type(exc).__name__}: {path.name}",
                    removed={"failedTarget": path.name, "targetCategory": "state"},
                    target_category="state",
                    target_path_safe=str(path.relative_to(self.project_root)).replace("\\", "/"),
                    errno_value=getattr(exc, "errno", None),
                    winerror=getattr(exc, "winerror", None),
                ) from exc

    def reset(
        self,
        *,
        active_task_ids: Iterable[str] = (),
        preserve_runtime_files: bool = True,
    ) -> dict[str, Any]:
        active = [str(item) for item in active_task_ids if str(item)]
        if active:
            raise WorkspaceResetError("ACTIVE_TASKS", "当前仍有处理任务运行，请先取消并等待任务结束。", stage="prepare")

        completed_phases: list[str] = []
        removed: dict[str, Any] = {}
        try:
            # The phases are deliberately explicit.  A multi-directory reset
            # cannot be one filesystem transaction, so failures report the
            # completed phases and counts instead of pretending it rolled back.
            self._validate_layout()
            completed_phases.append("prepare")
            with registry_write_mutex(self.registry_path):
                registry_result = self._clear_registry()
                removed["registry_rows"] = registry_result
                completed_phases.append("registry_clear")

                removed["artifacts"] = self._clear_directory(
                    self.workspace_root / "artifacts",
                    stage="artifact_clear",
                    target_category="output",
                )
                completed_phases.append("artifact_clear")

                removed["logs"] = self._clear_directory(
                    self.workspace_root / "logs",
                    preserve_names=paths.ACTIVE_RUNTIME_LOG_FILE_NAMES if preserve_runtime_files else (),
                    stage="cache_output_clear",
                    target_category="log",
                )
                for name in ("output", "quarantine", "staging"):
                    removed[name] = self._clear_directory(
                        self.workspace_root / name,
                        stage="cache_output_clear",
                        target_category="output",
                    )

                removed["cache"] = self._clear_directory(
                    self.project_root / "cache",
                    stage="cache_output_clear",
                    target_category="cache",
                )
                state_result = self._clear_directory(
                    self.workspace_root / "state",
                    preserve_names=(
                        "registry.duckdb",
                        "llm-settings.json",
                        "file-insight-policy.json",
                        "reset-journal.json",
                        ".gitkeep",
                        *(paths.ACTIVE_RUNTIME_CONTROL_FILE_NAMES if preserve_runtime_files else ()),
                    ),
                    stage="cache_output_clear",
                    target_category="state",
                )
                removed["state"] = state_result
                completed_phases.append("cache_output_clear")
                self._recreate_empty_layout()
            completed_phases.append("finalize")
        except WorkspaceResetError as exc:
            # Include the counts from the directory that failed as well as all
            # completed phases.  This makes partial cleanup inspectable by the
            # UI and by the request-id correlated server log.
            for name, value in exc.removed.items():
                if name == "files" or name == "directories":
                    continue
                removed[name] = value
            exc.completed_phases = list(completed_phases)
            exc.removed = removed
            raise
        except OSError as exc:
            raise WorkspaceResetError(
                "RESET_FAILED",
                "无法清除生成数据，请根据清理阶段和请求 ID 检查日志。",
                stage="cache_output_clear",
                diagnostic=type(exc).__name__,
                completed_phases=completed_phases,
                removed=removed,
                target_category="output",
                errno_value=getattr(exc, "errno", None),
                winerror=getattr(exc, "winerror", None),
            ) from exc
        preserved = [
            "workspace/input",
            "runtime",
            "runtime/models",
            "models",
            "config/llm.json",
            "workspace/state/llm-settings.json",
            "application files",
        ]
        if preserve_runtime_files:
            preserved.append("workspace/logs/server.log (active runtime log retained)")
        return {
            "reset": True,
            "phases": {phase: "completed" for phase in completed_phases},
            "removed": removed,
            "preserved": preserved,
        }
