"""Windows process ownership for the local ChongZu server.

This module intentionally uses only the standard library and Windows kernel32
APIs.  A project server owns one OS file lock for its lifetime and, when the
platform allows it, one Job Object for all of its descendants.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable

from chongzu import paths
from chongzu.locking import FileLock, LockUnavailable


SERVER_LOCK_NAME = paths.SERVER_LOCK_NAME
SERVER_START_LOCK_NAME = paths.SERVER_START_LOCK_NAME
SERVER_JOB_PREFIX = "Local\\ChongZu-"


def server_lock_path(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / "workspace" / "state" / SERVER_LOCK_NAME


def server_start_lock_path(project_root: Path | str) -> Path:
    """Return the short-lived lock used to serialize lifecycle start attempts."""

    return Path(project_root).resolve() / "workspace" / "state" / SERVER_START_LOCK_NAME


def acquire_server_lock(project_root: Path | str, *, metadata: dict[str, Any] | None = None) -> FileLock:
    lock = FileLock(server_lock_path(project_root))
    lock.acquire(timeout=0.0)
    if metadata:
        lock.write_metadata(metadata)
    return lock


def command_fingerprint(project_root: Path | str, port: int) -> str:
    """Hash the non-secret identity of a server command line."""

    payload = {
        "projectRoot": str(Path(project_root).resolve()).casefold(),
        "module": "chongzu.api.server",
        "host": "127.0.0.1",
        "port": int(port),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def project_root_fingerprint(project_root: Path | str) -> str:
    return hashlib.sha256(str(Path(project_root).resolve()).casefold().encode("utf-8")).hexdigest()


def job_name(project_root: Path | str, instance_id: str) -> str:
    digest = hashlib.sha256(f"{Path(project_root).resolve()}\0{instance_id}".encode("utf-8")).hexdigest()[:32]
    return f"{SERVER_JOB_PREFIX}{digest}"


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.ReadProcessMemory.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    _ntdll.NtQueryInformationProcess.restype = wintypes.LONG


class _ProcessBasicInformation(ctypes.Structure if os.name == "nt" else object):
    if os.name == "nt":
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]


class _UnicodeString(ctypes.Structure if os.name == "nt" else object):
    if os.name == "nt":
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]


def _filetime_seconds(value: wintypes.FILETIME) -> float:
    ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
    return ticks / 10_000_000 - 11_644_473_600


def _query_command_line(pid: int) -> str | None:
    """Read one process command line without spawning a diagnostic utility."""

    if os.name != "nt":  # pragma: no cover - production is Windows x64
        return None
    # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ. Protected/system
    # processes may deny this read; callers treat that as unverifiable.
    handle = _kernel32.OpenProcess(0x0400 | 0x0010, False, int(pid))
    if not handle:
        return None
    try:
        basic = _ProcessBasicInformation()
        returned = wintypes.ULONG()
        status = _ntdll.NtQueryInformationProcess(
            handle,
            0,  # ProcessBasicInformation
            ctypes.byref(basic),
            ctypes.sizeof(basic),
            ctypes.byref(returned),
        )
        if status != 0 or not basic.PebBaseAddress:
            return None
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        process_parameters_pointer = ctypes.c_void_p()
        bytes_read = ctypes.c_size_t()
        peb_offset = 0x20 if pointer_size == 8 else 0x10
        if not _kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(int(basic.PebBaseAddress) + peb_offset),
            ctypes.byref(process_parameters_pointer),
            pointer_size,
            ctypes.byref(bytes_read),
        ) or not process_parameters_pointer.value:
            return None
        command_line = _UnicodeString()
        command_offset = 0x70 if pointer_size == 8 else 0x40
        if not _kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(int(process_parameters_pointer.value) + command_offset),
            ctypes.byref(command_line),
            ctypes.sizeof(command_line),
            ctypes.byref(bytes_read),
        ):
            return None
        if not command_line.Buffer or not command_line.Length:
            return ""
        char_size = ctypes.sizeof(ctypes.c_wchar)
        count = int(command_line.Length) // char_size
        buffer = ctypes.create_unicode_buffer(count + 1)
        if not _kernel32.ReadProcessMemory(
            handle,
            command_line.Buffer,
            ctypes.byref(buffer),
            int(command_line.Length),
            ctypes.byref(bytes_read),
        ):
            return None
        return buffer.value
    except (OSError, TypeError, ValueError):
        return None
    finally:
        _kernel32.CloseHandle(handle)


def process_identity(pid: int) -> dict[str, Any] | None:
    """Return creation identity and executable path for one live process."""

    if os.name != "nt":  # pragma: no cover - production is Windows x64
        if pid != os.getpid():
            return None
        return {"pid": pid, "creationTime": None, "executable": str(Path(sys.executable).resolve())}
    query_limited = 0x1000
    handle = _kernel32.OpenProcess(query_limited, False, int(pid))
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not _kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        buffer = ctypes.create_unicode_buffer(32_768)
        size = wintypes.DWORD(len(buffer))
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        result: dict[str, Any] = {
            "pid": int(pid),
            "creationTime": _filetime_seconds(created),
            "executable": buffer.value,
        }
        command_line = _query_command_line(pid)
        if command_line is not None:
            result["commandLine"] = command_line
        return result
    finally:
        _kernel32.CloseHandle(handle)


class _ProcessEntry32W(ctypes.Structure if os.name == "nt" else object):
    if os.name == "nt":
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def descendant_processes(root_pid: int) -> list[dict[str, Any]]:
    """Return a best-effort snapshot of the current descendant process tree."""

    if os.name != "nt":  # pragma: no cover - production is Windows x64
        return []
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == invalid_handle:
        return []
    records: dict[int, dict[str, Any]] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            pid = int(entry.th32ProcessID)
            records[pid] = {
                "pid": pid,
                "parentPid": int(entry.th32ParentProcessID),
                "imageName": str(entry.szExeFile),
            }
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)

    children: dict[int, list[int]] = {}
    for record in records.values():
        children.setdefault(int(record["parentPid"]), []).append(int(record["pid"]))
    pending = list(children.get(int(root_pid), []))
    descendant_ids: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in descendant_ids:
            continue
        descendant_ids.add(pid)
        pending.extend(children.get(pid, []))
    result = []
    for pid in sorted(descendant_ids):
        record = dict(records.get(pid) or {"pid": pid})
        identity = process_identity(pid)
        if identity is not None:
            record.update(
                {
                    "creationTime": identity.get("creationTime"),
                    "executable": identity.get("executable"),
                }
            )
        result.append(record)
    return result


def wait_for_descendants_exit(root_pid: int, known_pids: Iterable[int] = (), timeout: float = 5.0) -> bool:
    """Wait until a captured/current descendant set no longer has live PIDs."""

    observed = {int(pid) for pid in known_pids if int(pid) > 0}
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        observed.update(int(record["pid"]) for record in descendant_processes(root_pid))
        if not any(process_identity(pid) is not None for pid in observed):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def wait_for_owned_tree_exit(root_pid: int, known_pids: Iterable[int] = (), timeout: float = 5.0) -> bool:
    """Wait for the server and a captured descendant set to disappear."""

    observed = {int(pid) for pid in known_pids if int(pid) > 0}
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        observed.update(int(record["pid"]) for record in descendant_processes(root_pid))
        root_alive = process_identity(root_pid) is not None
        descendants_alive = any(process_identity(pid) is not None for pid in observed)
        if not root_alive and not descendants_alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def wait_for_process_exit(pid: int, timeout: float) -> bool:
    if os.name != "nt":  # pragma: no cover - production is Windows x64
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.05)
        return process_identity(pid) is None
    synchronize = 0x00100000
    handle = _kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return True
    try:
        milliseconds = max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        return _kernel32.WaitForSingleObject(handle, milliseconds) == 0
    finally:
        _kernel32.CloseHandle(handle)


def terminate_process(pid: int, exit_code: int = 2) -> bool:
    """Terminate one already-verified PID without relying on image names.

    ``taskkill /T`` is the preferred Windows command when it is available, but
    a server inherited from another Job Object can return ``Access denied``
    even though the owning process itself has a usable terminate handle.  The
    lifecycle layer calls this only after checking the PID creation identity
    and executable path against a captured instance record.
    """

    if os.name != "nt":  # pragma: no cover - production is Windows x64
        try:
            os.kill(int(pid), 9)
        except OSError:
            return process_identity(int(pid)) is None
        return True
    # Ask only for PROCESS_TERMINATE.  An inherited Job Object may deny an
    # otherwise harmless SYNCHRONIZE right even though Windows permits the
    # caller to terminate this exact process.
    access = 0x0001  # PROCESS_TERMINATE
    handle = _kernel32.OpenProcess(access, False, int(pid))
    if not handle:
        return process_identity(int(pid)) is None
    try:
        if _kernel32.TerminateProcess(handle, int(exit_code)):
            return True
        return process_identity(int(pid)) is None
    finally:
        _kernel32.CloseHandle(handle)


def identity_matches(state: dict[str, Any], project_root: Path | str) -> bool:
    try:
        pid = int(state["pid"])
        expected_time = float(state["pidCreationTime"])
        expected_executable = str(state["executable"])
    except (KeyError, TypeError, ValueError):
        return False
    actual = process_identity(pid)
    if actual is None:
        return False
    try:
        actual_path = Path(str(actual["executable"])).resolve()
        expected_path = Path(expected_executable).resolve()
        project = Path(project_root).resolve()
    except (OSError, ValueError):
        return False
    if os.path.normcase(str(actual_path)) != os.path.normcase(str(expected_path)):
        return False
    if os.path.normcase(str(actual_path)).startswith(os.path.normcase(str(project)) + os.sep) is False:
        return False
    return abs(float(actual["creationTime"]) - expected_time) < 0.01


def server_command_matches(
    state: dict[str, Any],
    project_root: Path | str,
    *,
    identity: dict[str, Any] | None = None,
) -> bool:
    """Check that a live PID is running the ChongZu server command.

    Executable path and creation time prevent ordinary PID reuse, while this
    command check prevents an unrelated project-local ``python.exe`` from
    being mistaken for the server merely because it shares the interpreter.
    """

    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    actual = identity or process_identity(pid)
    if actual is None:
        return False
    command_line = str(actual.get("commandLine") or "")
    if not command_line:
        return False
    normalized = command_line.casefold().replace("/", "\\")
    project = str(Path(project_root).resolve()).casefold().replace("/", "\\")
    try:
        port = int(state["port"])
    except (KeyError, TypeError, ValueError):
        return False
    instance_id = str(state.get("instanceId") or "").casefold()
    required = (
        "-m chongzu.api.server",
        "--host 127.0.0.1",
        f"--port {port}",
        "--project-root",
        project,
        "--instance-id",
        instance_id,
    )
    return bool(instance_id) and all(item in normalized for item in required)


def process_record_matches(record: dict[str, Any], project_root: Path | str) -> bool:
    """Verify a captured descendant has not become a different process."""

    try:
        pid = int(record["pid"])
        expected_time = float(record["creationTime"])
        expected_executable = Path(str(record["executable"])).resolve()
        project = Path(project_root).resolve()
    except (KeyError, TypeError, ValueError, OSError):
        return False
    actual = process_identity(pid)
    if actual is None:
        return False
    try:
        actual_executable = Path(str(actual["executable"])).resolve()
        actual_time = float(actual["creationTime"])
    except (KeyError, TypeError, ValueError, OSError):
        return False
    if os.path.normcase(str(actual_executable)) != os.path.normcase(str(expected_executable)):
        return False
    if abs(actual_time - expected_time) >= 0.01:
        return False
    # The root process identity proves the captured record was in its tree.
    # Keep this additional path check for product Python workers; conhost is a
    # Windows-owned child executable and is permitted only because its exact
    # PID, executable, and creation identity were captured from that tree.
    if os.path.normcase(str(actual_executable)).startswith(os.path.normcase(str(project)) + os.sep):
        return True
    return str(record.get("imageName") or "").casefold() == "conhost.exe"


class _BasicLimitInformation(ctypes.Structure if os.name == "nt" else object):
    if os.name == "nt":
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


class _IOCounters(ctypes.Structure if os.name == "nt" else object):
    if os.name == "nt":
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


class _ExtendedLimitInformation(ctypes.Structure if os.name == "nt" else object):
    if os.name == "nt":
        _fields_ = [("BasicLimitInformation", _BasicLimitInformation), ("IoInfo", _IOCounters)]


@dataclass
class JobObject:
    """A named kill-on-close Job Object owned by the server process."""

    name: str
    handle: Any

    @classmethod
    def create_for_process(cls, project_root: Path | str, instance_id: str) -> "JobObject | None":
        if os.name != "nt":  # pragma: no cover - production is Windows x64
            return None
        name = job_name(project_root, instance_id)
        handle = _kernel32.CreateJobObjectW(None, name)
        if not handle:
            return None
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(handle)
            return None
        if not _kernel32.AssignProcessToJobObject(handle, _kernel32.GetCurrentProcess()):
            _kernel32.CloseHandle(handle)
            return None
        return cls(name=name, handle=handle)

    @classmethod
    def open_named(cls, name: str) -> "JobObject | None":
        if os.name != "nt" or not name:
            return None
        access = 0x0010000C  # SYNCHRONIZE | JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE
        handle = _kernel32.OpenJobObjectW(access, False, name)
        if not handle:
            return None
        return cls(name=name, handle=handle)

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or not self.handle:
            return False
        return bool(_kernel32.TerminateJobObject(self.handle, int(exit_code)))

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None

    def disarm_and_close(self) -> None:
        """Close after a confirmed graceful drain without killing the server."""

        if os.name != "nt" or not self.handle:
            return
        info = _ExtendedLimitInformation()
        if _kernel32.SetInformationJobObject(
            self.handle,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.close()
            return
        # If the limit cannot be cleared, leave the armed handle for process
        # exit; closing it here would terminate the still-running server.
        self.handoff_to_process_exit()

    def handoff_to_process_exit(self) -> None:
        """Keep kill-on-close armed until this server exits.

        Closing a kill-on-close Job from inside a member process would kill the
        server before its normal return path completes.  The OS closes this
        handle as the process exits, which preserves the crash guarantee.
        """

        self.handle = None


def expected_server_executable(project_root: Path | str) -> Path:
    root = Path(project_root).resolve()
    bundled = root / "runtime" / "python"
    candidates = list(bundled.glob("cpython-*/python.exe"))
    if candidates:
        return candidates[0].resolve()
    return Path(sys.executable).resolve()


__all__ = [
    "FileLock",
    "JobObject",
    "LockUnavailable",
    "acquire_server_lock",
    "command_fingerprint",
    "expected_server_executable",
    "descendant_processes",
    "job_name",
    "identity_matches",
    "process_identity",
    "project_root_fingerprint",
    "process_record_matches",
    "server_command_matches",
    "server_lock_path",
    "server_start_lock_path",
    "terminate_process",
    "wait_for_descendants_exit",
    "wait_for_owned_tree_exit",
    "wait_for_process_exit",
]
