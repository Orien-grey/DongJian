"""Asynchronous process-task service for the local product server."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from chongzu.cancellation import CancellationRequested
from chongzu.clean.runner import process_source
from chongzu.locking import registry_write_mutex
from chongzu.registry import Registry, RegistryError, is_registry_busy_error
from chongzu.worker_runtime import configure_hidden_worker_executable
from chongzu.vision.runner import normalize_vision_mode
from .report import ReportExecutionError

from .analysis import AnalysisExecutionError


class SourceValidationError(ValueError):
    """Raised when a user supplied process source is not a directory."""


class TaskAdmissionError(RuntimeError):
    """Raised when the server is already draining and accepts no new work."""


class ProgressCallback(Protocol):
    """Stable stage callback; the core pipeline does not know about HTTP."""

    def __call__(
        self,
        stage: str,
        progress: float,
        *,
        current_file: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_page: int | None = None,
        current_substage: str | None = None,
        current_step: int | None = None,
        max_steps: int | None = None,
        run_id: str | None = None,
    ) -> None: ...


def validate_source_directory(value: object) -> Path:
    if isinstance(value, Path):
        source_value = value
    elif isinstance(value, str) and value.strip():
        source_value = Path(value.strip())
    else:
        raise SourceValidationError("source must be a non-empty directory path")
    source = source_value.expanduser().resolve()
    if not source.exists():
        raise SourceValidationError(f"source directory does not exist: {source}")
    if not source.is_dir():
        raise SourceValidationError(f"source is not a directory: {source}")
    return source


@dataclass
class ProcessTask:
    task_id: str
    source: str
    task_type: str = "process"
    vision_mode: str = "local"
    status: str = "queued"
    progress: float = 0.0
    current_stage: str = "queued"
    current_file: str | None = None
    current_page: int | None = None
    completed: int = 0
    total: int = 0
    current_substage: str | None = None
    current_step: int = 0
    max_steps: int = 0
    elapsed_seconds: float = 0.0
    run_id: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_summary: str | None = None
    error: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    request_id: str | None = field(default=None, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _started_clock: float | None = field(default=None, repr=False)
    _future: Future[Any] | None = field(default=None, repr=False)
    _vision_provider: object | None = field(default=None, repr=False)
    analysis_run_id: str | None = None
    _analysis_runner: Callable[..., Mapping[str, Any]] | None = field(default=None, repr=False)
    _analysis_provider: object | None = field(default=None, repr=False)
    report_id: str | None = None
    _report_runner: Callable[..., Mapping[str, Any]] | None = field(default=None, repr=False)
    _report_provider: object | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "source": self.source,
            "taskType": self.task_type,
            "visionMode": self.vision_mode,
            "status": self.status,
            "progress": round(float(self.progress), 4),
            "currentStage": self.current_stage,
            "currentFile": self.current_file,
            "currentPage": self.current_page,
            "completed": self.completed,
            "total": self.total,
            "currentSubstage": self.current_substage,
            "currentStep": self.current_step,
            "maxSteps": self.max_steps,
            "elapsedSeconds": round(float(self.elapsed_seconds), 2),
            "runId": self.run_id,
            "analysisRunId": self.analysis_run_id,
            "reportId": self.report_id,
            "counts": dict(self.counts),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "errorSummary": self.error_summary,
            "error": self.error,
            "summary": self.summary,
        }


class ProcessTaskManager:
    """One bounded local worker; process_source retains file isolation."""

    def __init__(self, *, max_workers: int = 1) -> None:
        if int(max_workers) != 1:
            raise ValueError("the product process task manager supports exactly one active writer task")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chongzu-api-process")
        self._lock = threading.RLock()
        self._tasks: dict[str, ProcessTask] = {}
        self._shutdown_requested = False
        # Reset uses the same admission lock as task submission.  This is a
        # process-local barrier: once it is raised, no new writer/task can be
        # admitted while the reset service is changing generated state.
        self._reset_in_progress = False
        self.registry_path: Path | None = None
        self.workspace_root: Path | None = None

    @classmethod
    def for_paths(
        cls,
        *,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
        max_workers: int = 1,
    ) -> "ProcessTaskManager":
        manager = cls(max_workers=max_workers)
        manager.registry_path = Path(registry_path).resolve() if registry_path is not None else None
        manager.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        return manager

    def submit(
        self,
        source: Path | str,
        *,
        force: bool = False,
        request_id: str | None = None,
        vision_mode: str = "local",
        vision_provider: object | None = None,
    ) -> ProcessTask:
        validated = validate_source_directory(source)
        normalized_vision_mode = normalize_vision_mode(vision_mode)
        if normalized_vision_mode == "ai_vision" and vision_provider is None:
            raise TaskAdmissionError("AI Vision provider is not verified")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept new processing tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source=str(validated),
                vision_mode=normalized_vision_mode,
                request_id=request_id,
                _vision_provider=vision_provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run, task.task_id, validated, force)
        return task

    def submit_analysis(
        self,
        analysis_run_id: str,
        runner: Callable[..., Mapping[str, Any]],
        *,
        provider: object | None = None,
        request_id: str | None = None,
        max_steps: int = 6,
    ) -> ProcessTask:
        """Admit one bounded AI Analysis run onto the existing task worker."""

        if not isinstance(analysis_run_id, str) or not analysis_run_id.strip():
            raise TaskAdmissionError("analysis run ID is required")
        if not callable(runner):
            raise TaskAdmissionError("analysis runner is unavailable")
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1 or max_steps > 6:
            raise TaskAdmissionError("analysis step limit is invalid")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept new analysis tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source="AI Analysis",
                task_type="ai_analysis",
                vision_mode="local",
                request_id=request_id,
                analysis_run_id=analysis_run_id,
                run_id=analysis_run_id,
                max_steps=max_steps,
                _analysis_runner=runner,
                _analysis_provider=provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run_analysis, task.task_id)
        return task

    def submit_report(
        self,
        report_id: str,
        runner: Callable[..., Mapping[str, Any]],
        *,
        provider: object | None = None,
        request_id: str | None = None,
    ) -> ProcessTask:
        """Admit one bounded report composition task onto the same worker."""

        if not isinstance(report_id, str) or not report_id.strip():
            raise TaskAdmissionError("report ID is required")
        if not callable(runner):
            raise TaskAdmissionError("report runner is unavailable")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept new report tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source="Analysis Report",
                task_type="report_generation",
                vision_mode="local",
                request_id=request_id,
                report_id=report_id,
                run_id=report_id,
                max_steps=1,
                _report_runner=runner,
                _report_provider=provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run_report, task.task_id)
        return task

    def _set_stage(
        self,
        task: ProcessTask,
        stage: str,
        progress: float,
        *,
        current_file: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_page: int | None = None,
        current_substage: str | None = None,
        current_step: int | None = None,
        max_steps: int | None = None,
        run_id: str | None = None,
    ) -> None:
        with self._lock:
            if task.status == "queued":
                task.status = "running"
                task.started_at = datetime.now().astimezone().replace(tzinfo=None)
                task._started_clock = time.perf_counter()
            elif task.status == "cancelling":
                # A stop request owns the visible state. Late callbacks from
                # an active route must not make a cancelling task look alive
                # again while the server is draining it.
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
                return
            task.current_stage = stage
            task.progress = max(0.0, min(1.0, progress))
            if current_file is not None:
                task.current_file = current_file
            if completed is not None:
                task.completed = max(0, int(completed))
            if total is not None:
                task.total = max(0, int(total))
            if current_page is not None:
                task.current_page = max(1, int(current_page))
            if current_substage is not None:
                task.current_substage = current_substage
            if current_step is not None:
                task.current_step = max(0, int(current_step))
            if max_steps is not None:
                task.max_steps = max(0, int(max_steps))
            if run_id is not None:
                task.run_id = run_id
            if task._started_clock is not None:
                task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)

    @staticmethod
    def _error_for(task: ProcessTask, exc: Exception) -> dict[str, Any]:
        detail = str(exc).strip()
        provider = task._analysis_provider if task.task_type == "ai_analysis" else task._report_provider if task.task_type == "report_generation" else task._vision_provider
        provider_config = getattr(provider, "config", None)
        provider_secret = getattr(provider_config, "api_key", "") if provider_config is not None else ""
        if isinstance(provider_secret, str) and provider_secret:
            detail = detail.replace(provider_secret, "[REDACTED]")
        lowered = detail.casefold()
        if task.task_type == "ai_analysis":
            if isinstance(exc, AnalysisExecutionError):
                code = exc.code
                message = exc.message
                retryable = exc.retryable
                stage = exc.stage
            elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
                code = "ANALYSIS_CANCELLED"
                message = "analysis was cancelled"
                retryable = True
                stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "cancelled"
            else:
                code = "ANALYSIS_FAILED"
                message = "AI analysis could not be completed"
                retryable = True
                stage = task.current_stage
            scope = "analysis"
        elif task.task_type == "report_generation":
            if isinstance(exc, ReportExecutionError):
                code = exc.code
                message = exc.message
                retryable = exc.retryable
                stage = exc.stage
            elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
                code = "REPORT_CANCELLED"
                message = "report generation was cancelled"
                retryable = True
                stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "cancelled"
            else:
                code = "REPORT_FAILED"
                message = "analysis report could not be generated"
                retryable = True
                stage = task.current_stage
            scope = "report"
        elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
            code = "TASK_CANCELLED"
            message = "用户已请求停止处理。"
            retryable = True
            stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "shutdown"
            scope = "file" if task.current_file else "directory"
        elif is_registry_busy_error(exc):
            code = "REGISTRY_BUSY"
            message = "数据目录正在更新，请稍后重试。"
            retryable = True
            stage = "registry_init" if "initialize registry" in lowered else task.current_stage
            scope = "file" if task.current_file else "directory"
        elif isinstance(exc, RegistryError) or type(exc).__name__.casefold() in {"ioexception", "catalogexception"}:
            code = "REGISTRY_UNAVAILABLE"
            message = "数据目录暂时不可用，请稍后重试。"
            retryable = True
            stage = "registry_init"
            scope = "file" if task.current_file else "directory"
        else:
            code = "PROCESS_FAILED"
            message = "目录处理失败，请查看技术详情后重试。"
            retryable = True
            stage = task.current_stage
            scope = "file" if task.current_file else "directory"
        technical = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return {
            "code": code,
            "stage": stage,
            "message": message,
            "retryable": retryable,
            "affectedFile": task.current_file,
            "runId": task.run_id,
            "requestId": task.request_id,
            "scope": scope,
            "technicalDetail": technical,
        }

    def _recover_source(self, source: Path) -> None:
        if self.registry_path is None or not self.registry_path.is_file():
            return
        registry = None
        try:
            with registry_write_mutex(self.registry_path):
                registry = Registry.open(self.registry_path, initialize=False)
                source_root = str(source.resolve()).casefold()
                registry.recover_incomplete_runs(source_root)
                registry.recover_incomplete_extractions(source_root)
                registry.recover_incomplete_cleaning(source_root)
        except Exception:
            # Startup recovery is the final safety net if the database itself
            # is unavailable; never mask the original task failure here.
            return
        finally:
            if registry is not None:
                registry.close()

    @staticmethod
    def _summary_counts(summary: Any) -> dict[str, Any]:
        values = asdict(summary)
        return {
            "filesDiscovered": values.get("files_discovered", 0),
            "filesSupported": values.get("files_supported", 0),
            "filesUnsupported": values.get("files_unsupported", 0),
            "extracted": values.get("extracted", 0),
            "reusedExtraction": values.get("reused_extraction", 0),
            "tableAssets": values.get("table_assets", 0),
            "textAssets": values.get("text_assets", 0),
            "cleaned": values.get("cleaned", 0),
            "reusedCleaning": values.get("reused_cleaning", 0),
            "cleaningFailures": values.get("cleaning_failures", 0),
            "qualityIssues": values.get("quality_issues", 0),
            "ready": values.get("ready", 0),
            "needsReview": values.get("needs_review", 0),
            "unusable": values.get("unusable", 0),
        }

    def _run(self, task_id: str, source: Path, force: bool) -> None:
        with self._lock:
            task = self._tasks[task_id]
        configure_hidden_worker_executable()
        started = time.perf_counter()
        try:
            # The core coordinator remains independent from HTTP.  The callback
            # is a small stage event seam; future coordinators can emit finer
            # file events without changing the API contract.
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_substage: str | None = None,
                current_step: int | None = None,
                max_steps: int | None = None,
                run_id: str | None = None,
            ) -> None:
                self._set_stage(
                    task,
                    stage,
                    value,
                    current_file=current_file,
                    completed=completed,
                    total=total,
                    current_page=current_page,
                    current_substage=current_substage,
                    current_step=current_step,
                    max_steps=max_steps,
                    run_id=run_id,
                )

            with registry_write_mutex(self.registry_path or source / ".registry"):
                summary = process_source(
                    source,
                    force=force,
                    registry_path=self.registry_path,
                    workspace_root=self.workspace_root,
                    progress_callback=progress,
                    cancel_event=task._cancel_event,
                    vision_mode=task.vision_mode,
                    vision_provider=task._vision_provider,
                )
            public_summary = {
                "sourceRoot": summary.source_root,
                "filesDiscovered": summary.files_discovered,
                "filesSupported": summary.files_supported,
                "filesUnsupported": summary.files_unsupported,
                "extracted": summary.extracted,
                "reusedExtraction": summary.reused_extraction,
                "extractionFailures": summary.extraction_failures,
                "tableAssets": summary.table_assets,
                "textAssets": summary.text_assets,
                "cleaned": summary.cleaned,
                "reusedCleaning": summary.reused_cleaning,
                "cleaningFailures": summary.cleaning_failures,
                "ready": summary.ready,
                "needsReview": summary.needs_review,
                "unusable": summary.unusable,
                "qualityIssues": summary.quality_issues,
                "semanticPending": summary.semantic_pending,
                "rows": summary.rows,
                "chars": summary.chars,
                "wallTimeMs": summary.wall_time_ms,
            }
            with self._lock:
                if task._cancel_event.is_set() or task.status == "cancelling":
                    task.status = "cancelled"
                    task.current_stage = "cancelled"
                    task.progress = min(task.progress, 1.0)
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, CancellationRequested())
                    task.error_summary = task.error["message"]
                else:
                    task.status = "succeeded"
                    task.progress = 1.0
                    task.current_stage = "completed"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.counts = self._summary_counts(summary)
                task.summary = public_summary
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except CancellationRequested as exc:
            self._recover_source(source)
            with self._lock:
                task.status = "cancelled"
                task.current_stage = "cancelled"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:  # one task failure must be returned as JSON only
            if task._cancel_event.is_set():
                self._recover_source(source)
            with self._lock:
                task.status = "interrupted" if task._cancel_event.is_set() else "failed"
                task.progress = min(task.progress, 0.99)
                task.current_stage = "interrupted" if task._cancel_event.is_set() else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        finally:
            with self._lock:
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
            _ = started

    def _run_analysis(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
        started = time.perf_counter()
        try:
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_substage: str | None = None,
                current_step: int | None = None,
                max_steps: int | None = None,
                run_id: str | None = None,
            ) -> None:
                self._set_stage(
                    task,
                    stage,
                    value,
                    current_file=current_file,
                    completed=completed,
                    total=total,
                    current_page=current_page,
                    current_substage=current_substage,
                    current_step=current_step,
                    max_steps=max_steps,
                    run_id=run_id or task.analysis_run_id,
                )

            self._set_stage(
                task,
                "ai_analysis",
                0.01,
                current_file="AI Analysis",
                completed=0,
                total=task.max_steps,
                current_substage="preparing_scope",
                current_step=0,
                max_steps=task.max_steps,
                run_id=task.analysis_run_id,
            )
            runner = task._analysis_runner
            if runner is None:
                raise AnalysisExecutionError("ANALYSIS_FAILED", "AI analysis runner is unavailable")
            result = runner(progress, task._cancel_event)
            if task._cancel_event.is_set() or task.status == "cancelling":
                raise AnalysisExecutionError("ANALYSIS_CANCELLED", "analysis was cancelled", stage="cancelled", retryable=True)
            result_status = str(result.get("status") or "completed") if isinstance(result, Mapping) else "completed"
            steps_used = int(result.get("steps_used", task.current_step)) if isinstance(result, Mapping) else task.current_step
            provider_calls = int(result.get("provider_calls", 0)) if isinstance(result, Mapping) else 0
            with self._lock:
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.current_substage = "completed"
                task.current_step = max(0, steps_used)
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.counts = {"steps": max(0, steps_used), "providerCalls": max(0, provider_calls)}
                task.summary = {
                    "analysisRunId": task.analysis_run_id,
                    "analysisStatus": result_status,
                    "stepsUsed": max(0, steps_used),
                    "maxSteps": task.max_steps,
                    "providerCalls": max(0, provider_calls),
                }
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except (CancellationRequested, AnalysisExecutionError) as exc:
            with self._lock:
                task.status = "cancelled" if isinstance(exc, CancellationRequested) or getattr(exc, "code", "") == "ANALYSIS_CANCELLED" else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:
            with self._lock:
                task.status = "cancelled" if task._cancel_event.is_set() else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        finally:
            with self._lock:
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
            _ = started

    def _run_report(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
        started = time.perf_counter()
        try:
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_substage: str | None = None,
                current_step: int | None = None,
                max_steps: int | None = None,
                run_id: str | None = None,
            ) -> None:
                self._set_stage(
                    task,
                    stage,
                    value,
                    current_file=current_file,
                    completed=completed,
                    total=total,
                    current_page=current_page,
                    current_substage=current_substage,
                    current_step=current_step,
                    max_steps=max_steps,
                    run_id=run_id or task.report_id,
                )

            self._set_stage(
                task,
                "report_generation",
                0.01,
                current_file="Analysis Report",
                completed=0,
                total=1,
                current_substage="loading_analysis",
                current_step=0,
                max_steps=1,
                run_id=task.report_id,
            )
            runner = task._report_runner
            if runner is None:
                raise ReportExecutionError("REPORT_FAILED", "report runner is unavailable")
            result = runner(progress, task._cancel_event)
            if task._cancel_event.is_set() or task.status == "cancelling":
                raise ReportExecutionError("REPORT_CANCELLED", "report generation was cancelled", stage="cancelled", retryable=True)
            with self._lock:
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.current_substage = "completed"
                task.current_step = 1
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.completed = 1
                task.total = 1
                task.counts = {"reports": 1}
                task.summary = {
                    "reportId": task.report_id,
                    "generationMode": result.get("generation_mode") if isinstance(result, Mapping) else None,
                }
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except (CancellationRequested, ReportExecutionError) as exc:
            with self._lock:
                task.status = "cancelled" if isinstance(exc, CancellationRequested) or getattr(exc, "code", "") == "REPORT_CANCELLED" else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:
            with self._lock:
                task.status = "cancelled" if task._cancel_event.is_set() else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        finally:
            with self._lock:
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
            _ = started

    def get(self, task_id: str) -> ProcessTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def begin_reset(self) -> None:
        """Raise the task-admission barrier for one workspace reset."""

        with self._lock:
            if self._reset_in_progress:
                raise TaskAdmissionError("a workspace reset is already in progress")
            self._reset_in_progress = True

    def end_reset(self) -> None:
        """Release the reset barrier after success or a safe failure."""

        with self._lock:
            self._reset_in_progress = False

    @property
    def reset_in_progress(self) -> bool:
        with self._lock:
            return self._reset_in_progress

    def list(self, *, limit: int = 50) -> list[ProcessTask]:
        with self._lock:
            values = sorted(self._tasks.values(), key=lambda item: item.task_id, reverse=True)
            return values[:limit]

    def clear_finished(self) -> int:
        """Drop terminal in-memory task records after a project reset."""

        terminal = {"succeeded", "failed", "cancelled", "interrupted"}
        with self._lock:
            task_ids = [task_id for task_id, task in self._tasks.items() if task.status in terminal]
            for task_id in task_ids:
                self._tasks.pop(task_id, None)
            return len(task_ids)

    def cancel(self, task_id: str) -> ProcessTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                return task
            task._cancel_event.set()
            future = task._future
            if task.status == "queued" and future is not None and future.cancel():
                task.status = "cancelled"
                task.current_stage = "cancelled"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, CancellationRequested())
                task.error_summary = task.error["message"]
            else:
                task.status = "cancelling"
                task.current_stage = "cancelling"
            return task

    def request_shutdown(self) -> None:
        with self._lock:
            self._shutdown_requested = True
            for task in self._tasks.values():
                if task.status == "queued":
                    task._cancel_event.set()
                    future = task._future
                    if future is not None and future.cancel():
                        task.status = "cancelled"
                        task.current_stage = "cancelled"
                        task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                        task.error = self._error_for(task, CancellationRequested())
                        task.error_summary = task.error["message"]
                    else:
                        # The coordinator may have started between submit and
                        # this stop request, before its first progress event.
                        # Treat that state as active so shutdown waits for its
                        # cancellation/recovery path instead of declaring the
                        # server drained while the task is still opening files.
                        task.status = "cancelling"
                        task.current_stage = "cancelling"
                elif task.status in {"running", "cancelling"}:
                    task.status = "cancelling"
                    task.current_stage = "cancelling"
                    task._cancel_event.set()

    def shutdown(self, *, timeout: float = 5.0) -> bool:
        """Request cancellation and wait a bounded time for the coordinator."""

        self.request_shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                active = [
                    task
                    for task in self._tasks.values()
                    if task.status in {"running", "cancelling"}
                ]
            if not active:
                return True
            time.sleep(0.05)
        with self._lock:
            for task in self._tasks.values():
                if task.status in {"running", "cancelling"}:
                    task.status = "interrupted"
                    task.current_stage = "interrupted"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, CancellationRequested())
                    task.error_summary = task.error["message"]
        return False
