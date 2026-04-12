"""Tests for kairos.exceptions — written BEFORE implementation."""

import pytest

# --- Group 1: Hierarchy ---


class TestExceptionHierarchy:
    def test_kairos_error_is_exception(self):
        from kairos.exceptions import KairosError

        assert issubclass(KairosError, Exception)

    def test_all_errors_inherit_from_kairos_error(self):
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

        for cls in [
            PlanError,
            ExecutionError,
            ValidationError,
            StateError,
            PolicyError,
            SecurityError,
            ConfigError,
        ]:
            assert issubclass(cls, KairosError), f"{cls.__name__} must inherit KairosError"

    def test_all_errors_are_catchable_as_kairos_error(self):
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

        for cls in [
            PlanError,
            ExecutionError,
            ValidationError,
            StateError,
            PolicyError,
            SecurityError,
            ConfigError,
        ]:
            with pytest.raises(KairosError):
                raise cls("test")


# --- Group 2: Instantiation and attributes ---


class TestKairosError:
    def test_default_message(self):
        from kairos.exceptions import KairosError

        err = KairosError()
        assert err.message == ""
        assert str(err) == ""

    def test_custom_message(self):
        from kairos.exceptions import KairosError

        err = KairosError("something went wrong")
        assert err.message == "something went wrong"
        assert str(err) == "something went wrong"


class TestPlanError:
    def test_default_attributes(self):
        from kairos.exceptions import PlanError

        err = PlanError("bad plan")
        assert err.message == "bad plan"
        assert err.step_id is None

    def test_step_id_attribute(self):
        from kairos.exceptions import PlanError

        err = PlanError("cycle detected", step_id="step_1")
        assert err.step_id == "step_1"
        assert err.message == "cycle detected"


class TestExecutionError:
    def test_default_attributes(self):
        from kairos.exceptions import ExecutionError

        err = ExecutionError("failed")
        assert err.message == "failed"
        assert err.step_id is None
        assert err.attempt is None

    def test_all_attributes(self):
        from kairos.exceptions import ExecutionError

        err = ExecutionError("timeout", step_id="fetch", attempt=3)
        assert err.step_id == "fetch"
        assert err.attempt == 3
        assert err.message == "timeout"


class TestValidationError:
    def test_default_attributes(self):
        from kairos.exceptions import ValidationError

        err = ValidationError("invalid")
        assert err.message == "invalid"
        assert err.step_id is None
        assert err.field is None

    def test_all_attributes(self):
        from kairos.exceptions import ValidationError

        err = ValidationError("type mismatch", step_id="analyze", field="score")
        assert err.step_id == "analyze"
        assert err.field == "score"


class TestStateError:
    def test_default_attributes(self):
        from kairos.exceptions import StateError

        err = StateError("missing key")
        assert err.message == "missing key"
        assert err.key is None

    def test_key_attribute(self):
        from kairos.exceptions import StateError

        err = StateError("key not found", key="api_response")
        assert err.key == "api_response"


class TestPolicyError:
    def test_instantiation(self):
        from kairos.exceptions import PolicyError

        err = PolicyError("invalid policy")
        assert err.message == "invalid policy"
        assert str(err) == "invalid policy"


class TestSecurityError:
    def test_instantiation(self):
        from kairos.exceptions import SecurityError

        err = SecurityError("path traversal detected")
        assert err.message == "path traversal detected"
        assert str(err) == "path traversal detected"


class TestConfigError:
    def test_instantiation(self):
        from kairos.exceptions import ConfigError

        err = ConfigError("missing API key")
        assert err.message == "missing API key"
        assert str(err) == "missing API key"


# --- Group 3: Exception chaining ---


class TestExceptionChaining:
    def test_can_chain_with_cause(self):
        from kairos.exceptions import ExecutionError

        original = ValueError("original error")
        err = ExecutionError("wrapped")
        err.__cause__ = original
        assert err.__cause__ is original

    def test_raise_from_preserves_chain(self):
        from kairos.exceptions import ExecutionError

        with pytest.raises(ExecutionError) as exc_info:
            try:
                raise ValueError("root cause")
            except ValueError as e:
                raise ExecutionError("step failed", step_id="s1") from e

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert exc_info.value.step_id == "s1"


# --- Group 4: Total count ---


class TestExceptionCount:
    def test_eight_exception_classes(self):
        from kairos import exceptions

        exc_classes = [
            v
            for v in vars(exceptions).values()
            if isinstance(v, type)
            and issubclass(v, Exception)
            and v.__module__ == exceptions.__name__
        ]
        assert len(exc_classes) == 8
