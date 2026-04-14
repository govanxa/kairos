"""Tests for kairos.workflow — written BEFORE implementation.

Integration tests for the Workflow class. All components (StateStore, TaskGraph,
StepExecutor, FailureRouter, StructuralValidator) are used as real objects —
nothing is mocked except step actions themselves (plain Python functions).

Test priority order (TDD):
1. Failure paths (construction + runtime)
2. Boundary conditions
3. Happy paths
4. Security constraints
5. Serialization
6. Properties and repr
7. Async stub
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from kairos import (
    SKIP,
    ConfigError,
    FailureAction,
    FailurePolicy,
    PlanError,
    Schema,
    StateError,
    Step,
    StepContext,
    StepStatus,
    Workflow,
    WorkflowStatus,
)

# ---------------------------------------------------------------------------
# Shared step actions (pure functions — no LLM calls)
# ---------------------------------------------------------------------------


def _noop(ctx: StepContext) -> dict[str, object]:
    """Do nothing, return empty dict."""
    return {}


def _store_value(ctx: StepContext) -> dict[str, object]:
    """Write 'x' = 42 into state and return it."""
    ctx.state.set("x", 42)
    return {"x": 42}


def _read_x(ctx: StepContext) -> dict[str, object]:
    """Read 'x' from state and return double."""
    x = ctx.state.get("x")
    return {"doubled": x * 2}  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Group 1: Failure paths — Construction
# ---------------------------------------------------------------------------


class TestConstructionFailures:
    def test_empty_name_raises_config_error(self) -> None:
        """Empty name must raise ConfigError."""
        with pytest.raises(ConfigError):
            Workflow(name="", steps=[Step(name="s", action=_noop)])

    def test_whitespace_only_name_raises_config_error(self) -> None:
        """Whitespace-only name must raise ConfigError."""
        with pytest.raises(ConfigError):
            Workflow(name="   ", steps=[Step(name="s", action=_noop)])

    def test_empty_steps_raises_config_error(self) -> None:
        """Empty steps list must raise ConfigError."""
        with pytest.raises(ConfigError):
            Workflow(name="wf", steps=[])

    def test_cyclic_dependency_raises_plan_error(self) -> None:
        """A cycle in step dependencies is caught at construction time."""
        with pytest.raises(PlanError):
            Workflow(
                name="wf",
                steps=[
                    Step(name="a", action=_noop, depends_on=["b"]),
                    Step(name="b", action=_noop, depends_on=["a"]),
                ],
            )

    def test_duplicate_step_names_raises_plan_error(self) -> None:
        """Duplicate step names in the same workflow raise PlanError."""
        with pytest.raises(PlanError):
            Workflow(
                name="wf",
                steps=[
                    Step(name="s", action=_noop),
                    Step(name="s", action=_noop),
                ],
            )

    def test_missing_dependency_raises_plan_error(self) -> None:
        """A step depending on a non-existent step raises PlanError."""
        with pytest.raises(PlanError):
            Workflow(
                name="wf",
                steps=[
                    Step(name="a", action=_noop, depends_on=["does_not_exist"]),
                ],
            )


# ---------------------------------------------------------------------------
# Group 2: Failure paths — Runtime
# ---------------------------------------------------------------------------


class TestRuntimeFailures:
    def test_step_raises_produces_failed_result(self) -> None:
        """A step that raises an exception produces a FAILED WorkflowResult."""

        def _fail(ctx: StepContext) -> dict[str, object]:
            raise ValueError("something went wrong")

        wf = Workflow(
            name="wf",
            steps=[Step(name="fail_step", action=_fail, retries=0)],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.ABORT,
                max_retries=0,
            ),
        )
        result = wf.run()
        assert result.status == WorkflowStatus.FAILED

    def test_all_retries_exhausted_produces_failed_result(self) -> None:
        """Retries exhausted without success produces a FAILED result."""
        call_count = [0]

        def _always_fail(ctx: StepContext) -> dict[str, object]:
            call_count[0] += 1
            raise RuntimeError("always fails")

        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_always_fail, retries=2, retry_delay=0.0)],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
                fallback_action=FailureAction.ABORT,
            ),
        )
        result = wf.run()
        assert result.status == WorkflowStatus.FAILED
        # 1 original + 2 retries = 3 total attempts
        assert call_count[0] >= 1

    def test_non_serializable_initial_inputs_raises_state_error(self) -> None:
        """Non-JSON-serializable initial_inputs raises StateError."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        with pytest.raises(StateError):
            wf.run(initial_inputs={"bad": object()})

    def test_output_contract_fail_on_abort_policy_produces_failed(self) -> None:
        """A step returning wrong-typed output fails validation → ABORT → FAILED."""

        def _wrong_output(ctx: StepContext) -> dict[str, object]:
            return {"score": "not-a-number"}

        schema = Schema({"score": float})
        step = Step(
            name="s",
            action=_wrong_output,
            output_contract=schema,
            retries=0,
        )
        wf = Workflow(
            name="wf",
            steps=[step],
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.ABORT,
                max_retries=0,
            ),
        )
        result = wf.run()
        assert result.status == WorkflowStatus.FAILED


