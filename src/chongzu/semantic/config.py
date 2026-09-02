"""Provider-neutral, project-local semantic configuration.

Only ``<project>/.env`` is a runtime configuration source.  The legacy JSON
example remains loadable for compatibility tests and documentation, but it is
never discovered implicitly by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .. import paths


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip()


def _integer(value: object, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


@dataclass(frozen=True)
class SemanticConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = 60
    max_retries: int = 2
    config_version: str = paths.SEMANTIC_CONFIG_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticConfig":
        if not isinstance(value, Mapping):
            raise ValueError("LLM configuration must be a mapping")
        return cls(
            base_url=_string(value.get("base_url", ""), "base_url"),
            api_key=_string(value.get("api_key", ""), "api_key"),
            model=_string(value.get("model", ""), "model"),
            timeout_seconds=_integer(value.get("timeout_seconds", 60), "timeout_seconds", 60),
            max_retries=_integer(value.get("max_retries", 2), "max_retries", 2),
        )

    @classmethod
    def load(cls, path: Path) -> "SemanticConfig":
        """Compatibility loader for the inactive JSON example only."""

        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return cls.from_mapping(value)

    @classmethod
    def from_env_file(cls, path: Path) -> "SemanticConfig":
        values: dict[str, object] = {}
        if not path.is_file():
            return cls()
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"invalid .env line {line_number}")
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if key not in {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT_SECONDS", "LLM_MAX_RETRIES"}:
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
        return cls.from_mapping(
            {
                "base_url": values.get("LLM_BASE_URL", ""),
                "api_key": values.get("LLM_API_KEY", ""),
                "model": values.get("LLM_MODEL", ""),
                "timeout_seconds": values.get("LLM_TIMEOUT_SECONDS", 60),
                "max_retries": values.get("LLM_MAX_RETRIES", 2),
            }
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def status(self) -> str:
        return "CONFIGURED" if self.configured else "NOT_CONFIGURED"

    def validate_for_use(self) -> None:
        if not self.base_url:
            raise ValueError("base_url must be explicitly configured")
        if not self.api_key:
            raise ValueError("api_key must be explicitly configured")
        if not self.model:
            raise ValueError("model must be explicitly configured")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0 or self.max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an explicit http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")


# Preserve the provider-neutral Phase 2 import contract while the package is
# reorganized into small semantic-layer modules.
LLMConfig = SemanticConfig


def load_semantic_config(project_root: Path | None = None) -> SemanticConfig:
    root = (project_root or paths.PROJECT_ROOT).resolve()
    return SemanticConfig.from_env_file(root / ".env")
