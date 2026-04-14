"""Kairos logger — structured run logging with multiple sinks and security-hardened redaction.

Provides:
- LogEvent: A single structured event from the workflow lifecycle.
- RunSummary: Aggregated metrics for a completed workflow run.
- RunLog: Complete record of a single workflow execution.
- LogSink: Protocol for sink implementations.
- ConsoleSink: Pretty-printed events to a stream (default: stderr).
- JSONLinesSink: One JSON object per line to a .jsonl file.
- FileSink: Buffers events, writes complete RunLog as JSON on close().
- CallbackSink: Forwards events to a user-provided callable.
- RunLogger: ExecutorHooks subclass that wires all sinks and applies redaction.

Security contracts:
- S4: redact_sensitive() is called in _create_event() before any storage or dispatch.
- S9: sanitize_path() is used for all dynamic path components in file sinks.
- Exception sanitization: sanitize_exception() is used in on_step_fail().
- CallbackSink emits trust boundary warnings at construction time.
- All sink emit() calls are wrapped in try/except — sink errors never propagate.
"""

from __future__ import annotations

import contextlib
import json
import sys
import uuid
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, Protocol, cast, runtime_checkable

from kairos.enums import LogLevel, LogVerbosity, StepStatus, WorkflowStatus
from kairos.exceptions import ConfigError, SecurityError
from kairos.executor import ExecutorHooks, WorkflowResult
from kairos.plan import TaskGraph
from kairos.schema import ValidationResult
from kairos.security import (
    DEFAULT_SENSITIVE_PATTERNS,
    redact_sensitive,
    sanitize_exception,
    sanitize_path,
)
from kairos.step import Step, StepResult

# ---------------------------------------------------------------------------
# Internal path helpers
# ---------------------------------------------------------------------------


def _check_raw_name_for_traversal(name: str) -> None:
    """Raise SecurityError if *name* contains raw path traversal patterns.

    Checks the raw (unsanitized) input for ``..`` components and absolute-path
    indicators before ``sanitize_path`` replaces the offending characters.

    Args:
        name: The raw workflow name, run ID, or similar string.

    Raises:
        SecurityError: If *name* contains ``..`` or starts with a path separator.
    """
    if not name:
        raise SecurityError("Path name must not be empty.")
    # Reject raw '..' anywhere in the component
    if ".." in name:
        raise SecurityError(
            f"Path traversal attempt detected in name {name!r}. Names must not contain '..'."
        )
    # Reject absolute paths (Unix / or Windows C:\ or \\)
    if name.startswith("/") or name.startswith("\\") or (len(name) >= 2 and name[1] == ":"):
        raise SecurityError(
            f"Absolute path detected in name {name!r}. Names must be relative identifiers."
        )


# ---------------------------------------------------------------------------
# Verbosity rank — used to decide whether an event type passes the filter
# ---------------------------------------------------------------------------

_VERBOSITY_RANK: dict[LogVerbosity, int] = {
    LogVerbosity.MINIMAL: 0,
    LogVerbosity.NORMAL: 1,
    LogVerbosity.VERBOSE: 2,
}

# Minimum verbosity required for each event type
_EVENT_MIN_VERBOSITY: dict[str, LogVerbosity] = {
    "workflow_start": LogVerbosity.MINIMAL,
    "workflow_complete": LogVerbosity.MINIMAL,
    "step_start": LogVerbosity.NORMAL,
    "step_complete": LogVerbosity.NORMAL,
    "step_fail": LogVerbosity.MINIMAL,
    "step_retry": LogVerbosity.NORMAL,
    "step_skip": LogVerbosity.NORMAL,
    "validation_start": LogVerbosity.VERBOSE,
    "validation_complete": LogVerbosity.NORMAL,
    "validation_fail": LogVerbosity.MINIMAL,
}


def _event_passes_verbosity(event_type: str, verbosity: LogVerbosity) -> bool:
    """Return True if *event_type* should be emitted at *verbosity* level.

    Args:
        event_type: The event type string (e.g. ``"step_start"``).
        verbosity: The active verbosity setting.

    Returns:
        True when the verbosity rank is >= the event's minimum rank.
    """
    min_verbosity = _EVENT_MIN_VERBOSITY.get(event_type, LogVerbosity.NORMAL)
    return _VERBOSITY_RANK[verbosity] >= _VERBOSITY_RANK[min_verbosity]


