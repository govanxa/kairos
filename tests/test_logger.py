"""Tests for kairos.logger — written BEFORE implementation."""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from kairos.enums import LogLevel, LogVerbosity, StepStatus, WorkflowStatus
from kairos.exceptions import ConfigError, SecurityError
from kairos.executor import WorkflowResult
from kairos.logger import LogEvent, RunLogger
from kairos.plan import TaskGraph
from kairos.schema import ValidationResult
from kairos.step import Step, StepContext, StepResult

# --- Module-level step action helpers (lambdas cannot carry type annotations) ---


def _action_ok(ctx: StepContext) -> dict[str, str]:
    return {"result": "ok"}


def _action_a(ctx: StepContext) -> dict[str, int]:
    return {"a": 1}


def _action_b(ctx: StepContext) -> dict[str, int]:
    return {"b": 2}


def _action_c(ctx: StepContext) -> dict[str, int]:
    return {"c": 3}


def _action_empty(ctx: StepContext) -> dict[str, object]:
    return {}


def _action_s1(ctx: StepContext) -> dict[str, object]:
    return {}


# --- Fixtures ---


@pytest.fixture
def simple_step() -> Step:
    """A simple step for testing."""
    return Step(name="test_step", action=_action_ok)


@pytest.fixture
def simple_graph(simple_step: Step) -> TaskGraph:
    """A simple one-step TaskGraph."""
    return TaskGraph(name="test_workflow", steps=[simple_step])


@pytest.fixture
def multi_step_graph() -> TaskGraph:
    """A multi-step TaskGraph."""
    steps = [
        Step(name="step_a", action=_action_a),
        Step(name="step_b", action=_action_b, depends_on=["step_a"]),
        Step(name="step_c", action=_action_c, depends_on=["step_b"]),
    ]
    return TaskGraph(name="multi_workflow", steps=steps)


@pytest.fixture
def simple_result(simple_step: Step) -> WorkflowResult:
    """A simple WorkflowResult for testing."""
    step_result = StepResult(
        step_id="test_step",
        status=StepStatus.COMPLETED,
        output={"result": "ok"},
        attempts=[],
        duration_ms=50.0,
        timestamp=datetime.now(tz=UTC),
    )
    return WorkflowResult(
        status=WorkflowStatus.COMPLETE,
        step_results={"test_step": step_result},
        final_state={"result": "ok"},
        duration_ms=100.0,
        timestamp=datetime.now(tz=UTC),
        llm_calls=0,
    )


@pytest.fixture
def run_logger() -> RunLogger:
    """A RunLogger with no sinks (silent)."""
    from kairos.logger import RunLogger

    return RunLogger(sinks=[])


@pytest.fixture
def console_stream() -> io.StringIO:
    """A StringIO stream to capture console output."""
    return io.StringIO()


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class CaptureSink:
    """A reusable LogSink that buffers every emitted event for inspection.

    Placed at module level to avoid ~15 inline duplicate class definitions
    across test methods. Usage::

        sink = CaptureSink()
        logger = RunLogger(sinks=[sink], ...)
        # ... exercise the logger ...
        assert sink.events[0].event_type == "workflow_start"
    """

    def __init__(self) -> None:
        self.events: list[LogEvent] = []
        self.flushed: bool = False
        self.closed: bool = False

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


# --- Group 1: Failure paths (write FIRST) ---


class TestFailurePaths:
    def test_get_run_log_before_run_returns_none(self):
        """get_run_log() returns None before on_workflow_start is called."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        assert logger.get_run_log() is None

    def test_non_callable_callback_raises_config_error(self):
        """CallbackSink rejects non-callables with ConfigError."""
        from kairos.logger import CallbackSink

        with pytest.raises(ConfigError, match="callable"):
            CallbackSink("not_a_function")  # type: ignore[arg-type]

    def test_path_traversal_rejected_jsonlines_sink(self, tmp_path: Path):
        """JSONLinesSink raises SecurityError on raw path traversal attempt in workflow name."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        # Raw '..' in name indicates traversal attempt — must be rejected
        with pytest.raises(SecurityError):
            sink.set_run_context("..", "run-123")

    def test_path_traversal_rejected_file_sink(self, tmp_path: Path):
        """FileSink raises SecurityError on raw path traversal attempt in workflow name."""
        from kairos.logger import FileSink

        sink = FileSink(base_dir=str(tmp_path))
        # Raw '..' in name indicates traversal attempt — must be rejected
        with pytest.raises(SecurityError):
            sink.set_run_context("..", "run-123")

    def test_sink_exception_does_not_propagate(self, simple_graph: TaskGraph, simple_step: Step):
        """Exceptions in sink.emit() are caught and never propagate to caller."""
        from kairos.logger import RunLogger

        class BrokenSink:
            def emit(self, event: LogEvent) -> None:
                raise RuntimeError("Sink is broken!")

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[BrokenSink()])  # type: ignore[list-item]
        # Should NOT raise even though sink raises
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)

    def test_exception_sanitization_in_on_step_fail(self, simple_step: Step):
        """on_step_fail sanitizes the exception before storing it in the event."""
        from kairos.logger import RunLogger

        captured_events: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured_events.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        exc = RuntimeError("Connection failed with key sk-abc123secret and Bearer xyz789token")
        logger.on_step_fail(simple_step, exc, attempt=1)

        assert len(captured_events) == 1
        event = captured_events[0]
        event_str = str(event.data)
        assert "sk-abc123secret" not in event_str
        assert "xyz789token" not in event_str

    def test_non_serializable_sink_data_does_not_crash(self, simple_step: Step):
        """RunLogger handles edge cases without crashing even with unusual step data."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        # on_step_fail with exception whose str() is very long
        exc = RuntimeError("x" * 1000)
        logger.on_step_fail(simple_step, exc, attempt=1)  # should not raise

    def test_flush_all_sinks_called_on_workflow_complete(self, simple_graph: TaskGraph):
        """flush() is called on all sinks when on_workflow_complete fires."""
        from kairos.logger import RunLogger

        flush_called: list[bool] = []

        class TrackingSink:
            def emit(self, event: LogEvent) -> None:
                pass

            def flush(self) -> None:
                flush_called.append(True)

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[TrackingSink()])  # type: ignore
        logger.on_workflow_start(simple_graph)
        result = WorkflowResult(
            status=WorkflowStatus.COMPLETE,
            step_results={},
            final_state={},
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
            llm_calls=0,
        )
        logger.on_workflow_complete(result)
        assert len(flush_called) == 1

    def test_callback_sink_rejects_none(self):
        """CallbackSink rejects None with ConfigError."""
        from kairos.logger import CallbackSink

        with pytest.raises(ConfigError):
            CallbackSink(None)  # type: ignore[arg-type]

    def test_run_log_status_set_from_workflow_result(
        self, simple_graph: TaskGraph, simple_result: WorkflowResult
    ):
        """RunLog.status is set to the WorkflowResult.status on workflow_complete."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])  # type: ignore[arg-type]
        logger.on_workflow_start(simple_graph)
        logger.on_workflow_complete(simple_result)
        run_log = logger.get_run_log()
        assert run_log is not None
        assert run_log.status == WorkflowStatus.COMPLETE

    def test_close_called_on_sinks_at_workflow_complete(
        self, simple_graph: TaskGraph, simple_result: WorkflowResult
    ):
        """close() is called on all sinks when on_workflow_complete fires.

        This is the mechanism that causes FileSink to write its buffered JSON
        file and JSONLinesSink to release its file handle.
        """
        from kairos.logger import RunLogger

        sink = CaptureSink()
        logger = RunLogger(sinks=[sink])  # type: ignore[arg-type]
        logger.on_workflow_start(simple_graph)
        assert not sink.closed, "close() must not be called before workflow_complete"
        logger.on_workflow_complete(simple_result)
        assert sink.closed, "close() must be called on every sink at workflow_complete"

    def test_run_logger_close_method_closes_all_sinks(self, simple_graph: TaskGraph):
        """RunLogger.close() manually flushes and closes all sinks."""
        from kairos.logger import RunLogger

        sink = CaptureSink()
        logger = RunLogger(sinks=[sink])  # type: ignore[arg-type]
        logger.on_workflow_start(simple_graph)
        logger.close()
        assert sink.flushed
        assert sink.closed


