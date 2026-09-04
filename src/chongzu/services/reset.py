"""Safe product-level clearing of generated project data."""

from __future__ import annotations

from pathlib import Path
import shutil
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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.diagnostic = diagnostic
        self.completed_phases = list(completed_phases)
        self.removed = dict(removed or {})


_REGISTRY_DATA_TABLES = (
    "semantic_metadata",
    "quality_issues",
    "semantic_runs",
    "table_profiles",
    "text_profiles",
    "text_chunks",
    "table_assets",
    "text_assets",
    "extraction_runs",
    "cleaning_runs",
    "file_attempts",
    "run_errors",
    "contents",
    "files",
    "scan_runs",
)


class WorkspaceResetService:
    """Clear generated state while preserving user sources and application data.

    The registry schema itself is retained and emptied in one transaction. It
    avoids unlinking an open DuckDB file from a running server and lets the
    existing Registry initialization contract remain the single schema owner.
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
            try:
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
                else:
                    continue
            except OSError as exc:
                raise WorkspaceResetError(
                    "RESET_FAILED",
                    f"无法清除生成数据：{child.name}",
                    stage=stage,
                    diagnostic=f"{type(exc).__name__}: {child.name}",
                    removed={"failedTarget": child.name, "files": files, "directories": directories},
                ) from exc
            files += child_files
            directories += child_directories + (1 if was_directory else 0)
        return {"files": files, "directories": directories}

    def _clear_registry(self) -> int:
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            Registry.ensure_initialized(self.registry_path)
            registry = Registry.open(self.registry_path, initialize=False)
        except RegistryError as exc:
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法打开项目 Registry，未完成清空。",
                stage="registry_clear",
                diagnostic=type(exc).__name__,
            ) from exc
        except OSError as exc:
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法打开项目 Registry，未完成清空。",
                stage="registry_clear",
                diagnostic=type(exc).__name__,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize driver failures at the reset boundary
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法打开项目 Registry，未完成清空。",
                stage="registry_clear",
                diagnostic=type(exc).__name__,
            ) from exc
        try:
            registry.connection.execute("BEGIN TRANSACTION")
            for table in _REGISTRY_DATA_TABLES:
                registry.connection.execute(f'DELETE FROM "{table}"')
            registry.connection.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - convert driver failure at service boundary
            try:
                registry.connection.execute("ROLLBACK")
            except Exception:
                pass
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法清空项目 Registry，原有数据仍需核对。",
                stage="registry_clear",
                diagnostic=type(exc).__name__,
            ) from exc
        finally:
            registry.close()
        return len(_REGISTRY_DATA_TABLES)

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
                    removed={"failedTarget": str(path)},
                ) from exc

    def reset(self, *, active_task_ids: Iterable[str] = ()) -> dict[str, Any]:
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
                registry_tables = self._clear_registry()
                removed["registry_rows"] = {"tables": registry_tables}
                completed_phases.append("registry_clear")

                # server.log can be held by the child server process as its
                # stdout handle.  It is an active operational log, not user
                # project data; leave it in place so Windows can continue to
                # write to the same handle while reset is in progress.
                removed["artifacts"] = self._clear_directory(
                    self.workspace_root / "artifacts",
                    stage="artifact_clear",
                )
                completed_phases.append("artifact_clear")

                removed["logs"] = self._clear_directory(
                    self.workspace_root / "logs",
                    # The active server may own stdout and a logging-handler
                    # handle here. Preserve the complete runtime-log contract
                    # so reset never tries to unlink it on Windows.
                    preserve_names=paths.ACTIVE_RUNTIME_LOG_FILE_NAMES,
                    stage="cache_output_clear",
                )
                for name in ("output", "quarantine", "staging"):
                    removed[name] = self._clear_directory(
                        self.workspace_root / name,
                        stage="cache_output_clear",
                    )

                removed["cache"] = self._clear_directory(
                    self.project_root / "cache",
                    stage="cache_output_clear",
                )
                state_result = self._clear_directory(
                    self.workspace_root / "state",
                    # Registry and lifecycle control files are operational
                    # state, not project data. Keep this exclusion set
                    # explicit instead of adding one-off filename exceptions.
                    preserve_names=(
                        "registry.duckdb",
                        "llm-settings.json",
                        ".gitkeep",
                        *paths.ACTIVE_RUNTIME_CONTROL_FILE_NAMES,
                    ),
                    stage="cache_output_clear",
                )
                removed["registry_rows"].update(state_result)
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
        return {
            "reset": True,
            "phases": {phase: "completed" for phase in completed_phases},
            "removed": removed,
            "preserved": preserved + ["workspace/logs/server.log (active runtime log retained)"],
        }
