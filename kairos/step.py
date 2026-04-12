"""Kairos step — pure data structures for workflow step definition.

Provides:
- StepConfig: configuration for retry, timeout, foreach, and parallelism.
- StepContext: runtime context passed to every step action.
- AttemptRecord: immutable log of a single execution attempt (JSON-safe).
- StepResult: aggregated outcome of a step including all attempts.
- Step: the developer-facing step definition (name + callable + config).
- SKIP / _SkipSentinel: sentinel returned by a step action to signal a skip.

Security contracts:
- Step names are restricted to [a-zA-Z0-9_-] to prevent path traversal and
  injection via workflow identifiers.
- AttemptRecord stores only pre-sanitized string fields, never raw Exception objects.
- from_dict on AttemptRecord and StepResult reconstructs structural data only;
  callables are never reconstructed from serialized data.
- Step intentionally has no from_dict — step actions must be provided directly
  by the developer and can never be deserialized from untrusted data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

# Avoid a circular import: state.py already imports from exceptions.py.
# We reference StateStore and ScopedStateProxy as strings in annotations
# so that the type checker resolves them without executing the import at
# module load time. The TYPE_CHECKING block makes the import available
# to mypy and IDEs without creating a runtime dependency cycle.
from typing import TYPE_CHECKING

from kairos.enums import AttemptStatus, ForeachPolicy, StepStatus
from kairos.exceptions import ConfigError

if TYPE_CHECKING:
    from kairos.state import ScopedStateProxy, StateStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_STEP_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]+$")

# The set of keyword argument names that map to StepConfig fields.
# Any kwarg not in this set (and not a structural Step field) is rejected.
_STEP_CONFIG_KWARGS: frozenset[str] = frozenset(
    {
        "retries",
        "timeout",
        "foreach",
        "foreach_policy",
        "parallel",
        "max_concurrency",
        "retry_delay",
        "retry_backoff",
        "retry_jitter",
        "validation_timeout",
    }
)


# ---------------------------------------------------------------------------
# StepConfig
# ---------------------------------------------------------------------------


@dataclass
class StepConfig:
    """Configuration for step execution behaviour.

    Validates all fields in __post_init__ and raises ConfigError for any
    invalid value, so invalid configs are caught at definition time.

    Attributes:
        retries: Maximum retry attempts. Must be >= 0.
        timeout: Seconds before a step attempt times out. Must be > 0 or None.
        foreach: State key whose value to fan out over. None means no fan-out.
        foreach_policy: How partial fan-out failures are handled.
        parallel: Whether this step can run in parallel with siblings.
        max_concurrency: Maximum parallel fan-out instances. Must be >= 1 or None.
        retry_delay: Base seconds to wait between retries. Must be >= 0.
        retry_backoff: Multiplier applied to retry_delay each attempt. Must be >= 0.
        retry_jitter: Whether to add random jitter to retry delays.
        validation_timeout: Seconds before validation times out. Must be > 0.
    """

    retries: int = 0
    timeout: float | None = None
    foreach: str | None = None
    foreach_policy: ForeachPolicy = ForeachPolicy.REQUIRE_ALL
    parallel: bool = False
    max_concurrency: int | None = None
    retry_delay: float = 0.0
    retry_backoff: float = 1.0
    retry_jitter: bool = True
    validation_timeout: float = 30.0

    def __post_init__(self) -> None:
        """Validate all fields, raising ConfigError for any invalid value."""
        if self.retries < 0:
            raise ConfigError(f"StepConfig.retries must be >= 0, got {self.retries!r}.")
        if self.timeout is not None and self.timeout <= 0:
            raise ConfigError(f"StepConfig.timeout must be > 0 or None, got {self.timeout!r}.")
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ConfigError(
                f"StepConfig.max_concurrency must be >= 1 or None, got {self.max_concurrency!r}."
            )
        if self.retry_delay < 0:
            raise ConfigError(f"StepConfig.retry_delay must be >= 0, got {self.retry_delay!r}.")
        if self.retry_backoff < 0:
            raise ConfigError(f"StepConfig.retry_backoff must be >= 0, got {self.retry_backoff!r}.")
        if self.validation_timeout <= 0:
            raise ConfigError(
                f"StepConfig.validation_timeout must be > 0, got {self.validation_timeout!r}."
            )


# ---------------------------------------------------------------------------
# StepContext
# ---------------------------------------------------------------------------


@dataclass
class StepContext:
    """Runtime context passed to every step action as its single argument.

    The executor constructs a fresh StepContext for each attempt. When a step
    declares read_keys or write_keys, the state field is a ScopedStateProxy
    rather than the raw StateStore.

    Attributes:
        state: The state store (or scoped proxy) available to this step.
        inputs: Resolved input values from the state store — outputs of
            dependency steps, keyed by step ID.
        item: The current item when executing inside a foreach fan-out.
            None for non-foreach steps.
        retry_context: Sanitized metadata from the previous failed attempt.
            None on the first attempt. Contains only structured metadata —
            never raw output, LLM responses, or exception messages.
        step_id: The ID of the current step.
        attempt: Current attempt number, 1-based.
    """

    state: StateStore | ScopedStateProxy
    inputs: dict[str, object]
    item: object | None = None
    retry_context: dict[str, object] | None = None
    step_id: str = ""
    attempt: int = 1


# ---------------------------------------------------------------------------
# AttemptRecord
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    """Immutable log of a single step execution attempt.

    Security: stores only pre-sanitized strings for error fields — never raw
    Exception objects, which may contain credentials, API keys, or full LLM
    prompts. Use sanitize_exception() before storing error information here.

    Attributes:
        attempt_number: 1-based attempt index.
        status: Whether the attempt succeeded or failed.
        output: The step's return value for this attempt, or None.
        error_type: Exception class name only (e.g. "TimeoutError"). Sanitized.
        error_message: Truncated, redacted error message. Sanitized.
        duration_ms: Wall-clock time for this attempt in milliseconds.
        timestamp: UTC datetime when this attempt started.
    """

    attempt_number: int
    status: AttemptStatus
    output: object | None
    error_type: str | None
    error_message: str | None
    duration_ms: float
    timestamp: datetime

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-serializable dict.

        Returns:
            A dict with all fields in JSON-safe form: status as its string
            value, timestamp as an ISO 8601 string.
        """
        return {
            "attempt_number": self.attempt_number,
            "status": self.status.value,
            "output": self.output,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AttemptRecord:
        """Reconstruct an AttemptRecord from a plain dict.

        Raises ConfigError for missing required keys or invalid values.
        Extra keys in *data* are ignored.

        Args:
            data: Dict as produced by to_dict() or equivalent.

        Returns:
            A reconstructed AttemptRecord.

        Raises:
            ConfigError: When required keys are absent or values are invalid.
        """
        try:
            attempt_number = data["attempt_number"]
            raw_status = data["status"]
            output = data.get("output")
            raw_error_type = data.get("error_type")
            raw_error_message = data.get("error_message")
            duration_ms = data["duration_ms"]
            raw_timestamp = data["timestamp"]
        except KeyError as exc:
            raise ConfigError(f"AttemptRecord.from_dict: missing required key {exc}.") from exc

        try:
            status = AttemptStatus(str(raw_status))
        except ValueError as exc:
            raise ConfigError(
                f"AttemptRecord.from_dict: invalid status value {raw_status!r}."
            ) from exc

        try:
            timestamp = datetime.fromisoformat(str(raw_timestamp))
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                f"AttemptRecord.from_dict: invalid timestamp {raw_timestamp!r}."
            ) from exc

        error_type: str | None = str(raw_error_type) if raw_error_type is not None else None
        error_message: str | None = (
            str(raw_error_message) if raw_error_message is not None else None
        )

        return cls(
            attempt_number=int(str(attempt_number)),
            status=status,
            output=output,
            error_type=error_type,
            error_message=error_message,
            duration_ms=float(str(duration_ms)),
            timestamp=timestamp,
        )


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Aggregated outcome of a step execution, including all retry attempts.

    Attributes:
        step_id: The step identifier.
        status: Terminal status of this step (COMPLETED, FAILED_FINAL, SKIPPED).
        output: The step's final return value, or None.
        attempts: Ordered list of individual attempt records.
        duration_ms: Total wall-clock time across all attempts in milliseconds.
        timestamp: UTC datetime when the first attempt started.
    """

    step_id: str
    status: StepStatus
    output: object | None
    attempts: list[AttemptRecord]
    duration_ms: float
    timestamp: datetime

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-serializable dict.

        Returns:
            A dict with all fields in JSON-safe form. Nested AttemptRecords
            are serialized via their own to_dict() method.
        """
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "output": self.output,
            "attempts": [a.to_dict() for a in self.attempts],
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> StepResult:
        """Reconstruct a StepResult from a plain dict.

        Delegates to AttemptRecord.from_dict() for each attempt entry.
        Does NOT reconstruct step actions — callables are never deserialized.

        Args:
            data: Dict as produced by to_dict() or equivalent.

        Returns:
            A reconstructed StepResult.

        Raises:
            ConfigError: When required keys are absent or values are invalid.
        """
        try:
            step_id = data["step_id"]
            raw_status = data["status"]
            output = data.get("output")
            raw_attempts = data["attempts"]
            duration_ms = data["duration_ms"]
            raw_timestamp = data["timestamp"]
        except KeyError as exc:
            raise ConfigError(f"StepResult.from_dict: missing required key {exc}.") from exc

        try:
            status = StepStatus(str(raw_status))
        except ValueError as exc:
            raise ConfigError(
                f"StepResult.from_dict: invalid status value {raw_status!r}."
            ) from exc

        try:
            timestamp = datetime.fromisoformat(str(raw_timestamp))
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                f"StepResult.from_dict: invalid timestamp {raw_timestamp!r}."
            ) from exc

        if not isinstance(raw_attempts, list):
            raise ConfigError("StepResult.from_dict: 'attempts' must be a list.")

        attempts: list[AttemptRecord] = []
        for entry in raw_attempts:
            if not isinstance(entry, dict):
                raise ConfigError("StepResult.from_dict: each attempt entry must be a dict.")
            attempts.append(AttemptRecord.from_dict(entry))

        return cls(
            step_id=str(step_id),
            status=status,
            output=output,
            attempts=attempts,
            duration_ms=float(str(duration_ms)),
            timestamp=timestamp,
        )


