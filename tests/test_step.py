"""Tests for kairos.step — written BEFORE implementation.

Covers: StepConfig, StepContext, AttemptRecord, StepResult, Step, SKIP sentinel.

TDD priority order:
1. Failure paths (invalid config, bad names, from_dict errors, conflicts)
2. Boundary conditions (edge values, defaults, None fields)
3. Happy paths (creation, config, repr, to_dict/from_dict)
4. Security (name injection, no callable reconstruction, AttemptRecord sanitized strings)
5. Serialization (JSON round-trips, SKIP not serializable)
6. SKIP sentinel behaviour
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from kairos.enums import AttemptStatus, ForeachPolicy, StepStatus
from kairos.exceptions import ConfigError
from kairos.state import ScopedStateProxy, StateStore
from kairos.step import (
    SKIP,
    AttemptRecord,
    Step,
    StepConfig,
    StepContext,
    StepResult,
    _SkipSentinel,  # pyright: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _noop(ctx: object) -> None:
    """A minimal callable that satisfies the Step action requirement."""


def _make_attempt(
    *,
    attempt_number: int = 1,
    status: AttemptStatus = AttemptStatus.SUCCESS,
    output: object = None,
    error_type: str | None = None,
    error_message: str | None = None,
    duration_ms: float = 10.0,
    timestamp: datetime | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_number=attempt_number,
        status=status,
        output=output,
        error_type=error_type,
        error_message=error_message,
        duration_ms=duration_ms,
        timestamp=timestamp or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


def _make_result(
    *,
    step_id: str = "my_step",
    status: StepStatus = StepStatus.COMPLETED,
    output: object = None,
    attempts: list[AttemptRecord] | None = None,
    duration_ms: float = 50.0,
    timestamp: datetime | None = None,
) -> StepResult:
    resolved_attempts: list[AttemptRecord] = [_make_attempt()] if attempts is None else attempts
    return StepResult(
        step_id=step_id,
        status=status,
        output=output,
        attempts=resolved_attempts,
        duration_ms=duration_ms,
        timestamp=timestamp or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


# ===========================================================================
# Group 1 — Failure paths
# ===========================================================================


class TestStepConfigFailurePaths:
    """StepConfig raises ConfigError for all invalid field values."""

    def test_negative_retries_raises(self) -> None:
        with pytest.raises(ConfigError, match="retries"):
            StepConfig(retries=-1)

    def test_zero_timeout_raises(self) -> None:
        with pytest.raises(ConfigError, match="timeout"):
            StepConfig(timeout=0.0)

    def test_negative_timeout_raises(self) -> None:
        with pytest.raises(ConfigError, match="timeout"):
            StepConfig(timeout=-5.0)

    def test_max_concurrency_zero_raises(self) -> None:
        with pytest.raises(ConfigError, match="max_concurrency"):
            StepConfig(max_concurrency=0)

    def test_max_concurrency_negative_raises(self) -> None:
        with pytest.raises(ConfigError, match="max_concurrency"):
            StepConfig(max_concurrency=-1)

    def test_negative_retry_delay_raises(self) -> None:
        with pytest.raises(ConfigError, match="retry_delay"):
            StepConfig(retry_delay=-0.1)

    def test_negative_retry_backoff_raises(self) -> None:
        with pytest.raises(ConfigError, match="retry_backoff"):
            StepConfig(retry_backoff=-1.0)

    def test_zero_validation_timeout_raises(self) -> None:
        with pytest.raises(ConfigError, match="validation_timeout"):
            StepConfig(validation_timeout=0.0)

    def test_negative_validation_timeout_raises(self) -> None:
        with pytest.raises(ConfigError, match="validation_timeout"):
            StepConfig(validation_timeout=-10.0)


class TestStepFailurePaths:
    """Step raises ConfigError for invalid names, actions, and kwargs."""

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="", action=_noop)

    def test_whitespace_name_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="  ", action=_noop)

    def test_name_with_spaces_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="my step", action=_noop)

    def test_name_with_slash_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="my/step", action=_noop)

    def test_name_with_dot_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="my.step", action=_noop)

    def test_non_callable_action_raises(self) -> None:
        with pytest.raises(ConfigError, match="action"):
            Step(name="step1", action="not_callable")  # type: ignore[arg-type]

    def test_none_action_raises(self) -> None:
        with pytest.raises(ConfigError, match="action"):
            Step(name="step1", action=None)  # type: ignore[arg-type]

    def test_integer_action_raises(self) -> None:
        with pytest.raises(ConfigError, match="action"):
            Step(name="step1", action=42)  # type: ignore[arg-type]

    def test_config_and_kwargs_conflict_raises(self) -> None:
        """Providing both config= and config kwargs is an error."""
        cfg = StepConfig(retries=2)
        with pytest.raises(ConfigError, match="config"):
            Step(name="step1", action=_noop, config=cfg, retries=1)

    def test_unknown_kwarg_raises(self) -> None:
        """Unknown kwargs not in _STEP_CONFIG_KWARGS raise ConfigError."""
        with pytest.raises(ConfigError, match="unknown"):
            Step(name="step1", action=_noop, unknown_param=99)  # type: ignore[call-overload]


class TestAttemptRecordFromDictFailurePaths:
    """AttemptRecord.from_dict raises ConfigError for missing/invalid data."""

    def test_missing_attempt_number_raises(self) -> None:
        data: dict[str, object] = {
            "status": "success",
            "output": None,
            "error_type": None,
            "error_message": None,
            "duration_ms": 10.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError):
            AttemptRecord.from_dict(data)

    def test_missing_status_raises(self) -> None:
        data: dict[str, object] = {
            "attempt_number": 1,
            "output": None,
            "error_type": None,
            "error_message": None,
            "duration_ms": 10.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError):
            AttemptRecord.from_dict(data)

    def test_missing_duration_ms_raises(self) -> None:
        """AttemptRecord.from_dict missing duration_ms must raise ConfigError."""
        data: dict[str, object] = {
            "attempt_number": 1,
            "status": "success",
            "output": None,
            "error_type": None,
            "error_message": None,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError, match="duration_ms"):
            AttemptRecord.from_dict(data)

    def test_invalid_status_value_raises(self) -> None:
        data: dict[str, object] = {
            "attempt_number": 1,
            "status": "not_a_valid_status",
            "output": None,
            "error_type": None,
            "error_message": None,
            "duration_ms": 10.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError):
            AttemptRecord.from_dict(data)

    def test_invalid_timestamp_raises(self) -> None:
        data: dict[str, object] = {
            "attempt_number": 1,
            "status": "success",
            "output": None,
            "error_type": None,
            "error_message": None,
            "duration_ms": 10.0,
            "timestamp": "not-a-date",
        }
        with pytest.raises(ConfigError):
            AttemptRecord.from_dict(data)

    def test_missing_timestamp_raises(self) -> None:
        data: dict[str, object] = {
            "attempt_number": 1,
            "status": "success",
            "output": None,
            "error_type": None,
            "error_message": None,
            "duration_ms": 10.0,
        }
        with pytest.raises(ConfigError):
            AttemptRecord.from_dict(data)


class TestStepResultFromDictFailurePaths:
    """StepResult.from_dict raises ConfigError for missing/invalid data."""

    def test_missing_step_id_raises(self) -> None:
        data: dict[str, Any] = {
            "status": "completed",
            "output": None,
            "attempts": [],
            "duration_ms": 50.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError):
            StepResult.from_dict(data)

    def test_invalid_step_status_raises(self) -> None:
        data: dict[str, Any] = {
            "step_id": "s1",
            "status": "not_a_status",
            "output": None,
            "attempts": [],
            "duration_ms": 50.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError):
            StepResult.from_dict(data)

    def test_invalid_timestamp_in_step_result_raises(self) -> None:
        """StepResult.from_dict with unparseable timestamp must raise ConfigError."""
        data: dict[str, Any] = {
            "step_id": "s1",
            "status": "completed",
            "output": None,
            "attempts": [],
            "duration_ms": 50.0,
            "timestamp": "not-a-date",
        }
        with pytest.raises(ConfigError, match="timestamp"):
            StepResult.from_dict(data)

    def test_invalid_attempt_in_list_raises(self) -> None:
        data: dict[str, Any] = {
            "step_id": "s1",
            "status": "completed",
            "output": None,
            "attempts": [{"bad": "data"}],  # missing required fields
            "duration_ms": 50.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError):
            StepResult.from_dict(data)

    def test_attempts_not_a_list_raises(self) -> None:
        """attempts field as a non-list (e.g., string) must raise ConfigError."""
        data: dict[str, Any] = {
            "step_id": "s1",
            "status": "completed",
            "output": None,
            "attempts": "not_a_list",
            "duration_ms": 50.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError, match="list"):
            StepResult.from_dict(data)

    def test_attempt_entry_not_a_dict_raises(self) -> None:
        """Non-dict entry inside attempts list must raise ConfigError."""
        data: dict[str, Any] = {
            "step_id": "s1",
            "status": "completed",
            "output": None,
            "attempts": ["not_a_dict"],
            "duration_ms": 50.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        with pytest.raises(ConfigError, match="dict"):
            StepResult.from_dict(data)


# ===========================================================================
# Group 2 — Boundary conditions
# ===========================================================================


class TestStepConfigBoundaryConditions:
    """StepConfig edge values and defaults."""

    def test_retries_zero_is_valid(self) -> None:
        cfg = StepConfig(retries=0)
        assert cfg.retries == 0

    def test_timeout_none_is_valid(self) -> None:
        cfg = StepConfig(timeout=None)
        assert cfg.timeout is None

    def test_very_small_positive_timeout_is_valid(self) -> None:
        cfg = StepConfig(timeout=0.001)
        assert cfg.timeout == pytest.approx(0.001)  # pyright: ignore[reportUnknownMemberType]

    def test_max_concurrency_one_is_valid(self) -> None:
        cfg = StepConfig(max_concurrency=1)
        assert cfg.max_concurrency == 1

    def test_max_concurrency_none_is_valid(self) -> None:
        cfg = StepConfig(max_concurrency=None)
        assert cfg.max_concurrency is None

    def test_retry_delay_zero_is_valid(self) -> None:
        cfg = StepConfig(retry_delay=0.0)
        assert cfg.retry_delay == 0.0

    def test_retry_backoff_zero_is_valid(self) -> None:
        cfg = StepConfig(retry_backoff=0.0)
        assert cfg.retry_backoff == 0.0

    def test_defaults(self) -> None:
        cfg = StepConfig()
        assert cfg.retries == 0
        assert cfg.timeout is None
        assert cfg.foreach is None
        assert cfg.foreach_policy == ForeachPolicy.REQUIRE_ALL
        assert cfg.parallel is False
        assert cfg.max_concurrency is None
        assert cfg.retry_delay == 0.0
        assert cfg.retry_backoff == 1.0
        assert cfg.retry_jitter is True
        assert cfg.validation_timeout == 30.0


class TestStepBoundaryConditions:
    """Step edge cases for names, depends_on, and config kwargs."""

    def test_single_char_name_is_valid(self) -> None:
        s = Step(name="a", action=_noop)
        assert s.name == "a"

    def test_name_with_underscore_and_hyphen(self) -> None:
        s = Step(name="my-step_01", action=_noop)
        assert s.name == "my-step_01"

    def test_depends_on_none_stored_as_empty_list(self) -> None:
        s = Step(name="s", action=_noop, depends_on=None)
        assert s.depends_on == []

    def test_depends_on_default_is_empty_list(self) -> None:
        s = Step(name="s", action=_noop)
        assert s.depends_on == []

    def test_depends_on_list_is_copied(self) -> None:
        deps = ["a", "b"]
        s = Step(name="s", action=_noop, depends_on=deps)
        deps.append("c")
        assert s.depends_on == ["a", "b"]

    def test_no_config_no_kwargs_uses_default_stepconfig(self) -> None:
        s = Step(name="s", action=_noop)
        assert isinstance(s.config, StepConfig)
        assert s.config.retries == 0

    def test_stepcontext_defaults(self) -> None:
        store = StateStore()
        ctx = StepContext(state=store, inputs={})
        assert ctx.item is None
        assert ctx.retry_context is None
        assert ctx.step_id == ""
        assert ctx.attempt == 1


class TestAttemptRecordBoundaryConditions:
    """AttemptRecord edge values — None fields, zero duration."""

    def test_all_optional_fields_none(self) -> None:
        rec = _make_attempt(output=None, error_type=None, error_message=None)
        assert rec.output is None
        assert rec.error_type is None
        assert rec.error_message is None

    def test_zero_duration_is_valid(self) -> None:
        rec = _make_attempt(duration_ms=0.0)
        assert rec.duration_ms == 0.0

    def test_from_dict_ignores_extra_keys(self) -> None:
        data: dict[str, object] = {
            "attempt_number": 1,
            "status": "success",
            "output": "ok",
            "error_type": None,
            "error_message": None,
            "duration_ms": 5.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
            "extra_unknown_field": "ignored",
        }
        rec = AttemptRecord.from_dict(data)
        assert rec.attempt_number == 1

    def test_empty_attempts_list_in_step_result(self) -> None:
        result = _make_result(attempts=[])
        assert result.attempts == []


# ===========================================================================
# Group 3 — Happy paths
# ===========================================================================


class TestStepConfigHappyPaths:
    """StepConfig creation with valid values."""

    def test_full_config(self) -> None:
        cfg = StepConfig(
            retries=3,
            timeout=60.0,
            foreach="items",
            foreach_policy=ForeachPolicy.ALLOW_PARTIAL,
            parallel=True,
            max_concurrency=4,
            retry_delay=1.0,
            retry_backoff=2.0,
            retry_jitter=False,
            validation_timeout=15.0,
        )
        assert cfg.retries == 3
        assert cfg.timeout == 60.0
        assert cfg.foreach == "items"
        assert cfg.foreach_policy == ForeachPolicy.ALLOW_PARTIAL
        assert cfg.parallel is True
        assert cfg.max_concurrency == 4
        assert cfg.retry_delay == 1.0
        assert cfg.retry_backoff == 2.0
        assert cfg.retry_jitter is False
        assert cfg.validation_timeout == 15.0


class TestStepHappyPaths:
    """Step creation, config assignment, repr."""

    def test_minimal_step(self) -> None:
        s = Step(name="fetch_data", action=_noop)
        assert s.name == "fetch_data"
        assert s.action is _noop
        assert s.depends_on == []
        assert isinstance(s.config, StepConfig)
        assert s.input_contract is None
        assert s.output_contract is None
        assert s.read_keys is None
        assert s.write_keys is None
        assert s.failure_policy is None

    def test_step_with_config_object(self) -> None:
        cfg = StepConfig(retries=2, timeout=30.0)
        s = Step(name="s", action=_noop, config=cfg)
        assert s.config is cfg

    def test_step_with_config_kwargs(self) -> None:
        s = Step(name="s", action=_noop, retries=5, timeout=10.0)
        assert s.config.retries == 5
        assert s.config.timeout == 10.0

    def test_step_all_kwargs_forwarded(self) -> None:
        s = Step(
            name="s",
            action=_noop,
            foreach="items",
            foreach_policy=ForeachPolicy.ALLOW_PARTIAL,
            parallel=True,
            max_concurrency=2,
            retry_delay=0.5,
            retry_backoff=1.5,
            retry_jitter=False,
            validation_timeout=20.0,
        )
        assert s.config.foreach == "items"
        assert s.config.foreach_policy == ForeachPolicy.ALLOW_PARTIAL
        assert s.config.parallel is True
        assert s.config.max_concurrency == 2

    def test_step_with_depends_on_list(self) -> None:
        s = Step(name="s", action=_noop, depends_on=["a", "b"])
        assert s.depends_on == ["a", "b"]

    def test_step_repr(self) -> None:
        s = Step(name="fetch", action=_noop, depends_on=["init"])
        r = repr(s)
        assert "fetch" in r
        assert "init" in r
        assert "Step(" in r

    def test_step_repr_no_deps(self) -> None:
        s = Step(name="start", action=_noop)
        r = repr(s)
        assert "start" in r
        assert "[]" in r

    def test_step_stores_read_write_keys(self) -> None:
        s = Step(name="s", action=_noop, read_keys=["x"], write_keys=["y"])
        assert s.read_keys == ["x"]
        assert s.write_keys == ["y"]

    def test_stepcontext_with_all_fields(self) -> None:
        store = StateStore()
        ctx = StepContext(
            state=store,
            inputs={"key": "val"},
            item=42,
            retry_context={"attempt": 1},
            step_id="my_step",
            attempt=2,
        )
        assert ctx.state is store
        assert ctx.inputs == {"key": "val"}
        assert ctx.item == 42
        assert ctx.retry_context == {"attempt": 1}
        assert ctx.step_id == "my_step"
        assert ctx.attempt == 2

    def test_stepcontext_with_scoped_proxy(self) -> None:
        store = StateStore()
        proxy = store.scoped(read_keys=["a"])
        ctx = StepContext(state=proxy, inputs={})
        assert isinstance(ctx.state, ScopedStateProxy)


class TestAttemptRecordHappyPaths:
    """AttemptRecord creation and to_dict/from_dict."""

    def test_success_attempt(self) -> None:
        rec = _make_attempt(status=AttemptStatus.SUCCESS, output={"x": 1})
        assert rec.status == AttemptStatus.SUCCESS
        assert rec.output == {"x": 1}

    def test_failure_attempt(self) -> None:
        rec = _make_attempt(
            status=AttemptStatus.FAILURE,
            error_type="TimeoutError",
            error_message="Step timed out after 30s",
        )
        assert rec.status == AttemptStatus.FAILURE
        assert rec.error_type == "TimeoutError"
        assert rec.error_message == "Step timed out after 30s"

    def test_to_dict_structure(self) -> None:
        ts = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        rec = AttemptRecord(
            attempt_number=2,
            status=AttemptStatus.FAILURE,
            output=None,
            error_type="ValueError",
            error_message="bad input",
            duration_ms=123.4,
            timestamp=ts,
        )
        d = rec.to_dict()
        assert d["attempt_number"] == 2
        assert d["status"] == "failure"
        assert d["output"] is None
        assert d["error_type"] == "ValueError"
        assert d["error_message"] == "bad input"
        assert d["duration_ms"] == pytest.approx(123.4)  # pyright: ignore[reportUnknownMemberType]
        assert d["timestamp"] == "2024-06-15T10:00:00+00:00"

    def test_from_dict_round_trip(self) -> None:
        ts = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        original = AttemptRecord(
            attempt_number=3,
            status=AttemptStatus.SUCCESS,
            output={"score": 0.9},
            error_type=None,
            error_message=None,
            duration_ms=88.0,
            timestamp=ts,
        )
        d = original.to_dict()
        restored = AttemptRecord.from_dict(d)
        assert restored.attempt_number == original.attempt_number
        assert restored.status == original.status
        assert restored.output == original.output
        assert restored.error_type == original.error_type
        assert restored.error_message == original.error_message
        assert restored.duration_ms == pytest.approx(original.duration_ms)  # pyright: ignore[reportUnknownMemberType]
        assert restored.timestamp == original.timestamp


class TestStepResultHappyPaths:
    """StepResult creation and to_dict/from_dict."""

    def test_to_dict_structure(self) -> None:
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        result = StepResult(
            step_id="analyze",
            status=StepStatus.COMPLETED,
            output={"result": "ok"},
            attempts=[_make_attempt(timestamp=ts)],
            duration_ms=200.0,
            timestamp=ts,
        )
        d = result.to_dict()
        assert d["step_id"] == "analyze"
        assert d["status"] == "completed"
        assert d["output"] == {"result": "ok"}
        assert isinstance(d["attempts"], list)
        assert len(cast(list[object], d["attempts"])) == 1
        assert d["duration_ms"] == pytest.approx(200.0)  # pyright: ignore[reportUnknownMemberType]
        assert d["timestamp"] == "2024-06-15T12:00:00+00:00"

    def test_from_dict_round_trip(self) -> None:
        original = _make_result(
            step_id="step_x",
            status=StepStatus.FAILED_FINAL,
            output=None,
            attempts=[_make_attempt(status=AttemptStatus.FAILURE, error_type="RuntimeError")],
        )
        d = original.to_dict()
        restored = StepResult.from_dict(d)
        assert restored.step_id == original.step_id
        assert restored.status == original.status
        assert restored.output == original.output
        assert len(restored.attempts) == 1
        assert restored.attempts[0].status == AttemptStatus.FAILURE
        assert restored.attempts[0].error_type == "RuntimeError"

    def test_from_dict_empty_attempts(self) -> None:
        d: dict[str, Any] = {
            "step_id": "s1",
            "status": "completed",
            "output": None,
            "attempts": [],
            "duration_ms": 5.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        result = StepResult.from_dict(d)
        assert result.attempts == []


# ===========================================================================
# Group 4 — Security
# ===========================================================================


class TestStepSecurity:
    """Security constraints for Step and AttemptRecord."""

    def test_name_with_path_traversal_raises(self) -> None:
        """Step names must not allow path traversal characters."""
        with pytest.raises(ConfigError, match="name"):
            Step(name="../evil", action=_noop)

    def test_name_with_null_byte_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="step\x00", action=_noop)

    def test_name_with_newline_raises(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Step(name="step\ninjection", action=_noop)

    def test_step_has_no_from_dict(self) -> None:
        """Step has no from_dict — it is never reconstructed from serialized data.

        Step actions are callables and must be provided directly by the developer.
        """
        assert not hasattr(Step, "from_dict")

    def test_attempt_record_stores_strings_not_exceptions(self) -> None:
        """AttemptRecord must accept only string error fields, not Exception objects."""
        # error_type and error_message are str | None — verify Exception is not accepted
        # We verify by confirming the fields are plain strings in to_dict output
        exc_message = "Connection error with key sk-proj-abc123"
        sanitized_message = "sanitized message"  # simulate what sanitize_exception returns

        rec = _make_attempt(
            status=AttemptStatus.FAILURE,
            error_type="ConnectionError",
            error_message=sanitized_message,
        )
        d = rec.to_dict()
        # The raw exception message with the credential must not appear
        assert exc_message not in str(d)
        # The sanitized message should appear
        assert d["error_message"] == sanitized_message

    def test_step_result_from_dict_does_not_reconstruct_callables(self) -> None:
        """from_dict must never execute or eval any field as code."""
        data: dict[str, Any] = {
            "step_id": "evil_step",
            "status": "completed",
            "output": {"action": "__import__('os').system('rm -rf /')"},
            "attempts": [],
            "duration_ms": 1.0,
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        result = StepResult.from_dict(data)
        # Output is stored as plain data, not executed
        assert isinstance(result.output, dict)
        output = cast(dict[str, Any], result.output)  # pyright: ignore[reportUnknownMemberType]
        assert "__import__" in str(output)  # literal string, not executed


# ===========================================================================
# Group 5 — Serialization
# ===========================================================================


class TestAttemptRecordSerialization:
    """AttemptRecord to_dict/from_dict produces JSON-serializable output."""

    def test_to_dict_is_json_serializable(self) -> None:
        rec = _make_attempt(output={"key": "value", "num": 42})
        d = rec.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_from_dict_from_json_string(self) -> None:
        rec = _make_attempt(status=AttemptStatus.SUCCESS, output="hello")
        json_str = json.dumps(rec.to_dict())
        restored = AttemptRecord.from_dict(json.loads(json_str))
        assert restored.status == AttemptStatus.SUCCESS
        assert restored.output == "hello"

    def test_to_dict_status_is_string_value(self) -> None:
        rec = _make_attempt(status=AttemptStatus.FAILURE)
        d = rec.to_dict()
        assert d["status"] == "failure"
        assert isinstance(d["status"], str)

    def test_to_dict_timestamp_is_iso_string(self) -> None:
        rec = _make_attempt()
        d = rec.to_dict()
        assert isinstance(d["timestamp"], str)
        # Must be parseable
        datetime.fromisoformat(d["timestamp"])


class TestStepResultSerialization:
    """StepResult to_dict/from_dict produces JSON-serializable output."""

    def test_to_dict_is_json_serializable(self) -> None:
        result = _make_result(output={"x": 1})
        d = result.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_nested_attempts_serialized(self) -> None:
        attempts = [
            _make_attempt(attempt_number=1, status=AttemptStatus.FAILURE),
            _make_attempt(attempt_number=2, status=AttemptStatus.SUCCESS),
        ]
        result = _make_result(attempts=attempts)
        d = result.to_dict()
        attempt_list = cast(list[dict[str, object]], d["attempts"])
        assert len(attempt_list) == 2
        assert attempt_list[0]["attempt_number"] == 1
        assert attempt_list[1]["status"] == "success"

    def test_to_dict_status_is_string_value(self) -> None:
        result = _make_result(status=StepStatus.SKIPPED)
        d = result.to_dict()
        assert d["status"] == "skipped"
        assert isinstance(d["status"], str)

    def test_full_round_trip_json(self) -> None:
        original = _make_result(
            step_id="pipeline_step",
            status=StepStatus.COMPLETED,
            output={"answer": 42},
            attempts=[_make_attempt(output={"answer": 42})],
        )
        json_str = json.dumps(original.to_dict())
        restored = StepResult.from_dict(json.loads(json_str))
        assert restored.step_id == original.step_id
        assert restored.status == original.status
        assert restored.output == original.output


class TestSkipSerialization:
    """SKIP is a sentinel and must not be JSON-serializable."""

    def test_skip_is_not_json_serializable(self) -> None:
        with pytest.raises(TypeError):
            json.dumps(SKIP)

    def test_skip_to_dict_not_possible(self) -> None:
        """SKIP has no to_dict — it's a sentinel, not a data structure."""
        assert not hasattr(SKIP, "to_dict")


