"""Threaded localhost HTTP server and static frontend host."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .app import ApiError, BackendApp


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

            def _send_error(self, error: ApiError) -> None:
                request_id = getattr(self, "request_id", "req_unknown")
                self._send_json(
                    error.status,
                    {"error": {"code": error.code, "message": error.message, "requestId": request_id}},
                )

            def _request_body(self) -> bytes:
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ApiError("invalid_content_length", "Content-Length must be an integer") from exc
                if length < 0 or length > 64 * 1024:
                    raise ApiError("request_too_large", "request body exceeds the local limit", 413)
                return self.rfile.read(length)

            def _dispatch(self, body: bytes = b"") -> None:
                parsed = urlsplit(self.path)
                self.request_id = f"req_{__import__('uuid').uuid4().hex}"
                if parsed.path.startswith("/api/"):
                    if parsed.path == "/api/v1/internal/shutdown" and self.command == "POST":
                        if not app_server_token_matches(self.server, self.headers.get("X-ChongZu-Control")):
                            self._send_error(ApiError("not_found", "resource was not found", 404))
                            return
                        self._send_json(202, {"status": "shutting_down"})
                        threading.Thread(target=self.server.shutdown, name="chongzu-shutdown", daemon=True).start()
                        return
                    try:
                        response = app.handle_api(self.command, parsed.path, parse_qs(parsed.query), body)
                        self._send_json(response.status, response.payload)
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
                try:
                    self._dispatch(self._request_body())
                except ApiError as exc:
                    self._send_error(exc)

            def do_PATCH(self) -> None:  # noqa: N802
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
) -> int:
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[3]
    logger = _logger(root)
    app = BackendApp(project_root=root, logger=logger)
    try:
        server = ChongZuHTTPServer((host, port), app, control_token=control_token)
    except OSError as exc:
        logger.error("could not bind %s:%s: %s", host, port, exc, exc_info=True)
        app.close()
        print(f"ChongZu server could not bind {host}:{port}: {exc}", file=sys.stderr)
        return 2
    print(f"ChongZu local server listening at http://{host}:{port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chongzu local server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--control-token", default=None)
    args = parser.parse_args(argv)
    if args.host != DEFAULT_HOST:
        parser.error("server host must remain 127.0.0.1")
    if not 1 <= args.port <= 65_535:
        parser.error("port must be between 1 and 65535")
    return run_server(host=args.host, port=args.port, project_root=args.project_root, control_token=args.control_token)


if __name__ == "__main__":
    raise SystemExit(main())