# --- Group 2: Boundary conditions ---


class TestBoundaryConditions:
    def test_no_sinks_runs_silently(self, simple_graph: TaskGraph, simple_step: Step):
        """RunLogger with empty sinks list runs without error."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)
        assert logger.get_run_log() is not None

    def test_empty_workflow_name_sanitized_in_file_sinks(self, tmp_path: Path):
        """A workflow name that is empty after sanitization raises SecurityError in file sinks.

        Note: sanitize_path replaces special chars with underscores, so non-empty
        all-special inputs like '@@@' become '___' (valid). Only a truly empty
        string or a path that escapes base_dir raises SecurityError.
        """
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        # Empty string produces empty sanitized name — that raises SecurityError
        with pytest.raises(SecurityError):
            sink.set_run_context("", "run-id-123")

    def test_special_chars_in_workflow_name_sanitized(self, tmp_path: Path):
        """Special characters in workflow name are replaced with underscores."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        # This should not raise — spaces become underscores
        sink.set_run_context("my workflow", "run-123")
        # Verify the file path uses sanitized name
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        basename = os.path.basename(sink_any._file_path)
        assert "@" not in basename
        assert " " not in basename

    def test_verbosity_minimal_filters_step_start(self, simple_graph: TaskGraph, simple_step: Step):
        """At MINIMAL verbosity, step_start events are NOT emitted."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)
        # workflow_start should pass through, step_start should be filtered
        event_types = [e.event_type for e in captured]
        assert "workflow_start" in event_types
        assert "step_start" not in event_types

    def test_verbosity_normal_includes_step_start(self, simple_graph: TaskGraph, simple_step: Step):
        """At NORMAL verbosity, step_start events ARE emitted."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.NORMAL)  # type: ignore
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)
        event_types = [e.event_type for e in captured]
        assert "step_start" in event_types

    def test_verbosity_normal_excludes_validation_start(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """At NORMAL verbosity, validation_start events are NOT emitted."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.NORMAL)  # type: ignore
        logger.on_validation_start(simple_step, phase="input", attempt=1)
        event_types = [e.event_type for e in captured]
        assert "validation_start" not in event_types

    def test_verbosity_verbose_includes_validation_start(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """At VERBOSE verbosity, validation_start events ARE emitted."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.VERBOSE)  # type: ignore
        logger.on_validation_start(simple_step, phase="input", attempt=1)
        event_types = [e.event_type for e in captured]
        assert "validation_start" in event_types

    def test_per_sink_verbosity_overrides_logger_verbosity(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """A sink with its own verbosity only receives events at that level or above."""
        from kairos.logger import ConsoleSink, RunLogger

        stream = io.StringIO()
        # Logger is VERBOSE but console sink is MINIMAL
        sink = ConsoleSink(stream=stream, verbosity=LogVerbosity.MINIMAL)
        logger = RunLogger(sinks=[sink], verbosity=LogVerbosity.VERBOSE)
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)
        output = stream.getvalue()
        assert "workflow_start" in output
        assert "step_start" not in output

    def test_multiple_workflow_runs_each_get_new_run_id(self, simple_graph: TaskGraph):
        """Each call to on_workflow_start creates a new run_id."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        logger.on_workflow_start(simple_graph)
        run_log1 = logger.get_run_log()
        assert run_log1 is not None
        run_id_1 = run_log1.run_id

        logger.on_workflow_start(simple_graph)
        run_log2 = logger.get_run_log()
        assert run_log2 is not None
        run_id_2 = run_log2.run_id

        assert run_id_1 != run_id_2

    def test_foreach_empty_collection_no_crash(self):
        """RunLogger handles steps without step_id gracefully."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        # No graph started — just calling step_start without workflow context
        step = Step(name="solo_step", action=_action_empty)
        logger.on_step_start(step, attempt=1)  # Should not crash

    def test_step_fail_minimal_verbosity_emits_event(self, simple_step: Step):
        """step_fail events are emitted at MINIMAL verbosity."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        logger.on_step_fail(simple_step, RuntimeError("oops"), attempt=1)
        event_types = [e.event_type for e in captured]
        assert "step_fail" in event_types

    def test_validation_fail_minimal_verbosity_emits_event(self, simple_step: Step):
        """validation_fail events are emitted at MINIMAL verbosity."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        result = ValidationResult(valid=False, errors=[])
        logger.on_validation_fail(simple_step, phase="output", result=result, attempt=1)
        event_types = [e.event_type for e in captured]
        assert "validation_fail" in event_types


# --- Group 3: Happy paths ---


class TestLogEventCreation:
    def test_log_event_has_required_fields(self):
        """LogEvent dataclass has all required fields."""
        from kairos.logger import LogEvent

        now = datetime.now(tz=UTC)
        event = LogEvent(
            timestamp=now,
            event_type="step_start",
            step_id="my_step",
            data={"attempt": 1},
            level=LogLevel.INFO,
        )
        assert event.timestamp == now
        assert event.event_type == "step_start"
        assert event.step_id == "my_step"
        assert event.data == {"attempt": 1}
        assert event.level == LogLevel.INFO

    def test_log_event_step_id_can_be_none(self):
        """LogEvent step_id is optional (None for workflow-level events)."""
        from kairos.logger import LogEvent

        event = LogEvent(
            timestamp=datetime.now(tz=UTC),
            event_type="workflow_start",
            step_id=None,
            data={},
            level=LogLevel.INFO,
        )
        assert event.step_id is None

    def test_log_event_to_dict(self):
        """LogEvent.to_dict() produces a JSON-serializable dict."""
        from kairos.logger import LogEvent

        now = datetime.now(tz=UTC)
        event = LogEvent(
            timestamp=now,
            event_type="step_complete",
            step_id="step_x",
            data={"status": "completed"},
            level=LogLevel.INFO,
        )
        d = event.to_dict()
        assert d["event_type"] == "step_complete"
        assert d["step_id"] == "step_x"
        assert d["level"] == "info"
        assert isinstance(d["timestamp"], str)  # ISO 8601
        # Must be JSON-serializable
        json.dumps(d)


class TestRunSummaryCreation:
    def test_run_summary_has_all_fields(self):
        """RunSummary dataclass has all required counter fields."""
        from kairos.logger import RunSummary

        summary = RunSummary(
            total_steps=3,
            completed_steps=2,
            failed_steps=1,
            skipped_steps=0,
            total_retries=2,
            total_duration_ms=500.0,
            validations_passed=4,
            validations_failed=1,
        )
        assert summary.total_steps == 3
        assert summary.completed_steps == 2
        assert summary.failed_steps == 1
        assert summary.total_retries == 2

    def test_run_summary_to_dict(self):
        """RunSummary.to_dict() produces a JSON-serializable dict."""
        from kairos.logger import RunSummary

        summary = RunSummary(
            total_steps=2,
            completed_steps=2,
            failed_steps=0,
            skipped_steps=0,
            total_retries=0,
            total_duration_ms=100.0,
            validations_passed=2,
            validations_failed=0,
        )
        d = summary.to_dict()
        assert d["total_steps"] == 2
        assert d["total_duration_ms"] == 100.0
        json.dumps(d)


class TestRunLogCreation:
    def test_run_log_has_required_fields(self, simple_graph: TaskGraph):
        """RunLog dataclass has all required fields."""
        from kairos.logger import RunLog, RunSummary

        now = datetime.now(tz=UTC)
        summary = RunSummary(0, 0, 0, 0, 0, 0.0, 0, 0)
        log = RunLog(
            run_id="test-run-123",
            workflow_name="test_workflow",
            started_at=now,
            completed_at=None,
            status=WorkflowStatus.COMPLETE,
            events=[],
            summary=summary,
        )
        assert log.run_id == "test-run-123"
        assert log.workflow_name == "test_workflow"
        assert log.completed_at is None
        assert log.events == []

    def test_run_log_to_dict(self):
        """RunLog.to_dict() produces a JSON-serializable dict."""
        from kairos.logger import RunLog, RunSummary

        now = datetime.now(tz=UTC)
        summary = RunSummary(1, 1, 0, 0, 0, 50.0, 1, 0)
        log = RunLog(
            run_id="abc-123",
            workflow_name="my_workflow",
            started_at=now,
            completed_at=now,
            status=WorkflowStatus.COMPLETE,
            events=[],
            summary=summary,
        )
        d = log.to_dict()
        assert d["run_id"] == "abc-123"
        assert d["workflow_name"] == "my_workflow"
        assert d["status"] == "complete"
        assert isinstance(d["started_at"], str)
        assert isinstance(d["completed_at"], str)
        json.dumps(d)


class TestRunLogStoresInitialPlan:
    def test_run_log_stores_initial_plan(self, simple_graph: TaskGraph):
        """on_workflow_start stores the TaskGraph as initial_plan on the RunLog."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])  # type: ignore[arg-type]
        logger.on_workflow_start(simple_graph)
        run_log = logger.get_run_log()
        assert run_log is not None
        assert run_log.initial_plan is simple_graph

    def test_run_log_initial_plan_serializes_to_dict(self, simple_graph: TaskGraph):
        """RunLog.to_dict() includes initial_plan serialized via TaskGraph.to_dict()."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])  # type: ignore[arg-type]
        logger.on_workflow_start(simple_graph)
        run_log = logger.get_run_log()
        assert run_log is not None
        d = run_log.to_dict()
        assert "initial_plan" in d
        assert d["initial_plan"] is not None
        assert isinstance(d["initial_plan"], dict)
        # Must be JSON-serializable
        import json

        json.dumps(d)

    def test_run_log_initial_plan_none_when_not_set(self):
        """RunLog.initial_plan defaults to None."""
        from datetime import UTC, datetime

        from kairos.logger import RunLog, RunSummary

        now = datetime.now(tz=UTC)
        summary = RunSummary(0, 0, 0, 0, 0, 0.0, 0, 0)
        log = RunLog(
            run_id="x",
            workflow_name="w",
            started_at=now,
            completed_at=None,
            status=WorkflowStatus.COMPLETE,
            events=[],
            summary=summary,
        )
        assert log.initial_plan is None
        d = log.to_dict()
        assert d["initial_plan"] is None


class TestRunLoggerHooks:
    def test_workflow_start_creates_run_log(self, simple_graph: TaskGraph):
        """on_workflow_start creates a RunLog with a UUID run_id."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        assert logger.get_run_log() is None
        logger.on_workflow_start(simple_graph)
        run_log = logger.get_run_log()
        assert run_log is not None
        assert run_log.workflow_name == "test_workflow"
        assert len(run_log.run_id) == 36  # UUID4 format

    def test_workflow_start_dispatches_event(self, simple_graph: TaskGraph):
        """on_workflow_start emits a workflow_start event."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        logger.on_workflow_start(simple_graph)
        assert len(captured) == 1
        assert captured[0].event_type == "workflow_start"

    def test_step_start_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_start records a step_start event at NORMAL+ verbosity."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "step_start" in event_types

    def test_step_complete_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_complete records a step_complete event."""
        from kairos.logger import RunLogger

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={"result": "ok"},
            attempts=[],
            duration_ms=50.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_complete(simple_step, step_result)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "step_complete" in event_types

    def test_step_fail_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_fail records a step_fail event at MINIMAL+ verbosity."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_fail(simple_step, RuntimeError("bad"), attempt=1)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "step_fail" in event_types

    def test_step_retry_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_retry records a step_retry event at NORMAL+ verbosity."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_retry(simple_step, attempt=2)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "step_retry" in event_types

    def test_step_skip_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_skip records a step_skip event at NORMAL+ verbosity."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_skip(simple_step, reason="dependency failed")
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "step_skip" in event_types

    def test_validation_start_records_event_at_verbose(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """on_validation_start records event only at VERBOSE verbosity."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.VERBOSE)
        logger.on_workflow_start(simple_graph)
        logger.on_validation_start(simple_step, phase="input", attempt=1)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "validation_start" in event_types

    def test_validation_complete_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_validation_complete records event at NORMAL+ verbosity."""
        from kairos.logger import RunLogger

        result = ValidationResult(valid=True, errors=[])
        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_validation_complete(simple_step, phase="output", result=result)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "validation_complete" in event_types

    def test_validation_fail_records_event(self, simple_graph: TaskGraph, simple_step: Step):
        """on_validation_fail records event at MINIMAL+ verbosity."""
        from kairos.logger import RunLogger

        result = ValidationResult(valid=False, errors=[])
        logger = RunLogger(sinks=[], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_validation_fail(simple_step, phase="output", result=result, attempt=1)
        run_log = logger.get_run_log()
        assert run_log is not None
        event_types = [e.event_type for e in run_log.events]
        assert "validation_fail" in event_types

    def test_workflow_complete_finalizes_run_log(
        self, simple_graph: TaskGraph, simple_result: WorkflowResult
    ):
        """on_workflow_complete sets completed_at and status on the RunLog."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_workflow_complete(simple_result)
        run_log = logger.get_run_log()
        assert run_log is not None
        assert run_log.completed_at is not None
        assert run_log.status == WorkflowStatus.COMPLETE

    def test_workflow_complete_dispatches_event(
        self, simple_graph: TaskGraph, simple_result: WorkflowResult
    ):
        """on_workflow_complete emits a workflow_complete event."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        logger.on_workflow_start(simple_graph)
        logger.on_workflow_complete(simple_result)
        event_types = [e.event_type for e in captured]
        assert "workflow_complete" in event_types

    def test_dispatch_to_multiple_sinks(self, simple_graph: TaskGraph):
        """RunLogger dispatches events to all registered sinks."""
        from kairos.logger import RunLogger

        counts = [0, 0]

        class CountSink:
            def __init__(self, idx: int):
                self.idx = idx

            def emit(self, event: LogEvent) -> None:
                counts[self.idx] += 1

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        sink1 = CountSink(0)
        sink2 = CountSink(1)
        logger = RunLogger(sinks=[sink1, sink2], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        logger.on_workflow_start(simple_graph)
        assert counts[0] == 1
        assert counts[1] == 1

    def test_summary_built_from_events(self, simple_graph: TaskGraph, simple_step: Step):
        """RunSummary is computed from the recorded events when workflow completes."""
        from kairos.logger import RunLogger

        step_result_ok = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={},
            attempts=[],
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
        )
        wf_result = WorkflowResult(
            status=WorkflowStatus.COMPLETE,
            step_results={"test_step": step_result_ok},
            final_state={},
            duration_ms=50.0,
            timestamp=datetime.now(tz=UTC),
            llm_calls=0,
        )
        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)
        logger.on_step_complete(simple_step, step_result_ok)
        logger.on_workflow_complete(wf_result)

        run_log = logger.get_run_log()
        assert run_log is not None
        assert run_log.summary.completed_steps == 1
        assert run_log.summary.total_duration_ms == 50.0


class TestConsoleSink:
    def test_console_sink_writes_to_stream(self, simple_graph: TaskGraph):
        """ConsoleSink writes formatted events to the provided stream."""
        from kairos.logger import ConsoleSink, RunLogger

        stream = io.StringIO()
        sink = ConsoleSink(stream=stream, verbosity=LogVerbosity.MINIMAL)
        logger = RunLogger(sinks=[sink], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(simple_graph)
        output = stream.getvalue()
        assert len(output) > 0
        assert "workflow_start" in output

    def test_console_sink_includes_level(self, simple_graph: TaskGraph):
        """ConsoleSink output includes the log level."""
        from kairos.logger import ConsoleSink, RunLogger

        stream = io.StringIO()
        sink = ConsoleSink(stream=stream, verbosity=LogVerbosity.MINIMAL)
        logger = RunLogger(sinks=[sink], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(simple_graph)
        output = stream.getvalue()
        # INFO or WARN or ERROR should appear
        assert any(level in output.upper() for level in ["INFO", "WARN", "ERROR"])

    def test_console_sink_default_stream_is_stderr(self):
        """ConsoleSink uses stderr by default."""
        from kairos.logger import ConsoleSink

        sink = ConsoleSink()
        assert cast(Any, sink)._stream is sys.stderr

    def test_console_sink_flush(self):
        """ConsoleSink.flush() does not raise."""
        from kairos.logger import ConsoleSink

        stream = io.StringIO()
        sink = ConsoleSink(stream=stream)
        sink.flush()  # Should not raise

    def test_console_sink_close(self):
        """ConsoleSink.close() does not raise."""
        from kairos.logger import ConsoleSink

        stream = io.StringIO()
        sink = ConsoleSink(stream=stream)
        sink.close()  # Should not raise


class TestJSONLinesSink:
    def test_jsonlines_sink_writes_one_json_per_line(self, tmp_path: Path):
        """JSONLinesSink writes one JSON object per event line."""
        from kairos.logger import JSONLinesSink, RunLogger

        sink = JSONLinesSink(base_dir=str(tmp_path))
        graph = TaskGraph(name="my_workflow", steps=[Step(name="s1", action=_action_s1)])
        logger = RunLogger(sinks=[sink], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(graph)

        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        with open(sink_any._file_path) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        assert len(lines) >= 1
        for line in lines:
            obj = json.loads(line)
            assert "event_type" in obj

    def test_jsonlines_sink_filename_uses_sanitized_name(self, tmp_path: Path):
        """JSONLinesSink filename contains the sanitized workflow name."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        sink.set_run_context("my-workflow", "abc-123")
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        assert "my-workflow" in sink_any._file_path or "my_workflow" in sink_any._file_path

    def test_jsonlines_sink_stays_within_base_dir(self, tmp_path: Path):
        """JSONLinesSink file path is within the configured base_dir."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        sink.set_run_context("workflow", "run-1")
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        real_base = os.path.realpath(str(tmp_path))
        real_file = os.path.realpath(sink_any._file_path)
        assert real_file.startswith(real_base)


class TestFileSink:
    def test_file_sink_writes_complete_json_on_close(self, tmp_path: Path):
        """FileSink writes a single complete JSON file when close() is called."""
        from kairos.logger import FileSink, RunLog, RunLogger, RunSummary

        sink = FileSink(base_dir=str(tmp_path))
        graph = TaskGraph(name="file_workflow", steps=[Step(name="fs1", action=_action_s1)])
        logger = RunLogger(sinks=[sink], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(graph)

        now = datetime.now(tz=UTC)
        summary = RunSummary(1, 1, 0, 0, 0, 50.0, 0, 0)
        run_log = RunLog(
            run_id="file-run-123",
            workflow_name="file_workflow",
            started_at=now,
            completed_at=now,
            status=WorkflowStatus.COMPLETE,
            events=[],
            summary=summary,
        )
        sink.set_run_log(run_log)
        sink.close()

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            data = json.load(f)
        assert "run_id" in data

    def test_file_sink_stays_within_base_dir(self, tmp_path: Path):
        """FileSink file path is within the configured base_dir."""
        from kairos.logger import FileSink

        sink = FileSink(base_dir=str(tmp_path))
        sink.set_run_context("workflow", "run-1")
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        real_base = os.path.realpath(str(tmp_path))
        real_file = os.path.realpath(sink_any._file_path)
        assert real_file.startswith(real_base)


class TestCallbackSink:
    def test_callback_sink_calls_callback_on_emit(self):
        """CallbackSink forwards events to the registered callback."""
        from kairos.logger import CallbackSink, LogEvent

        received: list[LogEvent] = []

        def cb(event: LogEvent) -> None:
            received.append(event)

        sink = CallbackSink(cb)
        event = LogEvent(
            timestamp=datetime.now(tz=UTC),
            event_type="step_start",
            step_id="step_1",
            data={"attempt": 1},
            level=LogLevel.INFO,
        )
        sink.emit(event)
        assert len(received) == 1
        assert received[0].event_type == "step_start"

    def test_callback_sink_accepts_any_callable(self):
        """CallbackSink accepts lambda, function, or any callable."""
        from kairos.logger import CallbackSink

        sink = CallbackSink(lambda e: None)
        assert sink is not None

    def test_callback_sink_logs_trust_warning_at_init(self, capsys: pytest.CaptureFixture[str]):
        """CallbackSink prints a trust boundary warning when registered."""
        from kairos.logger import CallbackSink, RunLogger

        # The warning should be emitted at RunLogger level during init, so let's test
        # via capturing warnings from RunLogger construction
        captured_warnings: list[str] = []
        original_warn = __import__("warnings").warn

        def capture_warn(msg: object, *args: object, **kwargs: object) -> None:
            if isinstance(msg, str):
                captured_warnings.append(msg)
            original_warn(msg, *args, **kwargs)  # type: ignore[arg-type]

        with patch("warnings.warn", side_effect=capture_warn):
            sink = CallbackSink(lambda e: None)
            RunLogger(sinks=[sink], verbosity=LogVerbosity.NORMAL)

        assert any("CallbackSink" in w or "callback" in w.lower() for w in captured_warnings)

    def test_callback_sink_flush_and_close_do_not_raise(self):
        """CallbackSink flush() and close() are no-ops that do not raise."""
        from kairos.logger import CallbackSink

        sink = CallbackSink(lambda e: None)
        sink.flush()
        sink.close()


# --- Group 4: Security ---


class TestSecuritySensitiveKeyRedaction:
    def test_sensitive_key_in_step_output_redacted_before_emit(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """State data containing api_key is redacted before reaching any sink."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(
            sinks=[CaptureSink()],
            verbosity=LogVerbosity.VERBOSE,  # type: ignore
        )
        logger.on_workflow_start(simple_graph)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={"result": "ok", "api_key": "sk-secret-12345"},
            attempts=[],
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        for event in captured:
            event_str = json.dumps(event.data)
            assert "sk-secret-12345" not in event_str

    def test_sensitive_key_password_redacted(self, simple_graph: TaskGraph, simple_step: Step):
        """State data containing password key is redacted before reaching sinks."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(
            sinks=[CaptureSink()],
            verbosity=LogVerbosity.VERBOSE,  # type: ignore
        )
        logger.on_workflow_start(simple_graph)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={"password": "super-secret", "result": "ok"},
            attempts=[],
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        for event in captured:
            data_str = json.dumps(event.data)
            assert "super-secret" not in data_str

    def test_custom_sensitive_patterns_applied(self, simple_graph: TaskGraph, simple_step: Step):
        """Custom sensitive_patterns passed to RunLogger are applied during redaction."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(
            sinks=[CaptureSink()],  # type: ignore
            verbosity=LogVerbosity.VERBOSE,
            sensitive_patterns=["*my_custom_secret*"],
        )
        logger.on_workflow_start(simple_graph)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={"my_custom_secret": "TOPSECRET", "result": "ok"},
            attempts=[],
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        for event in captured:
            data_str = json.dumps(event.data)
            assert "TOPSECRET" not in data_str


