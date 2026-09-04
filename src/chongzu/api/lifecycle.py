"""Start/stop helpers for the product's own localhost server process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser
from uuid import uuid4

from .app import APP_NAME
from .ownership import (
    FileLock,
    JobObject,
    LockUnavailable,
    acquire_server_lock,
    command_fingerprint,
    descendant_processes,
    identity_matches,
    job_name,
    process_identity,
    process_record_matches,
    project_root_fingerprint,
    server_command_matches,
    server_start_lock_path,
    terminate_process,
    wait_for_owned_tree_exit,
)
from .server import DEFAULT_HOST, DEFAULT_PORT
from chongzu import paths


PID_FILE_NAME = paths.SERVER_PID_NAME
START_TIMEOUT_SECONDS = 20.0
GRACEFUL_STOP_TIMEOUT_SECONDS = 5.0
FORCED_STOP_VERIFY_TIMEOUT_SECONDS = 5.0


def _state_file(project_root: Path) -> Path:
    return project_root / "workspace" / "state" / PID_FILE_NAME


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _state_identity_matches(state: dict[str, object], project_root: Path, port: int) -> bool:
    return (
        _state_scope_matches(state, project_root, port)
        and identity_matches(state, project_root)
        and server_command_matches(state, project_root)
    )


def _state_scope_matches(state: dict[str, object], project_root: Path, port: int) -> bool:
    """Return whether a PID record belongs to this project/command scope."""

    return (
        state.get("projectRootFingerprint") == project_root_fingerprint(project_root)
        and state.get("commandFingerprint") == command_fingerprint(project_root, port)
    )


def _healthy(
    port: int,
    *,
    expected_pid: int | None = None,
    expected_instance: str | None = None,
) -> bool:
    try:
        with urlopen(Request(f"http://{DEFAULT_HOST}:{port}/api/v1/health", method="GET"), timeout=0.6) as response:
            if response.status != 200:
                return False
            value = json.loads(response.read(64 * 1024).decode("utf-8"))
            if value.get("app", {}).get("name") != APP_NAME:
                return False
            server = value.get("server") or {}
            if expected_pid is not None:
                try:
                    if int(server.get("pid")) != int(expected_pid):
                        return False
                except (TypeError, ValueError):
                    return False
            if expected_instance is not None and server.get("instanceId") != expected_instance:
                return False
            return True
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def _port_in_use(port: int) -> bool:
    # A connect-only probe can miss a listener during startup races.  A bind
    # probe asks the kernel whether this product port is actually available.
    # SO_EXCLUSIVEADDRUSE is available on Windows and prevents a permissive
    # reuse flag from making the check report a false negative.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((DEFAULT_HOST, port))
        except OSError:
            return True
    return False


def _terminate_started_process(pid: int) -> None:
    """Terminate only the server process created by this start attempt."""

    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:  # pragma: no cover - production is Windows x64
        import os

        os.kill(pid, 15)
    except OSError:
        pass


def _start_server_locked(project_root: Path, *, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    if not (project_root / "frontend" / "dist" / "index.html").is_file():
        print("ChongZu server cannot start: frontend/dist/index.html is missing; run the frontend build", file=sys.stderr)
        return 2
    state_path = _state_file(project_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_state(state_path) if state_path.exists() else None
    if existing:
        try:
            active_port = int(existing.get("port", port))
        except (TypeError, ValueError):
            active_port = port
        try:
            existing_pid = int(existing.get("pid"))
        except (TypeError, ValueError):
            existing_pid = None
        existing_instance = existing.get("instanceId")
        if not isinstance(existing_instance, str):
            existing_instance = None
        if _healthy(
            active_port,
            expected_pid=existing_pid,
            expected_instance=existing_instance,
        ) and _state_identity_matches(existing, project_root, active_port):
            print(f"ChongZu server already running at http://{DEFAULT_HOST}:{active_port}/")
            if open_browser:
                webbrowser.open(f"http://{DEFAULT_HOST}:{active_port}/")
            return 0
    try:
        startup_probe = acquire_server_lock(project_root)
    except LockUnavailable:
        print("ChongZu 项目已有运行中的服务实例，当前服务未启动。", file=sys.stderr)
        return 3
    startup_probe.release()
    if state_path.exists():
        # A crashed server can leave a ProcessPool worker alive after the
        # lifetime lock has been released.  Before replacing stale state,
        # clean only descendants of a dead root whose project/command scope
        # still matches.  A live or reused PID is never treated as ours.
        if existing and _state_scope_matches(existing, project_root, active_port):
            try:
                previous_pid = int(existing.get("pid"))
            except (TypeError, ValueError):
                previous_pid = 0
            if previous_pid > 0 and process_identity(previous_pid) is None:
                if not _cleanup_orphaned_descendants(root_pid=previous_pid, project_root=project_root):
                    print("ChongZu server 上一次异常退出后仍有无法确认归属的子进程，已取消启动", file=sys.stderr)
                    return 2
        state_path.unlink(missing_ok=True)
    if _port_in_use(port):
        print(f"ChongZu server cannot start: {DEFAULT_HOST}:{port} is already in use", file=sys.stderr)
        return 2
    log_path = project_root / "workspace" / "logs" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    control_token = secrets.token_hex(32)
    instance_id = f"server_{uuid4().hex}"
    command = [
        sys.executable,
        "-m",
        "chongzu.api.server",
        "--host",
        DEFAULT_HOST,
        "--port",
        str(port),
        "--project-root",
        str(project_root),
        "--control-token",
        control_token,
        "--instance-id",
        instance_id,
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    identity = None
    for _ in range(20):
        identity = process_identity(process.pid)
        if identity is not None:
            break
        time.sleep(0.05)
    if identity is None:
        _terminate_started_process(process.pid)
        print("ChongZu server 无法确认启动进程身份，已停止启动。", file=sys.stderr)
        return 2
    _write_state(
        state_path,
        {
            "pid": process.pid,
            "pidCreationTime": identity.get("creationTime"),
            "executable": identity.get("executable"),
            "projectRootFingerprint": project_root_fingerprint(project_root),
            "commandFingerprint": command_fingerprint(project_root, port),
            "instanceId": instance_id,
            "jobName": job_name(project_root, instance_id),
            "port": port,
            "startedAt": time.time(),
            "controlToken": control_token,
        },
    )
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _healthy(port, expected_pid=process.pid, expected_instance=instance_id):
            print(f"ChongZu server started at http://{DEFAULT_HOST}:{port}/ (PID {process.pid})")
            if open_browser:
                webbrowser.open(f"http://{DEFAULT_HOST}:{port}/")
            return 0
        if process.poll() is not None:
            state_path.unlink(missing_ok=True)
            print("ChongZu server exited before health check; see workspace/logs/server.log", file=sys.stderr)
            return 2
        time.sleep(0.2)
    _terminate_started_process(process.pid)
    state_path.unlink(missing_ok=True)
    if _port_in_use(port):
        print(f"ChongZu server cannot start: {DEFAULT_HOST}:{port} became occupied during startup", file=sys.stderr)
    else:
        print("ChongZu server did not become healthy; see workspace/logs/server.log", file=sys.stderr)
    return 2


def start_server(project_root: Path, *, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    """Serialize launch, state publication, and health verification.

    The server lock is acquired by the child for its full lifetime. This
    separate short-lived lock closes the start-vs-start race before either
    child has acquired the lifetime lock or written ``server.pid``.
    """

    startup_lock = FileLock(server_start_lock_path(project_root))
    try:
        startup_lock.acquire(timeout=0.0)
    except LockUnavailable:
        print("ChongZu server start is already in progress; please try again shortly", file=sys.stderr)
        return 3
    try:
        return _start_server_locked(project_root, port=port, open_browser=open_browser)
    finally:
        startup_lock.release()


def _project_lock_available(project_root: Path) -> bool:
    try:
        probe = acquire_server_lock(project_root)
    except LockUnavailable:
        return False
    probe.release()
    return True


def _wait_for_verified_root_exit(
    state: dict[str, object],
    project_root: Path,
    timeout: float,
) -> bool:
    """Wait for the recorded root identity to disappear or change."""

    deadline = time.monotonic() + max(0.0, timeout)
    while identity_matches(state, project_root):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _terminate_verified_descendants(records: list[dict[str, object]], project_root: Path) -> None:
    """Terminate only descendants captured from this verified server tree."""

    if sys.platform != "win32":  # pragma: no cover - production is Windows x64
        return

    by_pid = {
        int(record["pid"]): record
        for record in records
        if record.get("pid") is not None
    }

    def depth(record: dict[str, object]) -> int:
        current = int(record.get("pid") or 0)
        seen: set[int] = set()
        value = 0
        while current in by_pid and current not in seen:
            seen.add(current)
            current = int(by_pid[current].get("parentPid") or 0)
            value += 1
        return value

    # Terminate grandchildren before their worker parent.  This matters on
    # Windows when taskkill cannot traverse a process inherited from an
    # incompatible outer Job Object.
    for record in sorted(records, key=depth, reverse=True):
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        if not process_record_matches(record, project_root):
            continue
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 and process_identity(pid) is not None:
            # A process can be an exact member of this captured tree while
            # taskkill is denied by an inherited Job Object.  Fall back to a
            # handle-based termination of this same verified PID only.
            terminate_process(pid)


def _cleanup_orphaned_descendants(
    *,
    root_pid: int,
    project_root: Path,
    timeout: float = FORCED_STOP_VERIFY_TIMEOUT_SECONDS,
) -> bool:
    """Clean descendants whose verified server root already died.

    Windows keeps the original parent PID in the process snapshot long enough
    for this targeted recovery.  Each candidate is still checked by its
    captured executable and creation identity before termination.
    """

    records = descendant_processes(root_pid)
    pids = [int(item["pid"]) for item in records if item.get("pid")]
    _terminate_verified_descendants(records, project_root)
    return wait_for_owned_tree_exit(root_pid, pids, timeout)


def stop_server(project_root: Path) -> int:
    state_path = _state_file(project_root)
    state = _read_state(state_path) if state_path.exists() else None
    if not state:
        try:
            probe = acquire_server_lock(project_root)
        except LockUnavailable:
            print("ChongZu server 正在运行，但缺少可验证的 PID state，未执行终止。", file=sys.stderr)
            return 2
        probe.release()
        state_path.unlink(missing_ok=True)
        print("ChongZu server is not running")
        return 0
    try:
        pid = int(state["pid"])
        port = int(state.get("port", DEFAULT_PORT))
    except (KeyError, TypeError, ValueError):
        if not _project_lock_available(project_root):
            print("ChongZu server state 无法验证，且项目仍被占用；未执行终止。", file=sys.stderr)
            return 2
        state_path.unlink(missing_ok=True)
        print("已清理无法验证的 ChongZu server state")
        return 0
    verified_identity = _state_identity_matches(state, project_root, port)
    if not verified_identity:
        # A dead PID is safe to forget only after the project lock confirms
        # that no current server owns the project.  A live but mismatching PID
        # is never terminated: it may be a reused PID or another program.
        if process_identity(pid) is not None or not _project_lock_available(project_root):
            print("ChongZu server 身份无法确认，未执行终止。", file=sys.stderr)
            return 2
        if not _state_scope_matches(state, project_root, port):
            # A legacy or mismatching record is safe to forget once both the
            # PID identity and the project lock say that no current server is
            # using it.  Without a matching scope fingerprint, descendants
            # cannot be attributed to this ChongZu instance safely.
            state_path.unlink(missing_ok=True)
            print("已清理不属于当前 ChongZu 实例的 stale state")
            return 0
        # A crash can leave a multiprocessing worker with the dead server PID
        # as its recorded parent.  The root PID is no longer reusable here,
        # the project lock is free, and descendant candidates are verified by
        # their own creation time/executable before any targeted termination.
        if not _cleanup_orphaned_descendants(root_pid=pid, project_root=project_root):
            print("ChongZu server 已退出，但仍有无法清理的项目子进程；保留 PID state。", file=sys.stderr)
            return 2
        state_path.unlink(missing_ok=True)
        print("已清理未运行的 ChongZu server state")
        return 0

    # Capture the tree before asking the server to drain.  If a worker becomes
    # orphaned during a forced path, its original PID and creation identity
    # remain available for the final ownership check.
    known_descendant_records = [item for item in descendant_processes(pid) if item.get("pid")]
    known_descendants = [int(item["pid"]) for item in known_descendant_records]
    control_token = state.get("controlToken")
    if isinstance(control_token, str) and control_token:
        try:
            request = Request(
                f"http://{DEFAULT_HOST}:{port}/api/v1/internal/shutdown",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json", "X-ChongZu-Control": control_token},
            )
            with urlopen(request, timeout=2) as response:
                if response.status not in {200, 202}:
                    raise OSError(f"shutdown returned HTTP {response.status}")
            if wait_for_owned_tree_exit(pid, known_descendants, GRACEFUL_STOP_TIMEOUT_SECONDS):
                state_path.unlink(missing_ok=True)
                print(f"Stopped ChongZu server PID {pid}")
                return 0
        except (OSError, URLError, ValueError):
            pass
    if not verified_identity:
        print("ChongZu server 未在 graceful timeout 内退出，且无法验证进程归属；未强制终止。", file=sys.stderr)
        return 2
    if sys.platform == "win32":
        # The graceful request can make the coordinator leave its HTTP loop
        # before the lifecycle helper reaches the fallback.  Refresh the
        # captured tree while the root still has the same identity; this
        # catches workers that spawned just after the initial snapshot without
        # ever treating a reused PID as belonging to this instance.
        if identity_matches(state, project_root):
            refreshed = descendant_processes(pid)
            by_pid = {int(item["pid"]): item for item in known_descendant_records if item.get("pid")}
            by_pid.update({int(item["pid"]): item for item in refreshed if item.get("pid")})
            known_descendant_records = list(by_pid.values())
            known_descendants = list(by_pid)
        current_job = None
        recorded_job = state.get("jobName")
        if isinstance(recorded_job, str) and recorded_job == job_name(project_root, str(state.get("instanceId") or "")):
            current_job = JobObject.open_named(recorded_job)
        if current_job is not None:
            terminated = current_job.terminate(exit_code=2)
            current_job.close()
            if not terminated:
                current_job = None
        if current_job is None:
            # This is still a targeted fallback: the PID and creation identity
            # were checked before the graceful request.  The root may already
            # have exited by now; in that case it is unsafe to use the PID
            # again, but the exact descendants captured from its tree remain
            # safe targeted termination candidates.  Never use image-name
            # termination, which could affect unrelated Python processes.
            _terminate_verified_descendants(known_descendant_records, project_root)
            root_still_owned = identity_matches(state, project_root)
            if root_still_owned:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0 and process_identity(pid) is not None:
                    terminate_process(pid)
                if not _wait_for_verified_root_exit(state, project_root, FORCED_STOP_VERIFY_TIMEOUT_SECONDS):
                    print("ChongZu server 无法停止；已验证的 PID 未被终止。", file=sys.stderr)
                    return 2
        if not wait_for_owned_tree_exit(pid, known_descendants, FORCED_STOP_VERIFY_TIMEOUT_SECONDS):
            print("ChongZu server 强制终止后仍未退出，保留 PID state 供再次诊断。", file=sys.stderr)
            return 2
        state_path.unlink(missing_ok=True)
        print(f"Stopped ChongZu server PID {pid}")
        return 0
    else:  # pragma: no cover - production is Windows x64
        try:
            import os

            os.kill(pid, 15)
        except OSError:
            pass
    state_path.unlink(missing_ok=True)
    print(f"Stopped ChongZu server PID {pid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chongzu server lifecycle")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--project-root", type=Path, default=paths.PROJECT_ROOT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    if args.action == "start":
        return start_server(root, port=args.port, open_browser=not args.no_browser)
    return stop_server(root)


if __name__ == "__main__":
    raise SystemExit(main())
