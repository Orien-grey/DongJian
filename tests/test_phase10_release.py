"""Small Phase 10 release/lifecycle contract checks."""

from __future__ import annotations

import socket
from pathlib import Path

from chongzu import doctor
from chongzu.api.lifecycle import _port_in_use


def test_lifecycle_bind_probe_detects_listening_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        assert _port_in_use(port) is True


def test_missing_provisioning_tools_are_optional(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        doctor.paths,
        "CORE_DIRECTORIES",
        {
            "runtime_uv": tmp_path / "uv",
            "runtime_venv": tmp_path / "venv",
        },
    )
    report = doctor.DoctorReport()
    doctor._check_directories(report)
    assert {check.name: check.status for check in report.checks} == {
        "runtime_uv": "INFO",
        "runtime_venv": "INFO",
    }
    assert report.ok