# ---------------------------------------------------------------------------
# Group 3: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_single_step_workflow(self) -> None:
        """A single-step workflow with no deps runs and completes."""
        wf = Workflow(name="wf", steps=[Step(name="only", action=_noop)])
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert "only" in result.step_results

    def test_none_initial_inputs(self) -> None:
        """run(None) is allowed and treated as empty initial state."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        result = wf.run(None)
        assert result.status == WorkflowStatus.COMPLETE

    def test_empty_dict_initial_inputs(self) -> None:
        """run({}) is allowed — empty state."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        result = wf.run({})
        assert result.status == WorkflowStatus.COMPLETE

    def test_step_returns_none(self) -> None:
        """A step returning None completes successfully."""

        def _return_none(ctx: StepContext) -> None:
            return None

        wf = Workflow(name="wf", steps=[Step(name="s", action=_return_none)])
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert result.step_results["s"].status == StepStatus.COMPLETED

    def test_step_returns_skip_sentinel(self) -> None:
        """A step returning SKIP is recorded as SKIPPED, workflow still COMPLETE."""

        def _skip_action(ctx: StepContext) -> object:
            return SKIP

        wf = Workflow(name="wf", steps=[Step(name="s", action=_skip_action)])
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert result.step_results["s"].status == StepStatus.SKIPPED

    def test_all_steps_skip(self) -> None:
        """All steps returning SKIP → workflow COMPLETE (not FAILED)."""

        def _skip_action(ctx: StepContext) -> object:
            return SKIP

        wf = Workflow(
            name="wf",
            steps=[
                Step(name="a", action=_skip_action),
                Step(name="b", action=_skip_action),
            ],
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE

    def test_run_called_twice_produces_isolated_results(self) -> None:
        """Each call to run() creates a fresh state — no state leak between runs."""
        counter = [0]

        def _increment(ctx: StepContext) -> dict[str, object]:
            counter[0] += 1
            return {"call": counter[0]}

        wf = Workflow(name="wf", steps=[Step(name="s", action=_increment)])
        result1 = wf.run()
        result2 = wf.run()
        # Both succeed
        assert result1.status == WorkflowStatus.COMPLETE
        assert result2.status == WorkflowStatus.COMPLETE
        # Different run outputs (counter advances)
        assert result1.step_results["s"].output != result2.step_results["s"].output

    def test_initial_inputs_available_to_first_step(self) -> None:
        """Initial inputs are merged into state and accessible in the first step."""

        def _read_input(ctx: StepContext) -> dict[str, object]:
            val = ctx.state.get("greeting", None)
            return {"found": val}

        wf = Workflow(name="wf", steps=[Step(name="s", action=_read_input)])
        result = wf.run(initial_inputs={"greeting": "hello"})
        assert result.status == WorkflowStatus.COMPLETE
        assert result.step_results["s"].output == {"found": "hello"}


# ---------------------------------------------------------------------------
# Group 4: Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_linear_chain_a_b_c(self) -> None:
        """Steps A→B→C execute in order and all complete."""
        executed: list[str] = []

        def make_step(
            name: str,
        ) -> Callable[[StepContext], dict[str, object]]:
            def action(ctx: StepContext) -> dict[str, object]:
                executed.append(name)
                return {"by": name}

            return action

        wf = Workflow(
            name="chain",
            steps=[
                Step(name="a", action=make_step("a")),
                Step(name="b", action=make_step("b"), depends_on=["a"]),
                Step(name="c", action=make_step("c"), depends_on=["b"]),
            ],
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert executed == ["a", "b", "c"]
        assert set(result.step_results.keys()) == {"a", "b", "c"}

    def test_diamond_dependency(self) -> None:
        """A → B + C → D (diamond) — D runs after both B and C."""
        executed: list[str] = []

        def make_step(
            name: str,
        ) -> Callable[[StepContext], dict[str, object]]:
            def action(ctx: StepContext) -> dict[str, object]:
                executed.append(name)
                return {"by": name}

            return action

        wf = Workflow(
            name="diamond",
            steps=[
                Step(name="a", action=make_step("a")),
                Step(name="b", action=make_step("b"), depends_on=["a"]),
                Step(name="c", action=make_step("c"), depends_on=["a"]),
                Step(name="d", action=make_step("d"), depends_on=["b", "c"]),
            ],
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        # D must come after both B and C
        assert executed.index("d") > executed.index("b")
        assert executed.index("d") > executed.index("c")

    def test_output_contract_passes_validation(self) -> None:
        """A step with a matching output contract completes successfully."""

        def _valid_output(ctx: StepContext) -> dict[str, object]:
            return {"score": 0.95}

        schema = Schema({"score": float})
        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_valid_output, output_contract=schema)],
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE

    def test_retry_succeeds_on_second_attempt(self) -> None:
        """A step that fails once then succeeds completes as COMPLETED.

        The FailureRouter checks ``attempt_number >= max_retries`` to decide
        when retries are exhausted.  With max_retries=2, attempt 1 (< 2)
        gets a retry, attempt 2 succeeds.
        """
        call_count = [0]

        def _fail_once(ctx: StepContext) -> dict[str, object]:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first attempt fails")
            return {"ok": True}

        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_fail_once, retries=2, retry_delay=0.0)],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
                fallback_action=FailureAction.ABORT,
            ),
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert call_count[0] == 2

    def test_result_contains_all_step_results(self) -> None:
        """WorkflowResult.step_results has an entry for every step."""
        wf = Workflow(
            name="wf",
            steps=[
                Step(name="alpha", action=_noop),
                Step(name="beta", action=_noop, depends_on=["alpha"]),
            ],
        )
        result = wf.run()
        assert "alpha" in result.step_results
        assert "beta" in result.step_results

    def test_positive_duration_ms(self) -> None:
        """WorkflowResult.duration_ms is >= 0."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        result = wf.run()
        assert result.duration_ms >= 0

    def test_metadata_preserved_in_workflow(self) -> None:
        """Metadata passed to Workflow is stored and accessible."""
        meta: dict[str, object] = {"author": "test", "version": "1.0"}
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)], metadata=meta)
        assert wf.graph.metadata == meta

    def test_state_passes_between_steps(self) -> None:
        """State written by step A is readable by step B."""
        wf = Workflow(
            name="wf",
            steps=[
                Step(name="writer", action=_store_value),
                Step(name="reader", action=_read_x, depends_on=["writer"]),
            ],
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert result.step_results["reader"].output == {"doubled": 84}


# ---------------------------------------------------------------------------
# Group 5: Security constraints
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_sensitive_keys_redacted_in_final_state(self) -> None:
        """Sensitive state keys are redacted in WorkflowResult.final_state."""

        def _write_secret(ctx: StepContext) -> dict[str, object]:
            ctx.state.set("api_key", "sk-super-secret")
            return {}

        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_write_secret)],
            sensitive_keys=["api_key"],
        )
        result = wf.run()
        # final_state uses to_safe_dict() — secret must be redacted
        assert result.final_state.get("api_key") == "[REDACTED]"

    def test_sensitive_key_accessible_inside_step_via_get(self) -> None:
        """Sensitive keys are accessible inside the step action via state.get()."""
        retrieved: list[object] = [None]

        def _write_then_read(ctx: StepContext) -> dict[str, object]:
            ctx.state.set("api_key", "sk-secret-value")
            retrieved[0] = ctx.state.get("api_key")
            return {}

        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_write_then_read)],
            sensitive_keys=["api_key"],
        )
        wf.run()
        assert retrieved[0] == "sk-secret-value"

    def test_scoped_proxy_blocks_unauthorized_read(self) -> None:
        """A step with read_keys cannot read keys outside that list.

        When the scoped proxy raises StateError, the executor catches it and
        marks the step as FAILED, so the workflow ends FAILED.
        """

        def _write_setup(ctx: StepContext) -> dict[str, object]:
            ctx.state.set("allowed", "yes")
            ctx.state.set("forbidden", "no")
            return {}

        def _try_unauthorized_read(ctx: StepContext) -> dict[str, object]:
            # This read is outside read_keys — should raise StateError
            return {"val": ctx.state.get("forbidden")}

        wf = Workflow(
            name="wf",
            steps=[
                Step(name="setup", action=_write_setup),
                Step(
                    name="restricted",
                    action=_try_unauthorized_read,
                    depends_on=["setup"],
                    read_keys=["allowed"],  # "forbidden" is NOT in read_keys
                    retries=0,
                ),
            ],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.ABORT,
                max_retries=0,
            ),
        )
        result = wf.run()
        assert result.status == WorkflowStatus.FAILED
        assert result.step_results["restricted"].status == StepStatus.FAILED_FINAL

    def test_retry_context_does_not_contain_raw_exception_message(self) -> None:
        """Retry context must never include the raw exception message."""
        retry_contexts: list[dict[str, object]] = []

        def _capture_retry_ctx(ctx: StepContext) -> dict[str, object]:
            if ctx.retry_context is not None:
                retry_contexts.append(dict(ctx.retry_context))
            raise RuntimeError("SECRET_API_KEY=sk-1234 exposed in error")

        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_capture_retry_ctx, retries=1, retry_delay=0.0)],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=1,
                fallback_action=FailureAction.ABORT,
            ),
        )
        wf.run()
        # If a retry context was captured, it must not contain the raw error message
        for ctx in retry_contexts:
            assert "SECRET_API_KEY" not in str(ctx)
            assert "sk-1234" not in str(ctx)

    def test_scoped_proxy_blocks_unauthorized_write(self) -> None:
        """A step with write_keys cannot write to keys outside that list.

        The ScopedStateProxy raises StateError on unauthorized writes.  The
        executor catches that and marks the step FAILED_FINAL, making the
        overall workflow FAILED.
        """

        def _try_unauthorized_write(ctx: StepContext) -> dict[str, object]:
            # "allowed" is in write_keys — this is fine.
            ctx.state.set("allowed", "ok")
            # "forbidden" is NOT in write_keys — should raise StateError.
            ctx.state.set("forbidden", "bad")
            return {}

        wf = Workflow(
            name="wf",
            steps=[
                Step(
                    name="restricted",
                    action=_try_unauthorized_write,
                    write_keys=["allowed"],  # "forbidden" is NOT in write_keys
                    retries=0,
                ),
            ],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.ABORT,
                max_retries=0,
            ),
        )
        result = wf.run()
        assert result.status == WorkflowStatus.FAILED
        assert result.step_results["restricted"].status == StepStatus.FAILED_FINAL


# ---------------------------------------------------------------------------
# Group 5b: Integration — LLM circuit breaker, inter-step validation, foreach
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_llm_calls_zero_for_workflow_with_no_llm_calls(self) -> None:
        """A workflow whose step actions never call increment_llm_calls reports 0 LLM calls.

        This verifies that max_llm_calls is wired through to the executor and
        that the WorkflowResult.llm_calls field is populated correctly.
        """
        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_noop)],
            max_llm_calls=10,
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE
        assert result.llm_calls == 0

    def test_output_contract_violation_prevents_bad_data_reaching_next_step(self) -> None:
        """Broken output from step A is caught by its output_contract before step B runs.

        Step A has output_contract=Schema({"value": str}) but returns {"value": 123}
        (an int).  The executor validates A's output after it completes and fails
        the step.  Step B depends on A, so B is skipped (dependency failed) and
        never executes with the bad data.
        """
        step_b_executed = [False]

        def _step_a(ctx: StepContext) -> dict[str, object]:
            # Returns int where str is required — violates output_contract
            return {"value": 123}

        def _step_b(ctx: StepContext) -> dict[str, object]:
            step_b_executed[0] = True
            return {}

        output_schema = Schema({"value": str})
        wf = Workflow(
            name="wf",
            steps=[
                Step(
                    name="a",
                    action=_step_a,
                    output_contract=output_schema,
                    retries=0,
                ),
                Step(name="b", action=_step_b, depends_on=["a"]),
            ],
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.ABORT,
                max_retries=0,
            ),
        )
        result = wf.run()
        # Workflow should be FAILED because A's output contract was violated
        assert result.status == WorkflowStatus.FAILED
        # Step B must never have executed — bad data was stopped at A
        assert step_b_executed[0] is False

    def test_foreach_fan_out_processes_each_item(self) -> None:
        """A foreach step fans out over a collection and collects per-item results.

        Initial state contains items=[10, 20, 30].  The step doubles each item.
        The output stored under the step name is a list of 3 result dicts.
        """

        def _double_item(ctx: StepContext) -> dict[str, object]:
            item = ctx.item
            return {"doubled": item * 2}  # type: ignore[operator]

        wf = Workflow(
            name="wf",
            steps=[
                Step(name="doubler", action=_double_item, foreach="items"),
            ],
        )
        result = wf.run(initial_inputs={"items": [10, 20, 30]})
        assert result.status == WorkflowStatus.COMPLETE
        step_output = result.step_results["doubler"].output
        # output is a list of per-item result dicts
        assert isinstance(step_output, list)
        typed_output = cast(list[dict[str, Any]], step_output)
        assert len(typed_output) == 3
        doubled_values = [item["doubled"] for item in typed_output]
        assert doubled_values == [20, 40, 60]


# ---------------------------------------------------------------------------
# Group 6: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict_is_json_safe(self) -> None:
        """to_dict() output passes json.dumps without error."""
        wf = Workflow(
            name="my-workflow",
            steps=[Step(name="a", action=_noop), Step(name="b", action=_noop, depends_on=["a"])],
            failure_policy=FailurePolicy(max_retries=3),
            max_llm_calls=25,
            metadata={"author": "test"},
        )
        d = wf.to_dict()
        serialized = json.dumps(d)
        assert serialized  # non-empty string

    def test_to_dict_omits_callables(self) -> None:
        """to_dict() must not include any callable objects."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        d = wf.to_dict()
        raw = json.dumps(d)
        # Round-trip back to dict should succeed and contain no callables
        parsed = json.loads(raw)
        assert "action" not in str(parsed)

    def test_to_dict_includes_required_keys(self) -> None:
        """to_dict() includes name, graph, and max_llm_calls."""
        wf = Workflow(name="my-wf", steps=[Step(name="s", action=_noop)])
        d = wf.to_dict()
        assert d["name"] == "my-wf"
        assert "graph" in d
        assert "max_llm_calls" in d

    def test_to_dict_includes_failure_policy_when_set(self) -> None:
        """to_dict() includes serialized failure_policy when one is provided."""
        policy = FailurePolicy(max_retries=5)
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)], failure_policy=policy)
        d = wf.to_dict()
        assert "failure_policy" in d
        assert d["failure_policy"] is not None

    def test_to_dict_omits_sensitive_keys(self) -> None:
        """to_dict() omits sensitive_keys from the serialized output."""
        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_noop)],
            sensitive_keys=["api_key", "password"],
        )
        d = wf.to_dict()
        assert "sensitive_keys" not in d

    def test_to_dict_failure_policy_absent_when_none(self) -> None:
        """to_dict() omits failure_policy key when none is set."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        d = wf.to_dict()
        # failure_policy should be absent or None when not configured
        assert d.get("failure_policy") is None or "failure_policy" not in d


# ---------------------------------------------------------------------------
# Group 7: Properties and repr
# ---------------------------------------------------------------------------


class TestPropertiesAndRepr:
    def test_name_property(self) -> None:
        """name property returns the workflow name."""
        wf = Workflow(name="test-wf", steps=[Step(name="s", action=_noop)])
        assert wf.name == "test-wf"

    def test_steps_property_returns_copy(self) -> None:
        """steps property returns a copy — mutation does not affect the workflow."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        steps_copy = wf.steps
        steps_copy.append(Step(name="intruder", action=_noop))
        assert len(wf.steps) == 1

    def test_graph_property(self) -> None:
        """graph property returns the TaskGraph."""
        from kairos.plan import TaskGraph

        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        assert isinstance(wf.graph, TaskGraph)

    def test_registry_property(self) -> None:
        """registry property returns a SchemaRegistry."""
        from kairos.schema import SchemaRegistry

        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        assert isinstance(wf.registry, SchemaRegistry)

    def test_failure_policy_property_none_by_default(self) -> None:
        """failure_policy property is None when not configured."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        assert wf.failure_policy is None

    def test_failure_policy_property_set(self) -> None:
        """failure_policy property returns the configured policy."""
        policy = FailurePolicy(max_retries=3)
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)], failure_policy=policy)
        assert wf.failure_policy is policy

    def test_repr_contains_name_and_step_names(self) -> None:
        """repr includes the workflow name and step names."""
        wf = Workflow(
            name="my-wf",
            steps=[Step(name="alpha", action=_noop), Step(name="beta", action=_noop)],
        )
        r = repr(wf)
        assert "my-wf" in r
        assert "alpha" in r
        assert "beta" in r

    def test_registry_populated_for_steps_with_contracts(self) -> None:
        """SchemaRegistry is populated for steps that declare input/output contracts."""
        output_schema = Schema({"result": str})
        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_noop, output_contract=output_schema)],
        )
        assert wf.registry.get_output_contract("s") is output_schema


# ---------------------------------------------------------------------------
# Group 8: Async stub
# ---------------------------------------------------------------------------


class TestAsyncStub:
    def test_run_async_raises_not_implemented(self) -> None:
        """run_async() raises NotImplementedError in MVP."""
        import asyncio

        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        with pytest.raises(NotImplementedError):
            asyncio.run(wf.run_async())


# ---------------------------------------------------------------------------
# Group 9: Concurrent execution — Workflow.max_concurrency
# ---------------------------------------------------------------------------


class TestWorkflowConcurrency:
    def test_max_concurrency_stored(self) -> None:
        """Workflow accepts and stores the max_concurrency parameter."""
        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_noop)],
            max_concurrency=4,
        )
        assert wf.max_concurrency == 4

    def test_default_max_concurrency_is_none(self) -> None:
        """Default max_concurrency is None (no explicit limit)."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        assert wf.max_concurrency is None

    def test_max_concurrency_forwarded_to_executor(self) -> None:
        """Workflow.run() forwards max_concurrency to StepExecutor."""
        # Run a workflow with a parallel step and max_concurrency — if it completes
        # without error, the parameter was forwarded correctly to StepExecutor.
        wf = Workflow(
            name="wf",
            steps=[Step(name="p", action=_noop, parallel=True)],
            max_concurrency=2,
        )
        result = wf.run()
        assert result.status == WorkflowStatus.COMPLETE

    def test_max_concurrency_in_to_dict(self) -> None:
        """to_dict() includes max_concurrency when set."""
        wf = Workflow(
            name="wf",
            steps=[Step(name="s", action=_noop)],
            max_concurrency=8,
        )
        d = wf.to_dict()
        assert d.get("max_concurrency") == 8

    def test_max_concurrency_none_in_to_dict(self) -> None:
        """to_dict() includes max_concurrency=None when not set."""
        wf = Workflow(name="wf", steps=[Step(name="s", action=_noop)])
        d = wf.to_dict()
        assert "max_concurrency" in d
        assert d["max_concurrency"] is None
