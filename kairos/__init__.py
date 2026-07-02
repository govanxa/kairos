"""Kairos — Security-hardened, model-agnostic SDK for contract-enforced AI workflows."""

__version__ = "0.4.6"

# Public enums
# Model adapter base types — importable from kairos directly per CLAUDE.md (FIX 2)
from kairos.adapters.base import ModelAdapter, ModelResponse, TokenUsage
from kairos.enums import (
    AttemptStatus,
    FailureAction,
    FailureType,
    ForeachPolicy,
    LogLevel,
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

# Logger — run observability (import after workflow to avoid circular issues)
from kairos.logger import (
    CallbackSink,
    ConsoleSink,
    FileSink,
    JSONLinesSink,
    LogEvent,
    LogSink,
    RunLog,
    RunLogger,
    RunSummary,
)

# Plan
from kairos.plan import TaskGraph

# Plugins
from kairos.plugins.registry import (
    PluginManifest,
    StepPluginSpec,
    build_manifest,
    discover_plugins,
    load_plugin,
    step_plugin,
    validator_plugin,
)

# Schema
from kairos.schema import ContractPair, FieldValidationError, Schema, ValidationResult

# Security utilities
from kairos.security import (
    DEFAULT_SENSITIVE_PATTERNS,
    FLAG_IMPERATIVE,
    FLAG_ROLE_MARKER,
    FLAG_TEMPLATE_TOKEN,
    FLAG_TOOL_CALL,
    INJECTION_FLAGS,
    SanitizedText,
    redact_sensitive,
    sanitize_exception,
    sanitize_path,
    sanitize_retry_context,
    sanitize_untrusted_text,
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
    "LogLevel",
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
    "FLAG_IMPERATIVE",
    "FLAG_ROLE_MARKER",
    "FLAG_TEMPLATE_TOKEN",
    "FLAG_TOOL_CALL",
    "INJECTION_FLAGS",
    "SanitizedText",
    "redact_sensitive",
    "sanitize_exception",
    "sanitize_path",
    "sanitize_retry_context",
    "sanitize_untrusted_text",
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
    # Logger
    "CallbackSink",
    "ConsoleSink",
    "FileSink",
    "JSONLinesSink",
    "LogEvent",
    "LogSink",
    "RunLog",
    "RunLogger",
    "RunSummary",
    # Workflow
    "Workflow",
    # Model adapter base types (FIX 2)
    "ModelAdapter",
    "ModelResponse",
    "TokenUsage",
    # Plugins
    "PluginManifest",
    "StepPluginSpec",
    "build_manifest",
    "discover_plugins",
    "load_plugin",
    "step_plugin",
    "validator_plugin",
]
