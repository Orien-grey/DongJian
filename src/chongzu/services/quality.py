"""Quality-review application service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chongzu import paths
from chongzu.locking import registry_write_mutex
from chongzu.registry import Registry


ALLOWED_STATUSES = frozenset({"open", "accepted", "ignored", "resolved"})


class QualityService:
    def __init__(self, *, registry_path: Path | str | None = None) -> None:
        self.registry_path = Path(registry_path or paths.REGISTRY_PATH).resolve()

    def list_issues(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        asset_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if status is not None and status not in ALLOWED_STATUSES:
            raise ValueError("invalid quality issue status")
        if severity is not None and severity not in {"info", "warning", "error", "critical"}:
            raise ValueError("invalid quality issue severity")
        registry = Registry.open_reader(self.registry_path)
        try:
            total = registry.count_quality_issues(status=status, severity=severity, asset_id=asset_id)
            items = registry.list_quality_issues(
                status=status,
                severity=severity,
                asset_id=asset_id,
                limit=limit,
                offset=offset,
            )
            return {
                "items": items,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "hasNext": offset + len(items) < total,
                },
            }
        finally:
            registry.close()

    def update_status(self, issue_id: str, status: str) -> dict[str, Any] | None:
        if status not in ALLOWED_STATUSES:
            raise ValueError("quality issue status must be open, accepted, ignored, or resolved")
        with registry_write_mutex(self.registry_path):
            registry = Registry.open(self.registry_path, initialize=False)
            try:
                return registry.update_quality_issue_status(issue_id, status)
            finally:
                registry.close()
