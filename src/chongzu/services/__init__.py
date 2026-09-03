"""Application services used by the local HTTP API.

The services deliberately sit above the registry and extraction coordinators.
HTTP handlers depend on these stable operations rather than on DuckDB or
Parquet implementation details.
"""

from .catalog import CatalogService
from .analysis import (
    ANALYSIS_ACTION_CONTRACT_VERSION,
    ANALYSIS_PROMPT_VERSION,
    MAX_ANALYSIS_HISTORY_LIMIT,
    MAX_ANALYSIS_STEPS,
    AnalysisExecutionError,
    AnalysisOrchestrator,
    AnalysisRunStore,
    AnalysisService,
    AnalysisServiceError,
    normalize_analysis_request,
    validate_analysis_action,
)
from .process import ProcessTaskManager, SourceValidationError, TaskAdmissionError, validate_source_directory
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
    "AnalysisService",
    "AnalysisServiceError",
    "AnalysisExecutionError",
    "AnalysisOrchestrator",
    "AnalysisRunStore",
    "ANALYSIS_ACTION_CONTRACT_VERSION",
    "ANALYSIS_PROMPT_VERSION",
    "MAX_ANALYSIS_HISTORY_LIMIT",
    "MAX_ANALYSIS_STEPS",
    "normalize_analysis_request",
    "validate_analysis_action",
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
    "TaskAdmissionError",
    "validate_source_directory",
    "verify_sql_sandbox",
    "run_sql_benchmark",
]
