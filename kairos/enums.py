"""Kairos enums — all status, policy, and strategy enums for the SDK."""

from enum import StrEnum


class WorkflowStatus(StrEnum):
    """Terminal status of a workflow run."""

    COMPLETE = "complete"
    FAILED = "failed"
    PARTIAL = "partial"


class StepStatus(StrEnum):
    """Lifecycle status of a workflow step."""

    PENDING = "pending"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    FAILED_FINAL = "failed_final"
    ROUTING = "routing"
    SKIPPED = "skipped"


class FailureAction(StrEnum):
    """Action to take when a step fails."""

    RETRY = "retry"
    REPLAN = "replan"
    SKIP = "skip"
    ABORT = "abort"
    CUSTOM = "custom"


class FailureType(StrEnum):
    """Category of failure that triggered the failure router."""

    EXECUTION = "execution"
    VALIDATION = "validation"


class ForeachPolicy(StrEnum):
    """How to handle partial failures in foreach fan-out."""

    REQUIRE_ALL = "require_all"
    ALLOW_PARTIAL = "allow_partial"


class AttemptStatus(StrEnum):
    """Outcome of a single step execution attempt."""

    SUCCESS = "success"
    FAILURE = "failure"


class ValidationLayer(StrEnum):
    """Which validation layer to apply."""

    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    BOTH = "both"


class Severity(StrEnum):
    """Severity level for validation issues."""

    ERROR = "error"
    WARNING = "warning"


class LogLevel(StrEnum):
    """Log event severity level."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LogVerbosity(StrEnum):
    """Verbosity setting for run logger output."""

    MINIMAL = "minimal"
    NORMAL = "normal"
    VERBOSE = "verbose"


class PlanStrategy(StrEnum):
    """How the task graph was constructed."""

    MANUAL = "manual"
    LLM_GENERATED = "llm_generated"
    HYBRID = "hybrid"