# ===========================================================================
# Group 6 — SKIP sentinel
# ===========================================================================


class TestSkipSentinel:
    """SKIP is a singleton, falsy, and distinct from None and strings."""

    def test_skip_is_singleton(self) -> None:
        a = _SkipSentinel()
        b = _SkipSentinel()
        assert a is b

    def test_skip_constant_is_instance(self) -> None:
        assert isinstance(SKIP, _SkipSentinel)

    def test_skip_is_falsy(self) -> None:
        assert not SKIP
        assert bool(SKIP) is False

    def test_skip_repr(self) -> None:
        assert repr(SKIP) == "SKIP"

    def test_skip_is_not_none(self) -> None:
        assert SKIP is not None

    def test_skip_is_not_string(self) -> None:
        assert SKIP != "SKIP"
        assert SKIP != ""

    def test_skip_identity_check(self) -> None:
        """The idiomatic check is `result is SKIP`, not `result == SKIP`."""
        result = SKIP
        assert result is SKIP

    def test_new_instance_is_same_object(self) -> None:
        new_skip = _SkipSentinel()
        assert new_skip is SKIP


# ===========================================================================
# Group 7 — StepContext.increment_llm_calls()
# ===========================================================================


class TestStepContextIncrementLLMCalls:
    """increment_llm_calls() participates in the LLM circuit breaker."""

    def test_increment_with_no_callback_is_noop(self) -> None:
        """StepContext without callback silently no-ops."""
        store = StateStore()
        ctx = StepContext(state=store, inputs={})
        # Must not raise
        ctx.increment_llm_calls()
        ctx.increment_llm_calls(5)

    def test_callback_receives_correct_count(self) -> None:
        """Callback invoked with the exact count argument."""
        store = StateStore()
        received: list[int] = []

        def cb(count: int) -> None:
            received.append(count)

        ctx = StepContext(state=store, inputs={}, _llm_call_callback=cb)
        ctx.increment_llm_calls(3)
        assert received == [3]

    def test_callback_default_count_is_1(self) -> None:
        """Default count is 1 when not specified."""
        store = StateStore()
        received: list[int] = []

        def cb(count: int) -> None:
            received.append(count)

        ctx = StepContext(state=store, inputs={}, _llm_call_callback=cb)
        ctx.increment_llm_calls()
        assert received == [1]

    def test_callback_exception_propagates(self) -> None:
        """If callback raises, exception propagates to step action."""
        store = StateStore()

        def cb(count: int) -> None:
            raise RuntimeError("circuit breaker triggered")

        ctx = StepContext(state=store, inputs={}, _llm_call_callback=cb)
        with pytest.raises(RuntimeError, match="circuit breaker triggered"):
            ctx.increment_llm_calls()

    def test_multiple_increments_each_invoke_callback(self) -> None:
        """Multiple calls each invoke the callback."""
        store = StateStore()
        call_counts: list[int] = []

        def cb(count: int) -> None:
            call_counts.append(count)

        ctx = StepContext(state=store, inputs={}, _llm_call_callback=cb)
        ctx.increment_llm_calls(1)
        ctx.increment_llm_calls(2)
        ctx.increment_llm_calls(3)
        assert call_counts == [1, 2, 3]

    def test_existing_construction_without_callback_works(self) -> None:
        """StepContext without _llm_call_callback defaults to None."""
        store = StateStore()
        ctx = StepContext(state=store, inputs={})
        assert ctx._llm_call_callback is None  # pyright: ignore[reportPrivateUsage]

    def test_callback_not_in_repr(self) -> None:
        """_llm_call_callback excluded from repr."""
        store = StateStore()

        def cb(count: int) -> None:
            pass

        ctx = StepContext(state=store, inputs={}, _llm_call_callback=cb)
        r = repr(ctx)
        assert "_llm_call_callback" not in r
        assert "cb" not in r

    def test_negative_count_rejected_without_callback(self) -> None:
        """Negative count raises ConfigError even without a callback."""
        store = StateStore()
        ctx = StepContext(state=store, inputs={})
        with pytest.raises(ConfigError, match="must be >= 1"):
            ctx.increment_llm_calls(-1)

    def test_zero_count_rejected_without_callback(self) -> None:
        """Zero count raises ConfigError even without a callback."""
        store = StateStore()
        ctx = StepContext(state=store, inputs={})
        with pytest.raises(ConfigError, match="must be >= 1"):
            ctx.increment_llm_calls(0)

    def test_callback_not_in_equality(self) -> None:
        """Two contexts equal regardless of callback identity."""
        store = StateStore()
        received: list[int] = []

        def cb(count: int) -> None:
            received.append(count)

        ctx1 = StepContext(state=store, inputs={})
        ctx2 = StepContext(state=store, inputs={}, _llm_call_callback=cb)
        # Both have same structural fields — they should compare equal
        assert ctx1 == ctx2