# ---------------------------------------------------------------------------
# LogEvent
# ---------------------------------------------------------------------------


@dataclass
class LogEvent:
    """A single structured event captured during a workflow run.

    Attributes:
        timestamp: UTC datetime when the event occurred.
        event_type: Identifier for the event (e.g. ``"step_start"``).
        step_id: The step this event belongs to, or None for workflow-level events.
        data: Event-specific payload dict. All values are JSON-serializable.
            Sensitive keys are redacted before this event is stored.
        level: Log severity level (INFO, WARN, ERROR).
    """

    timestamp: datetime
    event_type: str
    step_id: str | None
    data: dict[str, object]
    level: LogLevel

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-serializable dict.

        Returns:
            A dict with all fields in JSON-safe form: enums as strings,
            datetimes as ISO 8601 strings.
        """
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "step_id": self.step_id,
            "data": self.data,
            "level": str(self.level),
        }


# ---------------------------------------------------------------------------
# RunSummary
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    """Aggregated performance metrics for a completed workflow run.

    Attributes:
        total_steps: Total number of steps in the plan.
        completed_steps: Steps that finished with COMPLETED status.
        failed_steps: Steps that finished with FAILED_FINAL status.
        skipped_steps: Steps that were skipped.
        total_retries: Total retry attempts across all steps.
        total_duration_ms: Wall-clock time for the entire run in milliseconds.
        validations_passed: Total passing contract validation checks.
        validations_failed: Total failing contract validation checks.
    """

    total_steps: int
    completed_steps: int
    failed_steps: int
    skipped_steps: int
    total_retries: int
    total_duration_ms: float
    validations_passed: int
    validations_failed: int

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-serializable dict.

        Returns:
            A dict with all numeric fields.
        """
        return {
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "skipped_steps": self.skipped_steps,
            "total_retries": self.total_retries,
            "total_duration_ms": self.total_duration_ms,
            "validations_passed": self.validations_passed,
            "validations_failed": self.validations_failed,
        }


# ---------------------------------------------------------------------------
# RunLog
# ---------------------------------------------------------------------------


