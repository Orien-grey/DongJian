"""Threaded localhost HTTP server and static frontend host."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .app import ApiError, BackendApp
from .ownership import JobObject, LockUnavailable, acquire_server_lock, job_name


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18_765


def _logger(project_root: Path) -> logging.Logger:
    log_dir = project_root / "workspace" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("chongzu.api")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_dir / "server.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


class ChongZuHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: BackendApp, *, control_token: str | None = None):
        self.app = app
        self.control_token = control_token
        super().__init__(address, self._handler_type())

    def _handler_type(self):
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            server_version = "ChongZuLocal/1"

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(encoded)

            def _send_response(self, response: Any) -> None:
                if getattr(response, "raw_body", None) is None:
                    self._send_json(response.status, response.payload)
                    return
                encoded = response.raw_body
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(encoded)

            def _send_error(self, error: ApiError) -> None:
                request_id = self._ensure_request_id()
                # Keep the browser response concise while retaining a stable
                # correlation point in the server log.  The error object is
                # already sanitized by BackendApp; no traceback or secret is
                # sent to the client here.
                app.logger.warning(
                    "request %s api error code=%s status=%d retryable=%s",
                    request_id,
                    error.code,
                    error.status,
                    bool(getattr(error, "retryable", False)),
                )
                payload: dict[str, Any] = {
                    "code": error.code,
                    "message": error.message,
                    "retryable": bool(getattr(error, "retryable", False)),
                    "requestId": request_id,
                }
                diagnostic = getattr(error, "diagnostic", None)
                category = getattr(error, "category", None)
                if diagnostic:
                    payload["diagnostic"] = str(diagnostic)[:512]
                if category:
                    payload["category"] = str(category)[:80]
                stage = getattr(error, "stage", None)
                if stage:
                    payload["stage"] = str(stage)[:80]
                details = getattr(error, "details", None)
                if isinstance(details, dict) and details:
                    # Backend services only place bounded, non-secret reset
                    # diagnostics here.  Keep the response contract narrow so
                    # an accidental exception object or arbitrary value can
                    # never be serialized to the browser.
                    payload["details"] = details
                self._send_json(error.status, {"error": payload})

            def _request_body(self) -> bytes:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ApiError("invalid_content_length", "Content-Length must be an integer") from exc
                if length < 0 or length > 64 * 1024:
                    raise ApiError("request_too_large", "request body exceeds the local limit", 413)
                return self.rfile.read(length)

            def _ensure_request_id(self) -> str:
                request_id = getattr(self, "request_id", None)
                if not request_id:
                    request_id = f"req_{uuid4().hex}"
                    self.request_id = request_id
                return request_id

            def _dispatch(self, body: bytes = b"") -> None:
                parsed = urlsplit(self.path)
                self._ensure_request_id()
                if parsed.path.startswith("/api/"):
                    if parsed.path == "/api/v1/internal/shutdown" and self.command == "POST":
                        if not app_server_token_matches(self.server, self.headers.get("X-ChongZu-Control")):
                            self._send_error(ApiError("not_found", "resource was not found", 404))
                            return
                        app.request_shutdown()
                        self._send_json(202, {"status": "shutting_down"})
                        threading.Thread(target=self.server.shutdown, name="chongzu-shutdown", daemon=True).start()
                        return
                    try:
                        response = app.handle_api(
                            self.command,
                            parsed.path,
                            parse_qs(parsed.query),
                            body,
                            request_id=self.request_id,
                        )
                        self._send_response(response)
                    except ApiError as exc:
                        self._send_error(exc)
                    return
                if self.command not in {"GET", "HEAD"}:
                    self._send_error(ApiError("method_not_allowed", "method is not supported", 405))
                    return
                try:
                    file_path, content_type = app.static_file(self.path)
                    content = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(content)
                except ApiError as exc:
                    self._send_error(exc)
                except OSError:
                    app.logger.error("request %s could not read static asset", self.request_id, exc_info=True)
                    self._send_error(ApiError("static_read_error", "the frontend asset could not be read", 500))

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch()

            def do_HEAD(self) -> None:  # noqa: N802
                self._dispatch()

            def do_POST(self) -> None:  # noqa: N802
                self._ensure_request_id()
                try:
                    self._dispatch(self._request_body())
                except ApiError as exc:
                    self._send_error(exc)

            def do_PATCH(self) -> None:  # noqa: N802
                self._ensure_request_id()
                try:
                    self._dispatch(self._request_body())
                except ApiError as exc:
                    self._send_error(exc)

            def do_PUT(self) -> None:  # noqa: N802
                # BackendApp exposes additive PUT routes (currently the
                # project-local AI settings save).  Keep body handling exactly
                # aligned with POST/PATCH so the HTTP adapter does not turn a
                # valid backend route into the stdlib's 501 response.
                self._ensure_request_id()
                try:
                    self._dispatch(self._request_body())
                except ApiError as exc:
                    self._send_error(exc)

            def log_message(self, format: str, *args: Any) -> None:
                app.logger.info("%s - %s", self.address_string(), format % args)

        return Handler


def app_server_token_matches(server: ChongZuHTTPServer, token: str | None) -> bool:
    """Keep the lifecycle-only shutdown route bound to the recorded server."""

    return bool(server.control_token and token and token == server.control_token)


def run_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    project_root: Path | str | None = None,
    control_token: str | None = None,
    instance_id: str | None = None,
) -> int:
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[3]
    logger = _logger(root)
    instance = instance_id or f"server_{uuid4().hex}"
    try:
        ownership_lock = acquire_server_lock(
            root,
            metadata={"pid": __import__("os").getpid(), "instanceId": instance, "port": port},
        )
    except LockUnavailable:
        print("ChongZu 项目已有运行中的服务实例，当前服务未启动。", file=sys.stderr)
        return 3
    job = JobObject.create_for_process(root, instance)
    if job is None and sys.platform == "win32":
        logger.warning("Windows Job Object unavailable; using verified process-tree fallback")
    app: BackendApp | None = None

    def finish_ownership(graceful: bool) -> None:
        if app is not None:
            completed = app.close(timeout=5.0)
            graceful = graceful and completed
        if job is not None:
            if graceful:
                job.disarm_and_close()
            else:
                job.handoff_to_process_exit()
        if graceful:
            ownership_lock.release()

    try:
        app = BackendApp(
            project_root=root,
            logger=logger,
            instance_id=instance,
            server_pid=os.getpid(),
        )
    except Exception as exc:  # noqa: BLE001 - startup error is logged and sanitized
        logger.error("server startup failed: %s", exc, exc_info=True)
        print("ChongZu server 启动失败，请查看 workspace/logs/server.log。", file=sys.stderr)
        finish_ownership(False)
        return 2
    try:
        server = ChongZuHTTPServer((host, port), app, control_token=control_token)
    except OSError as exc:
        logger.error("could not bind %s:%s: %s", host, port, exc, exc_info=True)
        finish_ownership(True)
        print(f"ChongZu server could not bind {host}:{port}: {exc}", file=sys.stderr)
        return 2
    print(f"ChongZu local server listening at http://{host}:{port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        finish_ownership(True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chongzu local server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--control-token", default=None)
    parser.add_argument("--instance-id", default=None)
    args = parser.parse_args(argv)
    if args.host != DEFAULT_HOST:
        parser.error("server host must remain 127.0.0.1")
    if not 1 <= args.port <= 65_535:
        parser.error("port must be between 1 and 65535")
    return run_server(
        host=args.host,
        port=args.port,
        project_root=args.project_root,
        control_token=args.control_token,
        instance_id=args.instance_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
