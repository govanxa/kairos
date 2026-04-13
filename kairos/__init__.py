"""Kairos — Security-hardened, model-agnostic SDK for contract-enforced AI workflows."""

__version__ = "0.1.0"

# Public enums
from kairos.enums import (
    AttemptStatus,
    FailureAction,
    FailureType,
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

# Executor
from kairos.executor import ExecutorHooks, StepExecutor, WorkflowResult

# Failure router
from kairos.failure import (
    KAIROS_DEFAULTS,
    FailureEvent,
    FailurePolicy,
    FailureRouter,
    RecoveryDecision,
)

# Plan
from kairos.plan import TaskGraph

# Schema
from kairos.schema import ContractPair, FieldValidationError, Schema, ValidationResult

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

# Validators
from kairos.validators import CompositeValidator, LLMValidator, StructuralValidator, Validator

# Workflow — top-level orchestrator (import last to avoid circular issues)
from kairos.workflow import Workflow

__all__ = [
    # Enums (public)
    "AttemptStatus",
    "FailureAction",
    "FailureType",
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
    # Failure router
    "KAIROS_DEFAULTS",
    "FailureEvent",
    "FailurePolicy",
    "FailureRouter",
    "RecoveryDecision",
    # Executor
    "ExecutorHooks",
    "StepExecutor",
    "WorkflowResult",
    # Plan
    "TaskGraph",
    # Schema
    "ContractPair",
    "FieldValidationError",
    "Schema",
    "ValidationResult",
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
    # Validators
    "CompositeValidator",
    "LLMValidator",
    "StructuralValidator",
    "Validator",
    # Workflow
    "Workflow",
]