@dataclass
class RunLog:
    """Complete structured record of a single workflow execution.

    Attributes:
        run_id: UUID4 unique identifier for this run.
        workflow_name: Name of the workflow that was executed.
        started_at: UTC datetime when the run began.
        completed_at: UTC datetime when the run ended, or None if still running.
        status: Terminal status of the workflow.
        events: Ordered list of all LogEvents emitted during the run.
        summary: Aggregated metrics computed at run completion.
        initial_plan: The TaskGraph that was executed, or None if not captured.
    """

    run_id: str
    workflow_name: str
    started_at: datetime
    completed_at: datetime | None
    status: WorkflowStatus
    events: list[LogEvent]
    summary: RunSummary
    initial_plan: TaskGraph | None = field(default=None)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-serializable dict.

        Returns:
            A dict with all fields in JSON-safe form: enums as strings,
            datetimes as ISO 8601 strings, events serialized via to_dict(),
            initial_plan serialized via TaskGraph.to_dict() when present.
        """
        return {
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": str(self.status),
            "events": [e.to_dict() for e in self.events],
            "summary": self.summary.to_dict(),
            "initial_plan": self.initial_plan.to_dict() if self.initial_plan is not None else None,
        }


# ---------------------------------------------------------------------------
# LogSink Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LogSink(Protocol):
    """Protocol for log output destinations.

    All sinks must implement these three methods. The RunLogger dispatches
    each event to every registered sink via emit(). flush() is called at
    workflow completion. close() is called for cleanup (e.g. writing final files).
    """

    def emit(self, event: LogEvent) -> None:
        """Write a single log event.

        Args:
            event: The LogEvent to write.
        """
        ...

    def flush(self) -> None:
        """Flush any buffered output."""
        ...

    def close(self) -> None:
        """Release resources and finalize output."""
        ...


# ---------------------------------------------------------------------------
# ConsoleSink
# ---------------------------------------------------------------------------


class ConsoleSink:
    """Writes pretty-printed log events to a stream (default: stderr).

    Each event is written as a single line:
    ``[timestamp] LEVEL event_type: key=value, key=value``

    Args:
        stream: Output stream. Defaults to ``sys.stderr``.
        verbosity: Per-sink verbosity override. When set, events below this
            level are not emitted regardless of the logger's verbosity.
    """

    def __init__(
        self,
        stream: IO[str] | None = None,
        verbosity: LogVerbosity | None = None,
    ) -> None:
        self._stream: IO[str] = stream if stream is not None else sys.stderr
        self._verbosity = verbosity

    def emit(self, event: LogEvent) -> None:
        """Write the event as a formatted line to the stream.

        Args:
            event: The LogEvent to format and write.
        """
        # Apply per-sink verbosity filter if configured
        if self._verbosity is not None and not _event_passes_verbosity(
            event.event_type, self._verbosity
        ):
            return

        ts = event.timestamp.strftime("%H:%M:%S")
        level = str(event.level).upper()
        data_parts = ", ".join(f"{k}={v}" for k, v in event.data.items())
        line = f"[{ts}] {level} {event.event_type}"
        if data_parts:
            line = f"{line}: {data_parts}"
        self._stream.write(line + "\n")

    def flush(self) -> None:
        """Flush the underlying stream."""
        with contextlib.suppress(Exception):
            self._stream.flush()

    def close(self) -> None:
        """No-op for console sink — stream lifecycle is managed externally."""


# ---------------------------------------------------------------------------
# JSONLinesSink
# ---------------------------------------------------------------------------


class JSONLinesSink:
    """Appends one JSON object per log event to a ``.jsonl`` file.

    The file is named ``{sanitized_workflow_name}_{sanitized_run_id}.jsonl``
    and is created within *base_dir* when ``set_run_context()`` is called.

    Args:
        base_dir: Directory in which to create the ``.jsonl`` file.
    """

    def __init__(self, base_dir: str = ".") -> None:
        self._base_dir = base_dir
        self._file_path: str | None = None
        self._file_handle: IO[str] | None = None

    def set_run_context(self, workflow_name: str, run_id: str) -> None:
        """Open the output file for this run.

        Args:
            workflow_name: Workflow name — checked for traversal, then sanitized.
            run_id: Run identifier — checked for traversal, then sanitized.

        Raises:
            SecurityError: If name/id contains traversal patterns, is empty after
                sanitization, or escapes base_dir.
        """

        _check_raw_name_for_traversal(workflow_name)
        _check_raw_name_for_traversal(run_id)
        safe_name_only = sanitize_path(workflow_name)
        safe_id_only = sanitize_path(run_id)
        # Build filename with extension kept separate (not passed through sanitize_path)
        basename = f"{safe_name_only}_{safe_id_only}"
        # Verify the basename stays within base_dir before appending extension
        full_path = sanitize_path(basename, self._base_dir)
        self._file_path = full_path + ".jsonl"
        # Open for appending
        self._file_handle = open(self._file_path, "a", encoding="utf-8")  # noqa: SIM115

    def emit(self, event: LogEvent) -> None:
        """Append one JSON line for the event.

        Args:
            event: The LogEvent to serialize and append.
        """
        if self._file_handle is None:
            return
        line = json.dumps(event.to_dict())
        self._file_handle.write(line + "\n")
        self._file_handle.flush()

    def flush(self) -> None:
        """Flush the file handle."""
        if self._file_handle is not None:
            with contextlib.suppress(Exception):
                self._file_handle.flush()

    def close(self) -> None:
        """Close the file handle."""
        if self._file_handle is not None:
            with contextlib.suppress(Exception):
                self._file_handle.close()
            self._file_handle = None


# ---------------------------------------------------------------------------
# FileSink
# ---------------------------------------------------------------------------


class FileSink:
    """Buffers log events and writes a complete RunLog JSON file on close().

    The file is named ``{sanitized_workflow_name}_{sanitized_run_id}.json``
    and is written to *base_dir* when ``close()`` is called.

    Args:
        base_dir: Directory in which to create the JSON file.
    """

    def __init__(self, base_dir: str = ".") -> None:
        self._base_dir = base_dir
        self._file_path: str | None = None
        self._run_log: RunLog | None = None
        self._buffered_events: list[LogEvent] = []

    def set_run_context(self, workflow_name: str, run_id: str) -> None:
        """Configure the output file path for this run.

        Args:
            workflow_name: Workflow name — checked for traversal, then sanitized.
            run_id: Run identifier — checked for traversal, then sanitized.

        Raises:
            SecurityError: If name/id contains traversal patterns, is empty after
                sanitization, or escapes base_dir.
        """
        _check_raw_name_for_traversal(workflow_name)
        _check_raw_name_for_traversal(run_id)
        safe_name_only = sanitize_path(workflow_name)
        safe_id_only = sanitize_path(run_id)
        # Build filename with extension kept separate (not passed through sanitize_path)
        basename = f"{safe_name_only}_{safe_id_only}"
        # Verify the basename stays within base_dir before appending extension
        full_path = sanitize_path(basename, self._base_dir)
        self._file_path = full_path + ".json"

    def set_run_log(self, run_log: RunLog) -> None:
        """Provide the final RunLog for serialization on close().

        Args:
            run_log: The completed RunLog to write.
        """
        self._run_log = run_log

    def emit(self, event: LogEvent) -> None:
        """Buffer the event for writing on close().

        Args:
            event: The LogEvent to buffer.
        """
        self._buffered_events.append(event)

    def flush(self) -> None:
        """No-op — buffered events are written only on close()."""

    def close(self) -> None:
        """Write the complete RunLog JSON file.

        If set_run_log() was not called, writes only the buffered events.
        """
        if self._file_path is None:
            return

        if self._run_log is not None:
            data = self._run_log.to_dict()
        else:
            data = {"events": [e.to_dict() for e in self._buffered_events]}

        with contextlib.suppress(Exception), open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# CallbackSink
# ---------------------------------------------------------------------------


class CallbackSink:
    """Forwards log events to a user-provided callable.

    The callback receives each LogEvent as it is emitted. Because the callback
    runs with the same permissions as the Kairos process, this sink emits
    a trust boundary warning at construction time.

    Args:
        callback: Any callable that accepts a LogEvent. Non-callables raise
            ConfigError immediately.

    Raises:
        ConfigError: If *callback* is not callable.
    """

    def __init__(self, callback: Callable[[LogEvent], None]) -> None:
        if not callable(callback):
            raise ConfigError(
                f"CallbackSink requires a callable, got {type(callback).__name__!r}. "
                "Pass a function or any other callable."
            )
        self._callback: Callable[[LogEvent], None] = callback

    def emit(self, event: LogEvent) -> None:
        """Forward the event to the registered callback.

        Args:
            event: The LogEvent to forward.
        """
        self._callback(event)

    def flush(self) -> None:
        """No-op — callbacks manage their own buffering."""

    def close(self) -> None:
        """No-op — callbacks manage their own lifecycle."""


# ---------------------------------------------------------------------------
# RunLogger — ExecutorHooks implementation
# ---------------------------------------------------------------------------


class RunLogger(ExecutorHooks):
    """Structured workflow run logger that integrates with the executor lifecycle.

    Subscribes to all ExecutorHooks events, applies sensitive-key redaction,
    filters by verbosity, and dispatches to registered sinks.

    Security contracts:
    - redact_sensitive() is called in _create_event() before any storage or dispatch.
    - sanitize_exception() is used in on_step_fail() for the error payload.
    - Sink emit() calls are wrapped in try/except — sink errors never propagate.
    - CallbackSink triggers a trust boundary warning at RunLogger construction.

    Args:
        sinks: List of LogSink implementations to receive events.
        verbosity: Global verbosity level. Events below this level are dropped
            before reaching any sink. Default: NORMAL.
        sensitive_patterns: Additional glob patterns for sensitive key redaction,
            merged with DEFAULT_SENSITIVE_PATTERNS.
    """

    def __init__(
        self,
        sinks: list[LogSink],
        verbosity: LogVerbosity = LogVerbosity.NORMAL,
        sensitive_patterns: list[str] | None = None,
    ) -> None:
        self._sinks: list[LogSink] = list(sinks)
        self._verbosity = verbosity
        self._sensitive_patterns: list[str] = DEFAULT_SENSITIVE_PATTERNS + (
            sensitive_patterns or []
        )
        self._run_log: RunLog | None = None

        # Emit trust boundary warnings for any CallbackSink
        for sink in self._sinks:
            if isinstance(sink, CallbackSink):
                warnings.warn(
                    f"CallbackSink registered. Callback will receive log data at "
                    f"'{verbosity}' verbosity. Ensure the callback function is trusted.",
                    stacklevel=2,
                )
                if verbosity == LogVerbosity.VERBOSE:
                    warnings.warn(
                        "CallbackSink at 'verbose' level will receive full step inputs/outputs. "
                        "Sensitive data will be redacted but non-sensitive PII may be present.",
                        stacklevel=2,
                    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_run_log(self) -> RunLog | None:
        """Return the current RunLog, or None if no workflow has started.

        Returns:
            The active RunLog, or None before on_workflow_start() is called.
        """
        return self._run_log

    # ------------------------------------------------------------------
    # ExecutorHooks overrides
    # ------------------------------------------------------------------

    def on_workflow_start(self, graph: TaskGraph) -> None:
        """Create a new RunLog and emit a workflow_start event.

        Args:
            graph: The TaskGraph about to be executed.
        """
        run_id = str(uuid.uuid4())
        summary = RunSummary(
            total_steps=len(graph.steps),
            completed_steps=0,
            failed_steps=0,
            skipped_steps=0,
            total_retries=0,
            total_duration_ms=0.0,
            validations_passed=0,
            validations_failed=0,
        )
        self._run_log = RunLog(
            run_id=run_id,
            workflow_name=graph.name,
            started_at=datetime.now(tz=UTC),
            completed_at=None,
            status=WorkflowStatus.COMPLETE,  # placeholder; updated in on_workflow_complete
            events=[],
            summary=summary,
            initial_plan=graph,
        )

        # Notify file sinks of run context — SecurityError must propagate
        for sink in self._sinks:
            if hasattr(sink, "set_run_context"):
                cast(Any, sink).set_run_context(graph.name, run_id)

        event = self._create_event(
            event_type="workflow_start",
            step_id=None,
            data={
                "workflow_name": graph.name,
                "run_id": run_id,
                "total_steps": len(graph.steps),
            },
            level=LogLevel.INFO,
        )
        self._record_and_dispatch(event)

    def on_workflow_complete(self, result: WorkflowResult) -> None:
        """Finalize the RunLog and emit a workflow_complete event.

        Args:
            result: The final WorkflowResult.
        """
        if self._run_log is None:
            return

        summary = self._build_summary(result)
        self._run_log.status = result.status
        self._run_log.completed_at = datetime.now(tz=UTC)
        self._run_log.summary = summary

        event = self._create_event(
            event_type="workflow_complete",
            step_id=None,
            data={
                "status": str(result.status),
                "duration_ms": result.duration_ms,
                "summary": summary.to_dict(),
            },
            level=LogLevel.INFO,
        )
        self._record_and_dispatch(event)

        # Provide finalized RunLog to FileSink instances
        for sink in self._sinks:
            if hasattr(sink, "set_run_log"):
                with contextlib.suppress(Exception):
                    cast(Any, sink).set_run_log(self._run_log)

        self._flush_all_sinks()
        self._close_all_sinks()

    def on_step_start(self, step: Step, attempt: int) -> None:
        """Record a step_start event.

        Args:
            step: The step about to execute.
            attempt: 1-based attempt number.
        """
        event = self._create_event(
            event_type="step_start",
            step_id=step.name,
            data={"step_id": step.name, "attempt": attempt},
            level=LogLevel.INFO,
        )
        self._record_and_dispatch(event)

    def on_step_complete(self, step: Step, result: StepResult) -> None:
        """Record a step_complete event.

        At VERBOSE verbosity, the redacted step output is included in the event data.

        Args:
            step: The completed step.
            result: The step's StepResult.
        """
        data: dict[str, object] = {
            "step_id": step.name,
            "status": str(result.status),
            "duration_ms": result.duration_ms,
        }
        # Include output only at VERBOSE level
        if _VERBOSITY_RANK[self._verbosity] >= _VERBOSITY_RANK[LogVerbosity.VERBOSE]:
            raw_output: object = result.output
            if raw_output is not None and isinstance(raw_output, dict):
                data["output"] = redact_sensitive(
                    cast(dict[str, object], raw_output), self._sensitive_patterns
                )
            elif raw_output is not None:
                # Non-dict output: sanitize via exception wrapper to strip credentials,
                # then truncate. This prevents credential strings (sk-*, Bearer *, etc.)
                # from flowing unredacted to sinks when a step returns a raw string.
                _, sanitized = sanitize_exception(Exception(str(raw_output)))
                data["output"] = sanitized

        event = self._create_event(
            event_type="step_complete",
            step_id=step.name,
            data=data,
            level=LogLevel.INFO,
        )
        self._record_and_dispatch(event)

    def on_step_fail(self, step: Step, error: Exception, attempt: int) -> None:
        """Record a step_fail event with sanitized error information.

        Args:
            step: The step that failed.
            error: The exception that was raised.
            attempt: The 1-based attempt number that failed.
        """
        error_type, error_message = sanitize_exception(error)
        event = self._create_event(
            event_type="step_fail",
            step_id=step.name,
            data={
                "step_id": step.name,
                "error_type": error_type,
                "error_message": error_message,
                "attempt": attempt,
            },
            level=LogLevel.ERROR,
        )
        self._record_and_dispatch(event)

    def on_step_retry(self, step: Step, attempt: int) -> None:
        """Record a step_retry event.

        Args:
            step: The step about to be retried.
            attempt: The 1-based attempt number of the upcoming retry.
        """
        event = self._create_event(
            event_type="step_retry",
            step_id=step.name,
            data={"step_id": step.name, "attempt": attempt},
            level=LogLevel.WARN,
        )
        self._record_and_dispatch(event)

    def on_step_skip(self, step: Step, reason: str) -> None:
        """Record a step_skip event.

        The reason string is sanitized via sanitize_exception() to prevent
        credentials that may have been inadvertently embedded in skip reasons
        from flowing to sinks.

        Args:
            step: The skipped step.
            reason: Human-readable explanation for the skip.
        """
        _, safe_reason = sanitize_exception(Exception(reason))
        event = self._create_event(
            event_type="step_skip",
            step_id=step.name,
            data={"step_id": step.name, "reason": safe_reason},
            level=LogLevel.WARN,
        )
        self._record_and_dispatch(event)

    def on_validation_start(self, step: Step, phase: str, attempt: int) -> None:
        """Record a validation_start event (VERBOSE only).

        Args:
            step: The step whose contract is being validated.
            phase: ``"input"`` or ``"output"``.
            attempt: 1-based attempt number.
        """
        event = self._create_event(
            event_type="validation_start",
            step_id=step.name,
            data={"step_id": step.name, "phase": phase, "attempt": attempt},
            level=LogLevel.INFO,
        )
        self._record_and_dispatch(event)

    def on_validation_complete(self, step: Step, phase: str, result: ValidationResult) -> None:
        """Record a validation_complete event.

        Args:
            step: The step whose contract passed.
            phase: ``"input"`` or ``"output"``.
            result: The passing ValidationResult.
        """
        event = self._create_event(
            event_type="validation_complete",
            step_id=step.name,
            data={"step_id": step.name, "phase": phase},
            level=LogLevel.INFO,
        )
        self._record_and_dispatch(event)

    def on_validation_fail(
        self, step: Step, phase: str, result: ValidationResult, attempt: int
    ) -> None:
        """Record a validation_fail event.

        Args:
            step: The step whose contract failed.
            phase: ``"input"`` or ``"output"``.
            result: The failing ValidationResult.
            attempt: 1-based attempt number.
        """
        errors: list[object] = [
            {"field": str(e.field), "message": str(e.message)} for e in result.errors
        ]
        event = self._create_event(
            event_type="validation_fail",
            step_id=step.name,
            data={
                "step_id": step.name,
                "phase": phase,
                "errors": errors,
                "attempt": attempt,
            },
            level=LogLevel.ERROR,
        )
        self._record_and_dispatch(event)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _create_event(
        self,
        event_type: str,
        step_id: str | None,
        data: dict[str, object],
        level: LogLevel,
    ) -> LogEvent:
        """Create a LogEvent with redaction applied to data.

        This is the security boundary: redact_sensitive() is called here,
        before any storage or dispatch to sinks.

        Args:
            event_type: The event type string.
            step_id: The step identifier, or None.
            data: Raw event payload. Sensitive keys are redacted in the copy.
            level: Log severity level.

        Returns:
            A LogEvent with redacted data.
        """
        redacted_data = redact_sensitive(data, sensitive_patterns=self._sensitive_patterns)
        return LogEvent(
            timestamp=datetime.now(tz=UTC),
            event_type=event_type,
            step_id=step_id,
            data=redacted_data,
            level=level,
        )

    def _record_and_dispatch(self, event: LogEvent) -> None:
        """Store the event in the RunLog and dispatch to sinks if verbosity passes.

        Verbosity filtering happens here: events below the logger's minimum
        verbosity for their type are dropped. Per-sink verbosity is handled
        inside ConsoleSink.emit() itself.

        Args:
            event: The LogEvent to record and dispatch.
        """
        # Always record in the RunLog (for replay/summary), subject to global verbosity
        if _event_passes_verbosity(event.event_type, self._verbosity):
            if self._run_log is not None:
                self._run_log.events.append(event)
            self._dispatch_to_sinks(event)

    def _dispatch_to_sinks(self, event: LogEvent) -> None:
        """Forward an event to all registered sinks.

        Exceptions raised by sinks are caught and logged to the standard
        logging system — they must never propagate to the executor.

        Args:
            event: The LogEvent to dispatch.
        """
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                sink.emit(event)

    def _flush_all_sinks(self) -> None:
        """Call flush() on all registered sinks."""
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                sink.flush()

    def _close_all_sinks(self) -> None:
        """Call close() on all registered sinks.

        Called at the end of on_workflow_complete() after flushing.  This is
        what triggers FileSink to write its buffered JSON file and
        JSONLinesSink to release its open file handle.
        """
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                sink.close()

    def close(self) -> None:
        """Flush and close all registered sinks.

        Call this for manual cleanup when the RunLogger is not driven by
        on_workflow_complete() (e.g. in tests or when the workflow is aborted
        before completion).
        """
        self._flush_all_sinks()
        self._close_all_sinks()

    def _build_summary(self, result: WorkflowResult) -> RunSummary:
        """Compute a RunSummary from the WorkflowResult and recorded events.

        Args:
            result: The final WorkflowResult from the executor.

        Returns:
            A RunSummary with counters derived from step results and events.
        """
        completed = sum(
            1 for sr in result.step_results.values() if sr.status == StepStatus.COMPLETED
        )
        failed = sum(
            1 for sr in result.step_results.values() if sr.status == StepStatus.FAILED_FINAL
        )
        skipped = sum(1 for sr in result.step_results.values() if sr.status == StepStatus.SKIPPED)
        total_retries = sum(max(0, len(sr.attempts) - 1) for sr in result.step_results.values())

        # Count validation events from the recorded events list
        validations_passed = sum(
            1
            for e in (self._run_log.events if self._run_log else [])
            if e.event_type == "validation_complete"
        )
        validations_failed = sum(
            1
            for e in (self._run_log.events if self._run_log else [])
            if e.event_type == "validation_fail"
        )

        return RunSummary(
            total_steps=len(result.step_results),
            completed_steps=completed,
            failed_steps=failed,
            skipped_steps=skipped,
            total_retries=total_retries,
            total_duration_ms=result.duration_ms,
            validations_passed=validations_passed,
            validations_failed=validations_failed,
        )