# ---------------------------------------------------------------------------
# _SkipSentinel + SKIP
# ---------------------------------------------------------------------------


class _SkipSentinel:
    """Singleton sentinel returned by a step action to signal a voluntary skip.

    The executor detects ``result is SKIP`` and transitions the step to
    StepStatus.SKIPPED. Validation is not run on skipped steps.

    This is the ONLY way to signal a skip — returning a dict with
    ``{"skipped": True}`` is treated as normal output.
    """

    _instance: _SkipSentinel | None = None

    def __new__(cls) -> _SkipSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "SKIP"

    def __bool__(self) -> bool:
        return False


SKIP: _SkipSentinel = _SkipSentinel()


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class Step:
    """A single unit of work in a Kairos workflow.

    Step is the developer-facing definition object. It holds the step name,
    the callable that executes the work, dependency declarations, scope
    restrictions, and execution configuration. It contains NO execution logic.

    Step intentionally has no ``from_dict`` classmethod. Step actions are
    callables and must be provided directly by the developer — they can never
    be deserialized from untrusted data.

    Args:
        name: Unique step identifier. Must match ``[a-zA-Z0-9_-]+``.
        action: Callable that accepts a StepContext and returns a value.
        depends_on: Names of steps that must complete before this step runs.
            Defaults to an empty list. None is treated as an empty list.
        config: Pre-built StepConfig. Mutually exclusive with config kwargs.
        input_contract: Schema that incoming state inputs must satisfy.
        output_contract: Schema that this step's output must satisfy.
        read_keys: State keys this step is allowed to read. None = unrestricted.
        write_keys: State keys this step is allowed to write. None = unrestricted.
        failure_policy: Step-level failure policy overriding workflow defaults.
        **kwargs: Config field overrides (retries, timeout, foreach, …).
            Mutually exclusive with the config argument.

    Raises:
        ConfigError: When name is empty or contains invalid characters, action
            is not callable, config and kwargs are both provided, or an unknown
            kwarg is supplied.
    """

    def __init__(
        self,
        name: str,
        action: Callable[..., object],
        *,
        depends_on: list[str] | None = None,
        config: StepConfig | None = None,
        input_contract: object = None,
        output_contract: object = None,
        read_keys: list[str] | None = None,
        write_keys: list[str] | None = None,
        failure_policy: object = None,
        **kwargs: object,
    ) -> None:
        # --- Validate name ---
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Step name must be a non-empty string, got {name!r}.")
        if not _VALID_STEP_NAME_RE.match(name):
            raise ConfigError(
                f"Step name {name!r} contains invalid characters. Only [a-zA-Z0-9_-] are allowed."
            )

        # --- Validate action ---
        if not callable(action):
            raise ConfigError(f"Step action must be callable, got {type(action).__name__!r}.")

        # --- Validate config / kwargs exclusivity ---
        if config is not None and kwargs:
            raise ConfigError(
                "Step config cannot be provided together with config keyword "
                "arguments. Use one or the other."
            )

        # --- Validate unknown kwargs ---
        unknown = set(kwargs.keys()) - _STEP_CONFIG_KWARGS
        if unknown:
            raise ConfigError(
                f"Step received unknown keyword argument(s): {', '.join(sorted(unknown))}."
            )

        # --- Build config ---
        if config is not None:
            resolved_config = config
        elif kwargs:
            resolved_config = StepConfig(**kwargs)  # type: ignore[arg-type]
        else:
            resolved_config = StepConfig()

        # --- Assign attributes ---
        self.name: str = name
        self.action: Callable[..., object] = action
        self.depends_on: list[str] = list(depends_on) if depends_on is not None else []
        self.config: StepConfig = resolved_config
        self.input_contract: object = input_contract
        self.output_contract: object = output_contract
        self.read_keys: list[str] | None = read_keys
        self.write_keys: list[str] | None = write_keys
        self.failure_policy: object = failure_policy

    def __repr__(self) -> str:
        return f"Step(name={self.name!r}, depends_on={self.depends_on!r})"
