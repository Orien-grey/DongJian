"""Start/stop helpers for the product's own localhost server process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser

from .app import APP_NAME
from .server import DEFAULT_HOST, DEFAULT_PORT
from chongzu import paths


PID_FILE_NAME = "server.pid"
START_TIMEOUT_SECONDS = 20.0


def _state_file(project_root: Path) -> Path:
    return project_root / "workspace" / "state" / PID_FILE_NAME


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _healthy(port: int) -> bool:
    try:
        with urlopen(Request(f"http://{DEFAULT_HOST}:{port}/api/v1/health", method="GET"), timeout=0.6) as response:
            if response.status != 200:
                return False
            value = json.loads(response.read(64 * 1024).decode("utf-8"))
            return value.get("app", {}).get("name") == APP_NAME
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex((DEFAULT_HOST, port)) == 0


def start_server(project_root: Path, *, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    if not (project_root / "frontend" / "dist" / "index.html").is_file():
        print("ChongZu server cannot start: frontend/dist/index.html is missing; run the frontend build", file=sys.stderr)
        return 2
    state_path = _state_file(project_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_state(state_path) if state_path.exists() else None
    if existing and _healthy(int(existing.get("port", port))):
        active_port = int(existing.get("port", port))
        print(f"ChongZu server already running at http://{DEFAULT_HOST}:{active_port}/")
        if open_browser:
            webbrowser.open(f"http://{DEFAULT_HOST}:{active_port}/")
        return 0
    if state_path.exists():
        state_path.unlink(missing_ok=True)
    if _port_in_use(port):
        print(f"ChongZu server cannot start: {DEFAULT_HOST}:{port} is already in use", file=sys.stderr)
        return 2
    log_path = project_root / "workspace" / "logs" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    control_token = secrets.token_hex(32)
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
    state_path.write_text(
        json.dumps(
            {"pid": process.pid, "port": port, "startedAt": time.time(), "controlToken": control_token},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _healthy(port):
            print(f"ChongZu server started at http://{DEFAULT_HOST}:{port}/ (PID {process.pid})")
            if open_browser:
                webbrowser.open(f"http://{DEFAULT_HOST}:{port}/")
            return 0
        if process.poll() is not None:
            state_path.unlink(missing_ok=True)
            print("ChongZu server exited before health check; see workspace/logs/server.log", file=sys.stderr)
            return 2
        time.sleep(0.2)
    print("ChongZu server did not become healthy; see workspace/logs/server.log", file=sys.stderr)
    return 2


def stop_server(project_root: Path) -> int:
    state_path = _state_file(project_root)
    state = _read_state(state_path) if state_path.exists() else None
    if not state:
        state_path.unlink(missing_ok=True)
        print("ChongZu server is not running")
        return 0
    try:
        pid = int(state["pid"])
        port = int(state.get("port", DEFAULT_PORT))
    except (KeyError, TypeError, ValueError):
        state_path.unlink(missing_ok=True)
        print("Removed stale ChongZu server state")
        return 0
    # A stale PID must never be used to kill an unrelated process.  Health is
    # checked on the recorded localhost port before taskkill is issued.
    if not _healthy(port):
        state_path.unlink(missing_ok=True)
        print("ChongZu server is not healthy; removed stale server state")
        return 0
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
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _healthy(port):
                time.sleep(0.1)
            if not _healthy(port):
                state_path.unlink(missing_ok=True)
                print(f"Stopped ChongZu server PID {pid}")
                return 0
        except (OSError, URLError, ValueError):
            pass
    if sys.platform == "win32":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            print("ChongZu server could not be stopped; recorded PID was not terminated", file=sys.stderr)
            return 2
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
