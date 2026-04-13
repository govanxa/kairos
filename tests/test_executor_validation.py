"""Tests for validation and failure routing wired into kairos.executor.

Written BEFORE implementation (TDD mandate).

Test priority order:
1. Failure paths — output validation fail → retry/abort/skip, input validation fail,
   validator crash, router retry exhaustion, execution error with router
2. Boundary conditions — no validator = no validation, no router = current behavior,
   no contracts, non-Schema contract, SKIP bypasses output validation, foreach + validation
3. Happy paths — output passes validation, input passes validation, retry succeeds after
   validation fail, both contracts pass
4. Security — retry context from router is sanitized, validator crash does not leak
5. Hooks — validation hooks fire with correct phase, correct attempt number
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kairos import (
    SKIP,
    StateStore,
    Step,
    StepContext,
    StepStatus,
    WorkflowStatus,
)
from kairos.enums import FailureAction
from kairos.executor import ExecutorHooks, StepExecutor
from kairos.failure import FailurePolicy, FailureRouter
from kairos.plan import TaskGraph
from kairos.schema import Schema, ValidationResult
from kairos.validators import StructuralValidator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(ctx: StepContext) -> dict[str, object]:
    return {}


def _return_value(value: object):
    def action(ctx: StepContext) -> object:
        return value

    return action


def _always_fail(ctx: StepContext) -> None:
    raise RuntimeError("deliberate failure")


def _make_graph(steps: list[Step], name: str = "test") -> TaskGraph:
    return TaskGraph(name=name, steps=steps)


def _make_valid_result() -> ValidationResult:
    return ValidationResult(valid=True)


def _make_invalid_result() -> ValidationResult:
    from kairos.enums import Severity
    from kairos.schema import FieldValidationError

    return ValidationResult(
        valid=False,
        errors=[
            FieldValidationError(
                field="score",
                expected="int",
                actual="str",
                message="score must be int",
                severity=Severity.ERROR,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state() -> StateStore:
    return StateStore()


@pytest.fixture
def validator() -> MagicMock:
    """A mock StructuralValidator that passes validation by default."""
    v = MagicMock(spec=StructuralValidator)
    v.validate.return_value = _make_valid_result()
    return v


@pytest.fixture
def failing_validator() -> MagicMock:
    """A mock StructuralValidator that always returns invalid."""
    v = MagicMock(spec=StructuralValidator)
    v.validate.return_value = _make_invalid_result()
    return v


@pytest.fixture
def retry_router() -> FailureRouter:
    """A FailureRouter whose policy retries once then aborts.

    max_retries=2 means: retry while attempt_number < 2 (i.e., attempt 1 → RETRY),
    then fallback on attempt 2 (attempt_number >= max_retries). This gives 2 total
    attempts: 1 initial + 1 retry.
    """
    policy = FailurePolicy(
        on_validation_fail=FailureAction.RETRY,
        on_execution_fail=FailureAction.RETRY,
        max_retries=2,
        retry_delay=0.0,
        fallback_action=FailureAction.ABORT,
    )
    return FailureRouter(workflow_policy=policy)


@pytest.fixture
def abort_router() -> FailureRouter:
    """A FailureRouter that always aborts on validation failure."""
    policy = FailurePolicy(
        on_validation_fail=FailureAction.ABORT,
        on_execution_fail=FailureAction.ABORT,
        max_retries=0,
        retry_delay=0.0,
        fallback_action=FailureAction.ABORT,
    )
    return FailureRouter(workflow_policy=policy)


@pytest.fixture
def skip_router() -> FailureRouter:
    """A FailureRouter that skips on validation failure."""
    policy = FailurePolicy(
        on_validation_fail=FailureAction.SKIP,
        on_execution_fail=FailureAction.SKIP,
        max_retries=0,
        retry_delay=0.0,
        fallback_action=FailureAction.ABORT,
    )
    return FailureRouter(workflow_policy=policy)


# ---------------------------------------------------------------------------
# Group 1: Failure Paths — write FIRST
# ---------------------------------------------------------------------------


class TestOutputValidationFailure:
    """When output validation fails and a router is wired in."""

    def test_output_validation_fail_abort_gives_failed_final(
        self, state: StateStore, failing_validator: MagicMock, abort_router: FailureRouter
    ) -> None:
        """ABORT router action after output validation failure → FAILED_FINAL."""
        schema = Schema({"score": int})
        step = Step("s1", _return_value({"score": "not_an_int"}), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=failing_validator, failure_router=abort_router
        )
        result = executor.run(_make_graph([step]))

        assert result.status == WorkflowStatus.FAILED
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL

    def test_output_validation_fail_skip_gives_skipped(
        self, state: StateStore, failing_validator: MagicMock, skip_router: FailureRouter
    ) -> None:
        """SKIP router action after output validation failure → SKIPPED status."""
        schema = Schema({"score": int})
        step = Step("s1", _return_value({"score": "bad"}), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=failing_validator, failure_router=skip_router
        )
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.SKIPPED

    def test_output_validation_fail_retry_then_success(
        self, state: StateStore, retry_router: FailureRouter
    ) -> None:
        """RETRY action: first attempt output fails validation, second passes."""
        attempts_seen: list[int] = []
        good_schema = Schema({"score": int})

        def action(ctx: StepContext) -> dict[str, object]:
            attempts_seen.append(ctx.attempt)
            if ctx.attempt == 1:
                return {"score": "not_an_int"}  # will fail validation first time
            return {"score": 42}  # passes on retry

        # Validator: fail on attempt 1 (returns "not_an_int"), pass on attempt 2
        real_validator = StructuralValidator()
        step = Step("s1", action, output_contract=good_schema)

        executor = StepExecutor(state=state, validator=real_validator, failure_router=retry_router)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        assert 2 in attempts_seen

    def test_output_validation_fail_router_retry_exhausted_gives_failed_final(
        self, state: StateStore, failing_validator: MagicMock, retry_router: FailureRouter
    ) -> None:
        """When router retries and keeps failing, final status is FAILED_FINAL."""
        schema = Schema({"score": int})
        step = Step("s1", _return_value({"score": "always bad"}), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=failing_validator, failure_router=retry_router
        )
        result = executor.run(_make_graph([step]))

        assert result.status == WorkflowStatus.FAILED
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL


class TestInputValidationFailure:
    """When input validation fails before the step action runs."""

    def test_input_validation_fail_abort_stops_before_action(
        self, state: StateStore, failing_validator: MagicMock, abort_router: FailureRouter
    ) -> None:
        """Input validation fail with ABORT router → FAILED_FINAL, action never called."""
        action_called: list[bool] = []

        def action(ctx: StepContext) -> dict[str, object]:
            action_called.append(True)
            return {}

        input_schema = Schema({"dep": int})
        # dep step provides string, not int
        dep_step = Step("dep", _return_value("not_an_int"))
        step = Step("s1", action, depends_on=["dep"], input_contract=input_schema)

        # The validator needs to fail when validating the resolved inputs
        executor = StepExecutor(
            state=state, validator=failing_validator, failure_router=abort_router
        )
        result = executor.run(_make_graph([dep_step, step]))

        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL
        assert len(action_called) == 0

    def test_input_validation_fail_skip_gives_skipped(
        self, state: StateStore, failing_validator: MagicMock, skip_router: FailureRouter
    ) -> None:
        """Input validation fail with SKIP router → SKIPPED status."""
        input_schema = Schema({"dep": int})
        dep_step = Step("dep", _return_value("not_an_int"))
        step = Step("s1", _noop, depends_on=["dep"], input_contract=input_schema)

        executor = StepExecutor(
            state=state, validator=failing_validator, failure_router=skip_router
        )
        result = executor.run(_make_graph([dep_step, step]))

        assert result.step_results["s1"].status == StepStatus.SKIPPED


class TestExecutionErrorWithRouter:
    """When the step raises an exception and a router is wired in."""

    def test_execution_error_with_abort_router_gives_failed_final(
        self, state: StateStore, abort_router: FailureRouter
    ) -> None:
        """Execution error with ABORT router → FAILED_FINAL."""
        step = Step("s1", _always_fail)

        executor = StepExecutor(state=state, failure_router=abort_router)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL

    def test_execution_error_with_skip_router_gives_skipped(
        self, state: StateStore, skip_router: FailureRouter
    ) -> None:
        """Execution error with SKIP router → SKIPPED status."""
        step = Step("s1", _always_fail)

        executor = StepExecutor(state=state, failure_router=skip_router)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.SKIPPED

    def test_execution_error_with_retry_router_retries_then_succeeds(
        self, state: StateStore, retry_router: FailureRouter
    ) -> None:
        """Execution error + RETRY router: step succeeds on second attempt."""
        attempt_tracker: list[int] = []

        def action(ctx: StepContext) -> dict[str, object]:
            attempt_tracker.append(ctx.attempt)
            if ctx.attempt == 1:
                raise RuntimeError("transient failure")
            return {"ok": True}

        step = Step("s1", action)
        executor = StepExecutor(state=state, failure_router=retry_router)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        assert 2 in attempt_tracker


class TestValidatorCrash:
    """When the validator itself raises an unexpected exception."""

    def test_validator_crash_does_not_propagate(
        self, state: StateStore, abort_router: FailureRouter
    ) -> None:
        """If the validator crashes, the step fails cleanly (no unhandled exception)."""
        crashing_validator = MagicMock(spec=StructuralValidator)
        crashing_validator.validate.side_effect = RuntimeError("validator internal error")

        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=crashing_validator, failure_router=abort_router
        )
        # Must NOT raise — validator crash produces a failed ValidationResult
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status in (
            StepStatus.FAILED_FINAL,
            StepStatus.COMPLETED,  # if crash treated as valid=True, which would be wrong
        )
        # Actually a validator crash should fail the step, not silently pass
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL

    def test_validator_crash_gives_failed_final_not_exception(
        self, state: StateStore, abort_router: FailureRouter
    ) -> None:
        """Validator crash must return a ValidationResult(valid=False), never re-raise."""
        crashing_validator = MagicMock(spec=StructuralValidator)
        crashing_validator.validate.side_effect = ValueError("internal boom")

        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=crashing_validator, failure_router=abort_router
        )
        # Must not raise ValueError
        result = executor.run(_make_graph([step]))
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL


# ---------------------------------------------------------------------------
# Group 2: Boundary Conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge cases for the validation/router integration."""

    def test_no_validator_no_validation_runs(self, state: StateStore) -> None:
        """Without a validator, contracts are silently skipped even if present."""
        schema = Schema({"score": int})
        # Action returns wrong type — but no validator = no validation
        step = Step("s1", _return_value({"score": "wrong_type"}), output_contract=schema)

        executor = StepExecutor(state=state)  # no validator, no router
        result = executor.run(_make_graph([step]))

        # Step succeeds because there's no validator to catch the wrong type
        assert result.step_results["s1"].status == StepStatus.COMPLETED

    def test_no_router_execution_error_uses_stepconfig_retries(self, state: StateStore) -> None:
        """Without a failure router, execution errors fall back to StepConfig.retries."""
        attempts_tracker: list[int] = []

        def action(ctx: StepContext) -> dict[str, object]:
            attempts_tracker.append(ctx.attempt)
            if ctx.attempt < 3:
                raise RuntimeError("fail")
            return {"ok": True}

        # No router — relies on StepConfig.retries = 2
        step = Step("s1", action, retries=2)
        executor = StepExecutor(state=state)  # no failure_router
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        assert len(attempts_tracker) == 3

    def test_no_contract_skips_validation(self, state: StateStore, validator: MagicMock) -> None:
        """When a step has no contracts, validate is never called."""
        step = Step("s1", _return_value({"x": 1}))  # no contracts

        executor = StepExecutor(state=state, validator=validator)
        executor.run(_make_graph([step]))

        validator.validate.assert_not_called()

    def test_non_schema_contract_skips_validation(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """When output_contract is not a Schema instance, validation is skipped."""
        step = Step("s1", _return_value({"x": 1}), output_contract={"x": int})

        executor = StepExecutor(state=state, validator=validator)
        result = executor.run(_make_graph([step]))

        validator.validate.assert_not_called()
        assert result.step_results["s1"].status == StepStatus.COMPLETED

    def test_skip_sentinel_bypasses_output_validation(
        self, state: StateStore, failing_validator: MagicMock, abort_router: FailureRouter
    ) -> None:
        """SKIP sentinel must bypass output validation — no validation hook fires."""
        schema = Schema({"score": int})
        step = Step("s1", _return_value(SKIP), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=failing_validator, failure_router=abort_router
        )
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.SKIPPED
        failing_validator.validate.assert_not_called()

    def test_foreach_output_validation_runs_per_item(self, state: StateStore) -> None:
        """In foreach, output_contract validation runs for each item's output."""
        state.set("items", [{"x": 1}, {"x": 2}])

        call_count: list[int] = [0]

        def action(ctx: StepContext) -> dict[str, object]:
            call_count[0] += 1
            return ctx.item  # type: ignore[return-value]

        real_validator = StructuralValidator()
        schema = Schema({"x": int})
        step = Step("s1", action, foreach="items", output_contract=schema)

        executor = StepExecutor(state=state, validator=real_validator)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        assert call_count[0] == 2

    def test_validator_wired_without_router_validation_fail_uses_stepconfig(
        self, state: StateStore, failing_validator: MagicMock
    ) -> None:
        """Validator wired but no router: validation fail uses StepConfig retry logic."""
        schema = Schema({"score": int})
        step = Step("s1", _return_value({"score": "bad"}), output_contract=schema, retries=0)

        # Validator present, no router
        executor = StepExecutor(state=state, validator=failing_validator)
        result = executor.run(_make_graph([step]))

        # No router: failed validation → step fails after exhausting retries
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL


# ---------------------------------------------------------------------------
# Group 3: Happy Paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """Basic passing scenarios with validator and router wired in."""

    def test_output_passes_validation_step_completes(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """When output passes validation, step completes normally."""
        schema = Schema({"name": str})
        step = Step("s1", _return_value({"name": "Alice"}), output_contract=schema)

        executor = StepExecutor(state=state, validator=validator)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        validator.validate.assert_called_once()

    def test_input_passes_validation_action_runs(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """When input passes validation, the step action is called normally."""
        input_schema = Schema({"dep": str})
        dep_step = Step("dep", _return_value("hello"))
        step = Step("s1", _noop, depends_on=["dep"], input_contract=input_schema)

        executor = StepExecutor(state=state, validator=validator)
        result = executor.run(_make_graph([dep_step, step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED

    def test_both_contracts_pass_step_completes(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """When both input and output contracts pass, step completes."""
        input_schema = Schema({"dep": str})
        output_schema = Schema({"result": str})

        dep_step = Step("dep", _return_value("hello"))
        step = Step(
            "s1",
            _return_value({"result": "world"}),
            depends_on=["dep"],
            input_contract=input_schema,
            output_contract=output_schema,
        )

        executor = StepExecutor(state=state, validator=validator)
        result = executor.run(_make_graph([dep_step, step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        # validate called twice: once for input, once for output
        assert validator.validate.call_count == 2

    def test_retry_succeeds_after_validation_fail_with_router(
        self, state: StateStore, retry_router: FailureRouter
    ) -> None:
        """With router wired, step can recover from validation failure via retry."""
        real_validator = StructuralValidator()
        schema = Schema({"count": int})
        attempt_tracker: list[int] = []

        def action(ctx: StepContext) -> dict[str, object]:
            attempt_tracker.append(ctx.attempt)
            if ctx.attempt == 1:
                return {"count": "not_int"}  # fails validation
            return {"count": 5}  # passes on retry

        step = Step("s1", action, output_contract=schema)
        executor = StepExecutor(state=state, validator=real_validator, failure_router=retry_router)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED

    def test_executor_without_validator_router_same_as_before(self, state: StateStore) -> None:
        """Default constructor (no validator, no router) behaves identically to Module 7."""
        executor = StepExecutor(state=state)
        assert executor._validator is None
        assert executor._failure_router is None

        step = Step("s1", _return_value({"x": 1}))
        result = executor.run(_make_graph([step]))
        assert result.step_results["s1"].status == StepStatus.COMPLETED


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestRetryContextSecurity:
    """Retry context injected by the router must be sanitized."""

    def test_retry_context_from_router_never_contains_raw_output(
        self, state: StateStore, retry_router: FailureRouter
    ) -> None:
        """The retry_context passed to the next attempt must not contain raw step output."""
        received_contexts: list[dict[str, object] | None] = []

        def action(ctx: StepContext) -> dict[str, object]:
            received_contexts.append(ctx.retry_context)
            if ctx.attempt == 1:
                raise RuntimeError("sk-secret-key-12345")  # secret in exception
            return {"ok": True}

        step = Step("s1", action)
        executor = StepExecutor(state=state, failure_router=retry_router)
        executor.run(_make_graph([step]))

        # Second attempt should have received context
        assert len(received_contexts) >= 2
        retry_ctx = received_contexts[1]
        assert retry_ctx is not None
        # The secret key must NOT appear in the retry context
        ctx_str = str(retry_ctx)
        assert "sk-secret-key-12345" not in ctx_str

    def test_retry_context_from_router_contains_only_metadata(
        self, state: StateStore, retry_router: FailureRouter
    ) -> None:
        """Retry context must only contain structured metadata keys."""
        received_contexts: list[dict[str, object] | None] = []

        def action(ctx: StepContext) -> dict[str, object]:
            received_contexts.append(ctx.retry_context)
            if ctx.attempt == 1:
                raise RuntimeError("deliberate failure")
            return {}

        step = Step("s1", action)
        executor = StepExecutor(state=state, failure_router=retry_router)
        executor.run(_make_graph([step]))

        retry_ctx = received_contexts[1]
        assert retry_ctx is not None
        # Only structured metadata is allowed — these keys come from sanitize_retry_context
        # Actual keys: attempt, error_type, error_class, guidance (for execution failures)
        allowed_keys = {
            "attempt",
            "error_type",
            "error_class",
            "validation_errors",
            "guidance",
            "failure_type",  # alias used in some paths
        }
        assert all(k in allowed_keys for k in retry_ctx)

    def test_validator_crash_message_does_not_leak_to_step(
        self, state: StateStore, abort_router: FailureRouter
    ) -> None:
        """If the validator raises, the raw exception must not appear in step result."""
        crashing_validator = MagicMock(spec=StructuralValidator)
        crashing_validator.validate.side_effect = RuntimeError("INTERNAL SECRET: password=hunter2")

        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(
            state=state, validator=crashing_validator, failure_router=abort_router
        )
        result = executor.run(_make_graph([step]))

        # No raw error message must appear anywhere in the result
        result_str = str(result.to_dict())
        assert "password=hunter2" not in result_str
        assert "INTERNAL SECRET" not in result_str


# ---------------------------------------------------------------------------
# Group 5: Hooks
# ---------------------------------------------------------------------------


class TestValidationHooks:
    """New validation lifecycle hooks fire with correct arguments."""

    def test_on_validation_start_fires_for_output_contract(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """on_validation_start must fire with phase='output' when output_contract is present."""
        hook = MagicMock(spec=ExecutorHooks)
        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(state=state, validator=validator, hooks=[hook])
        executor.run(_make_graph([step]))

        phases_seen = [c.args[1] for c in hook.on_validation_start.call_args_list]
        assert "output" in phases_seen

    def test_on_validation_complete_fires_when_valid(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """on_validation_complete fires when validation passes."""
        hook = MagicMock(spec=ExecutorHooks)
        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(state=state, validator=validator, hooks=[hook])
        executor.run(_make_graph([step]))

        hook.on_validation_complete.assert_called_once()

    def test_on_validation_fail_fires_when_invalid(
        self, state: StateStore, failing_validator: MagicMock, abort_router: FailureRouter
    ) -> None:
        """on_validation_fail fires when validation returns invalid result."""
        hook = MagicMock(spec=ExecutorHooks)
        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": "bad"}), output_contract=schema)

        executor = StepExecutor(
            state=state,
            validator=failing_validator,
            failure_router=abort_router,
            hooks=[hook],
        )
        executor.run(_make_graph([step]))

        hook.on_validation_fail.assert_called_once()

    def test_on_validation_start_fires_for_input_contract(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """on_validation_start fires with phase='input' when input_contract is present."""
        hook = MagicMock(spec=ExecutorHooks)
        input_schema = Schema({"dep": str})
        dep = Step("dep", _return_value("hello"))
        step = Step("s1", _noop, depends_on=["dep"], input_contract=input_schema)

        executor = StepExecutor(state=state, validator=validator, hooks=[hook])
        executor.run(_make_graph([dep, step]))

        phases_seen = [c.args[1] for c in hook.on_validation_start.call_args_list]
        assert "input" in phases_seen

    def test_validation_hooks_carry_correct_step_reference(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """Validation hooks receive the correct Step object."""
        hook = MagicMock(spec=ExecutorHooks)
        schema = Schema({"x": int})
        step = Step("my_step", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(state=state, validator=validator, hooks=[hook])
        executor.run(_make_graph([step]))

        # Grab the step arg from on_validation_start (positional arg 0)
        step_arg = hook.on_validation_start.call_args.args[0]
        assert step_arg.name == "my_step"

    def test_hook_exception_does_not_crash_validation(
        self, state: StateStore, validator: MagicMock
    ) -> None:
        """A hook that raises must not crash the executor (same rule as existing hooks)."""
        hook = MagicMock(spec=ExecutorHooks)
        hook.on_validation_start.side_effect = RuntimeError("hook boom")
        hook.on_validation_complete.side_effect = RuntimeError("hook boom")

        schema = Schema({"x": int})
        step = Step("s1", _return_value({"x": 1}), output_contract=schema)

        executor = StepExecutor(state=state, validator=validator, hooks=[hook])
        # Must not raise
        result = executor.run(_make_graph([step]))
        assert result.step_results["s1"].status == StepStatus.COMPLETED


# ---------------------------------------------------------------------------
# Group 6: Max attempts controlled by router policy
# ---------------------------------------------------------------------------


class TestRouterControlsMaxAttempts:
    """When router is wired, policy.max_retries determines attempt count."""

    def test_router_policy_overrides_stepconfig_retries(self, state: StateStore) -> None:
        """Router policy.max_retries takes precedence over StepConfig.retries.

        The router's handle() falls back when attempt_number >= max_retries.
        With max_retries=3: attempt 1 → RETRY, attempt 2 → RETRY, attempt 3 → fallback ABORT.
        Total: 3 attempts.
        """
        attempt_counter: list[int] = []

        def action(ctx: StepContext) -> None:
            attempt_counter.append(ctx.attempt)
            raise RuntimeError("always fail")

        # StepConfig retries=0 but router allows up to 3 attempts (max_retries=3)
        policy = FailurePolicy(
            on_execution_fail=FailureAction.RETRY,
            max_retries=3,
            retry_delay=0.0,
            fallback_action=FailureAction.ABORT,
        )
        router = FailureRouter(workflow_policy=policy)
        step = Step("s1", action, retries=0)  # StepConfig says 0 retries

        executor = StepExecutor(state=state, failure_router=router)
        result = executor.run(_make_graph([step]))

        # Router with max_retries=3 → fallback at attempt 3, giving 3 total attempts
        assert len(attempt_counter) == 3
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL

    def test_router_max_retries_zero_gives_one_attempt(self, state: StateStore) -> None:
        """Router with max_retries=0 means only 1 attempt (no retries)."""
        attempt_counter: list[int] = []

        def action(ctx: StepContext) -> None:
            attempt_counter.append(ctx.attempt)
            raise RuntimeError("fail")

        policy = FailurePolicy(
            on_execution_fail=FailureAction.RETRY,
            max_retries=0,  # no retries → 1 attempt, then fallback
            retry_delay=0.0,
            fallback_action=FailureAction.ABORT,
        )
        router = FailureRouter(workflow_policy=policy)
        step = Step("s1", action, retries=5)  # StepConfig says 5 but router overrides

        executor = StepExecutor(state=state, failure_router=router)
        result = executor.run(_make_graph([step]))

        # max_retries=0 means attempt 1 fails, attempt_number(1) >= max_retries(0) → fallback ABORT
        assert len(attempt_counter) == 1
        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL


# ---------------------------------------------------------------------------
# Group 7: Coverage gap tests (written by QA)
# ---------------------------------------------------------------------------


class TestInputValidationRetryPath:
    """Cover the input validation RETRY path (lines 508-513 in executor.py).

    When input_contract validation fails and the failure router returns RETRY,
    the executor should retry the step. On the next attempt, inputs may change
    (e.g., if a dependency's output was corrected by a preceding retry), or
    the validator may pass.
    """

    def test_input_validation_fail_retry_then_pass(self, state: StateStore) -> None:
        """Input validation fails on attempt 1, passes on attempt 2 via retry router."""
        attempt_tracker: list[int] = []
        call_count = [0]

        # Validator that fails once then passes
        validator = MagicMock(spec=StructuralValidator)

        def validate_side_effect(data: object, schema: object) -> ValidationResult:
            call_count[0] += 1
            if call_count[0] <= 1:
                return _make_invalid_result()
            return _make_valid_result()

        validator.validate.side_effect = validate_side_effect

        def action(ctx: StepContext) -> dict[str, object]:
            attempt_tracker.append(ctx.attempt)
            return {"ok": True}

        input_schema = Schema({"dep": str})
        dep_step = Step("dep", _return_value("hello"))
        step = Step("s1", action, depends_on=["dep"], input_contract=input_schema)

        policy = FailurePolicy(
            on_validation_fail=FailureAction.RETRY,
            on_execution_fail=FailureAction.ABORT,
            max_retries=3,
            retry_delay=0.0,
            fallback_action=FailureAction.ABORT,
        )
        router = FailureRouter(workflow_policy=policy)

        executor = StepExecutor(state=state, validator=validator, failure_router=router)
        result = executor.run(_make_graph([dep_step, step]))

        # Step should have completed after retry
        assert result.step_results["s1"].status == StepStatus.COMPLETED
        assert len(attempt_tracker) >= 1


class TestNoRouterValidationRetryPath:
    """Cover the no-router validation retry path (lines 717-721 in executor.py).

    When no failure_router is present but the step has retries > 0, a validation
    failure should trigger a retry using StepConfig-based retry logic with
    sanitized retry context.
    """

    def test_output_validation_fail_no_router_retries_via_stepconfig(
        self, state: StateStore
    ) -> None:
        """Output validation fail with no router, retries=2: retries then succeeds."""
        call_count = [0]

        # Validator that fails once then passes
        validator = MagicMock(spec=StructuralValidator)

        def validate_side_effect(data: object, schema: object) -> ValidationResult:
            call_count[0] += 1
            if call_count[0] <= 1:
                return _make_invalid_result()
            return _make_valid_result()

        validator.validate.side_effect = validate_side_effect

        attempt_tracker: list[int] = []

        def action(ctx: StepContext) -> dict[str, object]:
            attempt_tracker.append(ctx.attempt)
            return {"score": 42}

        schema = Schema({"score": int})
        step = Step("s1", action, output_contract=schema, retries=2)

        # No router — uses StepConfig.retries
        executor = StepExecutor(state=state, validator=validator)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.COMPLETED
        assert len(attempt_tracker) == 2  # first attempt fails validation, second passes

    def test_output_validation_fail_no_router_exhausts_retries(
        self, state: StateStore, failing_validator: MagicMock
    ) -> None:
        """Output validation always fails with no router, retries=1: FAILED_FINAL."""
        attempt_tracker: list[int] = []

        def action(ctx: StepContext) -> dict[str, object]:
            attempt_tracker.append(ctx.attempt)
            return {"score": "bad"}

        schema = Schema({"score": int})
        step = Step("s1", action, output_contract=schema, retries=1)

        executor = StepExecutor(state=state, validator=failing_validator)
        result = executor.run(_make_graph([step]))

        assert result.step_results["s1"].status == StepStatus.FAILED_FINAL
        assert len(attempt_tracker) == 2  # initial + 1 retry