class TestSecurityExceptionSanitization:
    def test_api_key_in_exception_redacted(self, simple_step: Step):
        """API keys in exception messages are redacted before logging."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        exc = RuntimeError("Request failed: sk-proj-abc123xyz")
        logger.on_step_fail(simple_step, exc, attempt=1)
        assert len(captured) == 1
        data_str = str(captured[0].data)
        assert "sk-proj-abc123xyz" not in data_str

    def test_bearer_token_in_exception_redacted(self, simple_step: Step):
        """Bearer tokens in exception messages are redacted before logging."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        exc = RuntimeError("Unauthorized: Bearer abc-secret-token-xyz")
        logger.on_step_fail(simple_step, exc, attempt=1)
        data_str = str(captured[0].data)
        assert "abc-secret-token-xyz" not in data_str

    def test_exception_message_truncated_to_500(self, simple_step: Step):
        """Exception messages longer than 500 chars are truncated in log events."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        exc = RuntimeError("x" * 600)
        logger.on_step_fail(simple_step, exc, attempt=1)
        data_str = captured[0].data.get("error_message", "")
        assert isinstance(data_str, str)
        assert len(data_str) <= 500

    def test_file_path_in_exception_stripped_to_filename(self, simple_step: Step):
        """File paths in exception messages are stripped to filename only."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(sinks=[CaptureSink()], verbosity=LogVerbosity.MINIMAL)  # type: ignore
        exc = RuntimeError("Error in /home/user/projects/kairos/config/secrets.yaml")
        logger.on_step_fail(simple_step, exc, attempt=1)
        data_str = str(captured[0].data)
        assert "/home/user/projects/kairos/config/" not in data_str
        # Filename should still be present
        assert "secrets.yaml" in data_str

    def test_path_sanitization_in_jsonlines_sink(self, tmp_path: Path):
        """JSONLinesSink sanitizes special characters in file paths."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        # Workflow name with spaces and special chars
        sink.set_run_context("my workflow!", "run-123")
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        basename = os.path.basename(sink_any._file_path)
        # No spaces or ! in the filename
        assert " " not in basename
        assert "!" not in basename

    def test_callback_sink_verbose_warning_emitted(self):
        """CallbackSink at VERBOSE verbosity emits an extra security warning."""
        from kairos.logger import CallbackSink, RunLogger

        captured_warnings: list[str] = []

        def capture_warn(msg: object, *args: object, **kwargs: object) -> None:
            if isinstance(msg, str):
                captured_warnings.append(msg)

        with patch("warnings.warn", side_effect=capture_warn):
            sink = CallbackSink(lambda e: None)
            RunLogger(sinks=[sink], verbosity=LogVerbosity.VERBOSE)

        warning_text = " ".join(captured_warnings)
        # At VERBOSE level, should warn about full inputs/outputs
        assert "verbose" in warning_text.lower() or "CallbackSink" in warning_text

    def test_redaction_applied_before_dispatch_not_after(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """Redaction happens at RunLogger level, not inside sinks. Sink receives clean data."""
        from kairos.logger import RunLogger

        captured: list[LogEvent] = []

        class CaptureSink:
            def emit(self, event: LogEvent) -> None:
                captured.append(event)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        logger = RunLogger(
            sinks=[CaptureSink()],
            verbosity=LogVerbosity.VERBOSE,  # type: ignore
        )
        logger.on_workflow_start(simple_graph)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={"secret": "my-secret-value", "result": "ok"},
            attempts=[],
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        # The event received by the sink should have redacted secret
        for event in captured:
            if event.event_type == "step_complete":
                output_data = event.data.get("output", {})
                if isinstance(output_data, dict):
                    assert cast(dict[str, object], output_data).get("secret") == "[REDACTED]"


# --- Group 5: Serialization ---


class TestSerialization:
    def test_log_event_round_trip_json(self):
        """LogEvent survives a JSON round-trip via to_dict()."""
        from kairos.logger import LogEvent

        now = datetime.now(tz=UTC)
        event = LogEvent(
            timestamp=now,
            event_type="workflow_start",
            step_id=None,
            data={"workflow_name": "test", "total_steps": 3},
            level=LogLevel.INFO,
        )
        d = event.to_dict()
        serialized = json.dumps(d)
        restored = json.loads(serialized)
        assert restored["event_type"] == "workflow_start"
        assert restored["step_id"] is None
        assert restored["level"] == "info"

    def test_run_summary_round_trip_json(self):
        """RunSummary survives a JSON round-trip via to_dict()."""
        from kairos.logger import RunSummary

        summary = RunSummary(5, 4, 1, 0, 2, 1234.5, 8, 1)
        d = summary.to_dict()
        serialized = json.dumps(d)
        restored = json.loads(serialized)
        assert restored["total_steps"] == 5
        assert restored["total_retries"] == 2
        assert restored["total_duration_ms"] == 1234.5

    def test_run_log_round_trip_json(self):
        """RunLog survives a JSON round-trip via to_dict()."""
        from kairos.logger import LogEvent, RunLog, RunSummary

        now = datetime.now(tz=UTC)
        summary = RunSummary(1, 1, 0, 0, 0, 50.0, 1, 0)
        event = LogEvent(
            timestamp=now,
            event_type="workflow_start",
            step_id=None,
            data={},
            level=LogLevel.INFO,
        )
        log = RunLog(
            run_id="round-trip-test",
            workflow_name="rt_workflow",
            started_at=now,
            completed_at=now,
            status=WorkflowStatus.COMPLETE,
            events=[event],
            summary=summary,
        )
        d = log.to_dict()
        serialized = json.dumps(d)
        restored = json.loads(serialized)
        assert restored["run_id"] == "round-trip-test"
        assert restored["status"] == "complete"
        assert len(restored["events"]) == 1

    def test_datetime_serialized_as_iso8601(self):
        """Datetimes in to_dict() are ISO 8601 strings."""
        from kairos.logger import LogEvent

        now = datetime.now(tz=UTC)
        event = LogEvent(
            timestamp=now, event_type="test", step_id=None, data={}, level=LogLevel.INFO
        )
        d = event.to_dict()
        ts = d["timestamp"]
        assert isinstance(ts, str)
        # Parse it back — should not raise
        datetime.fromisoformat(ts)

    def test_enums_serialized_as_strings(self):
        """Enum values in to_dict() are strings, not enum objects."""
        from kairos.logger import LogEvent

        event = LogEvent(
            timestamp=datetime.now(tz=UTC),
            event_type="step_fail",
            step_id="x",
            data={},
            level=LogLevel.ERROR,
        )
        d = event.to_dict()
        assert isinstance(d["level"], str)
        assert d["level"] == "error"

    def test_run_log_with_events_is_fully_json_serializable(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """A fully populated RunLog (with events from hooks) serializes cleanly."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[], verbosity=LogVerbosity.NORMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_step_start(simple_step, attempt=1)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output={"x": 1},
            attempts=[],
            duration_ms=5.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        wf_result = WorkflowResult(
            status=WorkflowStatus.COMPLETE,
            step_results={"test_step": step_result},
            final_state={"x": 1},
            duration_ms=20.0,
            timestamp=datetime.now(tz=UTC),
            llm_calls=0,
        )
        logger.on_workflow_complete(wf_result)

        run_log = logger.get_run_log()
        assert run_log is not None
        d = run_log.to_dict()
        # Must not raise
        json.dumps(d)


