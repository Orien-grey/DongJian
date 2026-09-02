"""Application services used by the local HTTP API.

The services deliberately sit above the registry and extraction coordinators.
HTTP handlers depend on these stable operations rather than on DuckDB or
Parquet implementation details.
"""

from .catalog import CatalogService
from .process import ProcessTaskManager, SourceValidationError, validate_source_directory
from .quality import QualityService
from .sql import (
    SqlAssetError,
    SqlExecutionError,
    SqlQueryService,
    SqlBenchmark,
    SqlSchemaResponse,
    SqlServiceError,
    SqlTimeoutError,
    SqlValidationError,
    verify_sql_sandbox,
    run_sql_benchmark,
)

__all__ = [
    "CatalogService",
    "ProcessTaskManager",
    "QualityService",
    "SqlAssetError",
    "SqlExecutionError",
    "SqlQueryService",
    "SqlBenchmark",
    "SqlSchemaResponse",
    "SqlServiceError",
    "SqlTimeoutError",
    "SqlValidationError",
    "SourceValidationError",
    "validate_source_directory",
    "verify_sql_sandbox",
    "run_sql_benchmark",
]
