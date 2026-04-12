"""Kairos — Security-hardened, model-agnostic SDK for contract-enforced AI workflows."""

__version__ = "0.1.0"

# Public enums
from kairos.enums import (
    AttemptStatus,
    FailureAction,
    ForeachPolicy,
    LogVerbosity,
    Severity,
    StepStatus,
    WorkflowStatus,
)

# All exceptions
from kairos.exceptions import (
    ConfigError,
    ExecutionError,
    KairosError,
    PlanError,
    PolicyError,
    SecurityError,
    StateError,
    ValidationError,
)

# Security utilities
from kairos.security import (
    DEFAULT_SENSITIVE_PATTERNS,
    redact_sensitive,
    sanitize_exception,
    sanitize_path,
    sanitize_retry_context,
)

# State management
from kairos.state import ScopedStateProxy, StateSnapshot, StateStore

# Step definitions
from kairos.step import SKIP, AttemptRecord, Step, StepConfig, StepContext, StepResult

__all__ = [
    # Enums (public)
    "AttemptStatus",
    "FailureAction",
    "ForeachPolicy",
    "LogVerbosity",
    "Severity",
    "StepStatus",
    "WorkflowStatus",
    # Exceptions
    "ConfigError",
    "ExecutionError",
    "KairosError",
    "PlanError",
    "PolicyError",
    "SecurityError",
    "StateError",
    "ValidationError",
    # Security utilities
    "DEFAULT_SENSITIVE_PATTERNS",
    "redact_sensitive",
    "sanitize_exception",
    "sanitize_path",
    "sanitize_retry_context",
    # State management
    "ScopedStateProxy",
    "StateSnapshot",
    "StateStore",
    # Step definitions
    "SKIP",
    "AttemptRecord",
    "Step",
    "StepConfig",
    "StepContext",
    "StepResult",
]
