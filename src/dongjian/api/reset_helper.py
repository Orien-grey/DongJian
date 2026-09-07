"""Project-local STOP -> RESET -> RESTART helper."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import uuid4

from dongjian import paths
from dongjian.locking import registry_write_mutex
from dongjian.registry import Registry, RegistryError
from dongjian.services.reset import WorkspaceResetError, WorkspaceResetJournal, WorkspaceResetService

from .lifecycle import DEFAULT_PORT, _read_state, start_server, stop_server


class ResetHelperError(RuntimeError):
    def __init__(self, phase: str, code: str) -> None:
        super().__init__(code)
        self.phase = phase
        self.code = code


def _run(project_root: Path, request_id: str, *, restart: bool) -> int:
    root = project_root.resolve()
    journal = WorkspaceResetJournal(root / "workspace")
    started_at = (journal.read(request_id) or {}).get("started_at")
    if not isinstance(started_at, str) or not started_at:
        started_at = journal._now()
    state_path = root / "workspace" / "state" / paths.SERVER_PID_NAME
    state = _read_state(state_path) if state_path.exists() else None
    old_server_pid = (state or {}).get("pid")
    helper_pid = os.getpid()
    try:
        port = int((state or {}).get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    server_stopped = False
    try:
        # The parent has finished its journal writes before spawning this
        # process.  Record supervisor ownership here so the parent and helper
        # cannot race a read-modify-write of the same journal file.
        journal.write(
            request_id=request_id,
            phase="supervisor_started",
            started_at=started_at,
            stage="supervisor_start",
            target_category="state",
            supervisor_started_at=journal._now(),
            helper_pid=helper_pid,
        )
        journal.write(
            request_id=request_id,
            phase="draining",
            started_at=started_at,
            stage="drain",
            target_category="state",
            supervisor_started_at=journal._now(),
            old_server_pid=old_server_pid,
            helper_pid=helper_pid,
            drain_policy="cancel_then_wait_then_owned_tree_termination",
        )
        if stop_server(root, reset=True) != 0:
            raise ResetHelperError("stopping_server", "server_stop_failed")
        server_stopped = True

        journal.write(
            request_id=request_id,
            phase="server_stopped",
            started_at=started_at,
            stage="stop",
            target_category="state",
            old_server_pid=old_server_pid,
            helper_pid=helper_pid,
            server_stopped_at=journal._now(),
            drain_completed="server process and owned descendants exited",
        )
        journal.write(
            request_id=request_id,
            phase="cleanup",
            started_at=started_at,
            stage="reset",
            target_category="output",
            cleanup_started_at=journal._now(),
            old_server_pid=old_server_pid,
            helper_pid=helper_pid,
        )
        WorkspaceResetService(root).reset(preserve_runtime_files=False)

        registry_path = root / "workspace" / "state" / "registry.duckdb"
        try:
            with registry_write_mutex(registry_path):
                registry = Registry.open(registry_path, initialize=False, read_only=True)
                try:
                    registry.schema_version()
                finally:
                    registry.close()
        except (RegistryError, OSError) as exc:
            raise WorkspaceResetError(
                "RESET_REGISTRY_FAILED",
                "无法验证重新创建的项目 Registry。",
                stage="registry_verify",
                diagnostic=f"{type(exc).__name__}:registry_recreated_not_readable",
                target_category="state",
                target_path_safe="workspace/state/registry.duckdb",
                errno_value=getattr(exc, "errno", None),
                winerror=getattr(exc, "winerror", None),
            ) from exc
        journal.write(request_id=request_id, phase="registry_recreated", started_at=started_at, stage="registry", target_category="state")

        if restart:
            journal.write(request_id=request_id, phase="restart", started_at=started_at, stage="restart", target_category="state")
            if start_server(root, port=port, open_browser=False) != 0:
                raise ResetHelperError("restarting_server", "server_restart_failed")
            new_state = _read_state(state_path) if state_path.exists() else None
            journal.write(
                request_id=request_id,
                phase="health_restored",
                started_at=started_at,
                stage="health",
                target_category="state",
                restart_pid=(new_state or {}).get("pid"),
                health_restored_at=journal._now(),
            )

        journal.write(
            request_id=request_id,
            phase="completed",
            started_at=started_at,
            finished_at=journal._now(),
            result="succeeded",
            stage="completed",
            target_category=None,
            safe_detail="reset_completed",
        )
        return 0
    except WorkspaceResetError as exc:
        code = str(exc.code or "reset_failed")
        failure_phase = str(exc.stage or "resetting_workspace")
        failure_exception_type = type(exc).__name__
        failure_target_category = exc.target_category or str(exc.removed.get("targetCategory") or "state")
        failure_target_path = exc.target_path_safe
        failure_errno = exc.errno
        failure_winerror = exc.winerror
        failure_safe_detail = exc.diagnostic or f"{failure_exception_type}:{code}"
        failure_completed_phases = list(exc.completed_phases)
        failure_removed = dict(exc.removed)
    except ResetHelperError as exc:
        code = exc.code
        failure_phase = exc.phase
        failure_exception_type = type(exc).__name__
        failure_target_category = "state"
        failure_target_path = None
        failure_errno = None
        failure_winerror = None
        failure_safe_detail = f"{failure_exception_type}:{code}"
        failure_completed_phases = []
        failure_removed = {}
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        code = type(exc).__name__.lower()
        failure_phase = "resetting_workspace"
        failure_exception_type = type(exc).__name__
        failure_target_category = "state"
        failure_target_path = None
        failure_errno = getattr(exc, "errno", None)
        failure_winerror = getattr(exc, "winerror", None)
        failure_safe_detail = f"{failure_exception_type}:{code}"
        failure_completed_phases = []
        failure_removed = {}
    except Exception:
        code = "unexpected_reset_failure"
        failure_phase = "resetting_workspace"
        failure_exception_type = "Exception"
        failure_target_category = "state"
        failure_target_path = None
        failure_errno = None
        failure_winerror = None
        failure_safe_detail = f"{failure_exception_type}:{code}"
        failure_completed_phases = []
        failure_removed = {}

    safe_error = f"{failure_phase}:{code}"[:512]
    journal.write(
        request_id=request_id,
        phase="failed",
        started_at=started_at,
        finished_at=journal._now(),
        result="failed",
        safe_error=safe_error,
        stage=failure_phase,
        target_category=failure_target_category,
        exception_type=failure_exception_type,
        safe_detail=failure_safe_detail,
            target_path_safe=failure_target_path,
            errno_value=failure_errno,
            winerror=failure_winerror,
            old_server_pid=old_server_pid,
            helper_pid=helper_pid,
            completed_phases=failure_completed_phases,
            removed=failure_removed,
        )
    # A failed cleanup must still be visible after a fresh server process when
    # the server was successfully stopped.  Do not hide a restart failure in
    # the journal; retain the original reset phase as the primary error.
    if restart and server_stopped:
        try:
            if start_server(root, port=port, open_browser=False) != 0:
                journal.write(
                    request_id=request_id,
                    phase="failed",
                    started_at=started_at,
                    finished_at=journal._now(),
                    result="failed",
                    safe_error=f"{safe_error};restart:server_restart_failed"[:512],
                )
        except Exception:
            journal.write(
                request_id=request_id,
                phase="failed",
                started_at=started_at,
                finished_at=journal._now(),
                result="failed",
                safe_error=f"{safe_error};restart:server_restart_failed"[:512],
            )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dongjian reset helper")
    parser.add_argument("--project-root", type=Path, default=paths.PROJECT_ROOT)
    parser.add_argument("--request-id", default=f"reset_{uuid4().hex}")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return _run(args.project_root, str(args.request_id), restart=not args.no_restart)


if __name__ == "__main__":
    raise SystemExit(main())