# ---------------------------------------------------------------------------
# New tests for code-review and security-audit findings
# ---------------------------------------------------------------------------


class TestSinkIsolation:
    """Verify that a failing sink never silences events to healthy sinks."""

    def test_second_sink_receives_events_when_first_fails(self, simple_graph: TaskGraph):
        """Events reach a healthy sink even when the preceding sink raises on emit().

        The RunLogger must catch per-sink exceptions in isolation so that one
        broken sink cannot starve downstream sinks.
        """
        from kairos.logger import RunLogger

        class BrokenSink:
            def emit(self, event: LogEvent) -> None:
                raise RuntimeError("I am broken!")

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        healthy = CaptureSink()
        logger = RunLogger(
            sinks=[BrokenSink(), healthy],  # type: ignore[list-item]
            verbosity=LogVerbosity.MINIMAL,
        )
        logger.on_workflow_start(simple_graph)
        # The BrokenSink raises, but healthy must still receive all events
        assert len(healthy.events) >= 1
        assert healthy.events[0].event_type == "workflow_start"


class TestPathTraversalExtended:
    """Additional path traversal attack vector tests for file sinks."""

    def test_absolute_unix_path_rejected_in_jsonlines_sink(self, tmp_path: Path):
        """JSONLinesSink rejects Unix absolute paths in workflow names."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        with pytest.raises(SecurityError):
            sink.set_run_context("/etc/passwd", "run-123")

    def test_absolute_windows_path_rejected_in_jsonlines_sink(self, tmp_path: Path):
        """JSONLinesSink rejects Windows absolute paths in workflow names."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        with pytest.raises(SecurityError):
            sink.set_run_context("C:\\windows\\system32", "run-123")

    def test_null_byte_in_name_sanitized_not_rejected(self, tmp_path: Path):
        """Null bytes in workflow names are replaced with underscores, not crash.

        sanitize_path replaces all non-[a-zA-Z0-9_-] characters with '_', so a
        null byte becomes '_' rather than causing an error.  The resulting name
        is valid and the file is created without error.
        """
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        # Null byte is a non-[a-zA-Z0-9_-] char — sanitize_path replaces it
        # The _check_raw_name_for_traversal guard does NOT reject null bytes
        # (they are not '..' or an absolute-path prefix), so this must succeed.
        sink.set_run_context("workflow\x00name", "run-123")
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        assert "\x00" not in sink_any._file_path

    def test_url_encoded_traversal_sanitized(self, tmp_path: Path):
        """URL-encoded traversal sequences (%2e%2e) are sanitized to underscores.

        '%2e%2e' is not literally '..', so _check_raw_name_for_traversal does
        NOT block it.  sanitize_path replaces '%' with '_', making the name
        safe.  The key guarantee is that no actual path traversal occurs.
        """
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        # URL-encoded dots are not the literal '..' — they get sanitized to '_'
        sink.set_run_context("%2e%2e%2fetc%2fpasswd", "run-123")
        sink_any = cast(Any, sink)
        assert sink_any._file_path is not None
        real_base = str(tmp_path)
        import os

        assert os.path.realpath(sink_any._file_path).startswith(os.path.realpath(real_base))

    def test_absolute_unix_path_rejected_in_file_sink(self, tmp_path: Path):
        """FileSink rejects Unix absolute paths in workflow names."""
        from kairos.logger import FileSink

        sink = FileSink(base_dir=str(tmp_path))
        with pytest.raises(SecurityError):
            sink.set_run_context("/etc/passwd", "run-123")

    def test_absolute_windows_path_rejected_in_file_sink(self, tmp_path: Path):
        """FileSink rejects Windows absolute paths in workflow names."""
        from kairos.logger import FileSink

        sink = FileSink(base_dir=str(tmp_path))
        with pytest.raises(SecurityError):
            sink.set_run_context("C:\\windows\\system32", "run-123")


