"""Atomic, workspace-contained text and PDF profile artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

def workspace_relative(path: Path, workspace_root: Path) -> str:
    return path.resolve().relative_to(workspace_root.resolve()).as_posix()


def artifact_absolute(relative_path: str, workspace_root: Path) -> Path:
    candidate = (workspace_root / Path(relative_path)).resolve()
    candidate.relative_to(workspace_root.resolve())
    return candidate


def _temporary_target(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.with_name(f".{uuid4().hex[:12]}.tmp")


def write_text_atomic(target: Path, content: str) -> None:
    temporary = _temporary_target(target)
    try:
        temporary.write_text(content, encoding="utf-8", newline="")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(target: Path, payload: dict[str, Any]) -> None:
    temporary = _temporary_target(target)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def text_asset_targets(workspace_root: Path, text_asset_id: str) -> dict[str, Path]:
    target_dir = workspace_root / "artifacts" / "text" / text_asset_id
    return {
        "raw": target_dir / "raw.txt",
        "normalized": target_dir / "normalized.txt",
        "metadata": target_dir / "metadata.json",
    }


def profile_target(workspace_root: Path, file_id: str, content_sha256: str) -> Path:
    return workspace_root / "artifacts" / "pdf_profiles" / file_id / content_sha256 / "profile.json"


def write_text_asset(
    *,
    workspace_root: Path,
    text_asset_id: str,
    raw_text: str,
    normalized_text: str,
    metadata: dict[str, Any],
) -> dict[str, str]:
    targets = text_asset_targets(workspace_root, text_asset_id)
    write_text_atomic(targets["raw"], raw_text)
    write_text_atomic(targets["normalized"], normalized_text)
    write_json_atomic(targets["metadata"], metadata)
    return {name: workspace_relative(path, workspace_root) for name, path in targets.items()}
