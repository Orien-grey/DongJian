"""Small standard-library configuration model for Phase 1."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class ProjectConfig:
    """Non-secret defaults shared by future commands.

    Heavy parser settings intentionally do not appear here yet. They will be
    added with their owning extraction phase and an explicit schema version.
    """

    project_root: Path = paths.PROJECT_ROOT
    input_root: Path = paths.INPUT_ROOT
    staging_root: Path = paths.STAGING_ROOT
    output_root: Path = paths.OUTPUT_ROOT
    state_root: Path = paths.STATE_ROOT
    logs_root: Path = paths.LOGS_ROOT
    pipeline_version: str = "phase1-foundation"
    schema_version: str = "0"
    fast_workers: int = max(1, min(8, os.cpu_count() or 1))

    def __post_init__(self) -> None:
        for name in (
            "project_root",
            "input_root",
            "staging_root",
            "output_root",
            "state_root",
            "logs_root",
        ):
            value = getattr(self, name)
            if not paths.is_within_project(Path(value)):
                raise ValueError(f"{name} must remain below project root: {value}")
        if self.fast_workers < 1:
            raise ValueError("fast_workers must be positive")

    def as_dict(self) -> dict[str, object]:
        """Return a serialization-friendly view without adding a dependency."""

        values = asdict(self)
        return {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}


def load_config() -> ProjectConfig:
    """Load the Phase 1 defaults.

    File-based configuration is intentionally deferred until the schema is
    needed by the processing pipeline.
    """

    return ProjectConfig()