class TestNonDictOutputRedaction:
    """Verify that non-dict step outputs are credential-scanned at VERBOSE level."""

    def test_non_dict_output_with_credential_redacted_at_verbose(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """A step returning a string with a credential is sanitized at VERBOSE level.

        When result.output is not a dict (e.g. a plain string), the raw value
        must NOT flow into log events.  It should be sanitized via
        sanitize_exception() which strips sk-*, Bearer *, etc.
        """
        from kairos.logger import RunLogger

        captured = CaptureSink()
        logger = RunLogger(
            sinks=[captured],  # type: ignore[list-item]
            verbosity=LogVerbosity.VERBOSE,
        )
        logger.on_workflow_start(simple_graph)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output="Fetched data with key sk-abc123secret attached",  # type: ignore[arg-type]
            attempts=[],
            duration_ms=10.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        for event in captured.events:
            event_str = json.dumps(event.data, default=str)
            assert "sk-abc123secret" not in event_str, (
                "Credential in non-dict output must be redacted before reaching sinks"
            )

    def test_non_dict_output_none_not_stored_at_verbose(
        self, simple_graph: TaskGraph, simple_step: Step
    ):
        """None output at VERBOSE level stores nothing in the output field."""
        from kairos.logger import RunLogger

        captured = CaptureSink()
        logger = RunLogger(
            sinks=[captured],  # type: ignore[list-item]
            verbosity=LogVerbosity.VERBOSE,
        )
        logger.on_workflow_start(simple_graph)

        step_result = StepResult(
            step_id="test_step",
            status=StepStatus.COMPLETED,
            output=None,
            attempts=[],
            duration_ms=5.0,
            timestamp=datetime.now(tz=UTC),
        )
        logger.on_step_complete(simple_step, step_result)

        for event in captured.events:
            if event.event_type == "step_complete":
                # output key should NOT be present for None output
                assert "output" not in event.data


class TestSkipReasonRedaction:
    """Verify that skip reasons are credential-scanned before dispatch."""

    def test_skip_reason_with_credential_redacted(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_skip sanitizes credentials in the reason string.

        A skip reason containing a credential string (e.g. 'sk-*') must be
        sanitized before the event is stored or dispatched to sinks.
        """
        from kairos.logger import RunLogger

        captured = CaptureSink()
        logger = RunLogger(
            sinks=[captured],  # type: ignore[list-item]
            verbosity=LogVerbosity.NORMAL,
        )
        logger.on_workflow_start(simple_graph)
        logger.on_step_skip(
            simple_step,
            reason="Skipped because token=sk-secret999 was invalid",
        )

        for event in captured.events:
            if event.event_type == "step_skip":
                reason_str = str(event.data.get("reason", ""))
                assert "sk-secret999" not in reason_str, (
                    "Credentials in skip reasons must be redacted"
                )

    def test_skip_reason_normal_text_preserved(self, simple_graph: TaskGraph, simple_step: Step):
        """on_step_skip preserves non-credential skip reasons unchanged."""
        from kairos.logger import RunLogger

        captured = CaptureSink()
        logger = RunLogger(
            sinks=[captured],  # type: ignore[list-item]
            verbosity=LogVerbosity.NORMAL,
        )
        logger.on_workflow_start(simple_graph)
        logger.on_step_skip(simple_step, reason="upstream dependency failed")

        skip_events = [e for e in captured.events if e.event_type == "step_skip"]
        assert len(skip_events) == 1
        assert "upstream dependency failed" in str(skip_events[0].data.get("reason", ""))


# ---------------------------------------------------------------------------
# QA-added tests — coverage gaps for JSONLinesSink/FileSink lifecycle and
# defensive guards in RunLogger/FileSink.
# ---------------------------------------------------------------------------


class TestJSONLinesSinkLifecycle:
    """Cover JSONLinesSink flush/close with an active file handle and emit
    before set_run_context."""

    def test_emit_before_set_run_context_is_noop(self, tmp_path: Path):
        """Emitting an event before set_run_context is called does nothing."""
        from kairos.logger import JSONLinesSink, LogEvent

        sink = JSONLinesSink(base_dir=str(tmp_path))
        event = LogEvent(
            timestamp=datetime.now(tz=UTC),
            event_type="step_start",
            step_id="s",
            data={},
            level=LogLevel.INFO,
        )
        # Should silently return — no file handle open
        sink.emit(event)
        # No files created
        assert list(tmp_path.iterdir()) == []

    def test_flush_with_active_file_handle(self, tmp_path: Path):
        """flush() delegates to the underlying file handle when open."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        sink.set_run_context("wf", "run-1")
        # Should not raise and should flush the open handle
        sink.flush()
        assert cast(Any, sink)._file_handle is not None

    def test_close_releases_file_handle(self, tmp_path: Path):
        """close() closes the file handle and sets it to None."""
        from kairos.logger import JSONLinesSink

        sink = JSONLinesSink(base_dir=str(tmp_path))
        sink.set_run_context("wf", "run-2")
        assert cast(Any, sink)._file_handle is not None
        sink.close()
        assert cast(Any, sink)._file_handle is None


class TestFileSinkLifecycle:
    """Cover FileSink.close() edge cases: no file path, and fallback to
    buffered events when set_run_log was never called."""

    def test_close_without_set_run_context_is_noop(self):
        """close() does nothing when set_run_context was never called."""
        from kairos.logger import FileSink

        sink = FileSink(base_dir=".")
        # _file_path is None — close should silently return
        sink.close()

    def test_close_writes_buffered_events_when_no_run_log(self, tmp_path: Path):
        """When set_run_log() was not called, close() writes buffered events."""
        from kairos.logger import FileSink, LogEvent

        sink = FileSink(base_dir=str(tmp_path))
        sink.set_run_context("wf", "run-3")

        event = LogEvent(
            timestamp=datetime.now(tz=UTC),
            event_type="step_start",
            step_id="s1",
            data={"attempt": 1},
            level=LogLevel.INFO,
        )
        sink.emit(event)
        # Do NOT call set_run_log — force the fallback path
        sink.close()

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        import json

        with open(files[0]) as f:
            data = json.load(f)
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "step_start"


class TestRunLoggerWorkflowCompleteEdgeCases:
    """Cover on_workflow_complete guard and FileSink integration path."""

    def test_workflow_complete_before_start_is_noop(self):
        """on_workflow_complete does nothing when no workflow has started."""
        from kairos.logger import RunLogger

        logger = RunLogger(sinks=[])
        result = WorkflowResult(
            status=WorkflowStatus.COMPLETE,
            step_results={},
            final_state={},
            duration_ms=0.0,
            timestamp=datetime.now(tz=UTC),
            llm_calls=0,
        )
        # Should silently return — _run_log is None
        logger.on_workflow_complete(result)
        assert logger.get_run_log() is None

    def test_file_sink_receives_run_log_via_workflow_complete(
        self, tmp_path: Path, simple_graph: TaskGraph, simple_result: WorkflowResult
    ):
        """FileSink.set_run_log() is called during on_workflow_complete, writing
        the complete RunLog JSON on close()."""
        from kairos.logger import FileSink, RunLogger

        sink = FileSink(base_dir=str(tmp_path))
        logger = RunLogger(sinks=[sink], verbosity=LogVerbosity.MINIMAL)
        logger.on_workflow_start(simple_graph)
        logger.on_workflow_complete(simple_result)

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        import json

        with open(files[0]) as f:
            data = json.load(f)
        # Should be the full RunLog, not just buffered events
        assert "run_id" in data
        assert "summary" in data
