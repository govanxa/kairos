"""Kairos executor — runtime engine that drives workflow step execution.

Provides:
- ExecutorHooks: Protocol/base class for step and workflow lifecycle events.
- WorkflowResult: Terminal result of a complete workflow run.
- StepExecutor: Executes a TaskGraph step-by-step with retries, foreach fan-out,
  timeout enforcement, scoped state proxies, and LLM call counting.

Security contracts:
- AttemptRecord stores only sanitized error info via sanitize_exception().
- Retry context uses sanitize_retry_context() — never raw exception messages.
- ScopedStateProxy is provided when a step declares read_keys or write_keys.
- Input resolution uses json.loads(json.dumps()) for deep copy — never references.
- WorkflowResult.final_state uses state.to_safe_dict() — sensitive keys redacted.
- LLM call circuit breaker aborts the workflow at max_llm_calls (default 50).
- Hook exceptions are caught and logged — they must never crash the executor.
"""

from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from kairos.enums import AttemptStatus, FailureType, ForeachPolicy, StepStatus, WorkflowStatus
from kairos.exceptions import ConfigError, ExecutionError, StateError
from kairos.plan import TaskGraph
from kairos.schema import ValidationResult
from kairos.security import sanitize_exception, sanitize_retry_context
from kairos.state import StateStore
from kairos.step import SKIP, AttemptRecord, Step, StepConfig, StepContext, StepResult

if TYPE_CHECKING:
    from kairos.failure import FailureRouter
    from kairos.validators import StructuralValidator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal sentinel exception for the LLM circuit breaker
# ---------------------------------------------------------------------------


class _LLMCircuitBreakerError(ExecutionError):
    """Raised by increment_llm_calls() when the LLM call limit is hit.

    This is an internal exception that signals the executor to abort the
    entire workflow immediately. It is NOT treated as a retryable step failure
    — it propagates directly out of run() as an ExecutionError.

    Users see ExecutionError (the public base type) in their ``except`` clauses.
    The internal subclass exists purely so _execute_with_retries can distinguish
    'circuit breaker abort' from 'step raised ExecutionError itself'.
    """


# ---------------------------------------------------------------------------
# ExecutorHooks — lifecycle event interface
# ---------------------------------------------------------------------------


class ExecutorHooks:
    """Base class providing no-op implementations of all lifecycle hooks.

    Consumers (RunLogger, ValidationEngine, FailureRouter) subclass this and
    override only the events they care about. The executor calls each registered
    hook safely — exceptions inside hooks are caught and logged, they never
    propagate to the executor.

    All methods are called synchronously in the order hooks appear in the
    ``StepExecutor.hooks`` list.
    """

    def on_step_start(self, step: Step, attempt: int) -> None:
        """Called at the start of each attempt, including retries.

        Args:
            step: The step about to be executed.
            attempt: 1-based attempt number (1 for first try, 2 for first retry, …).
        """

    def on_step_complete(self, step: Step, result: StepResult) -> None:
        """Called when a step finishes successfully (status COMPLETED).

        Not called for SKIPPED steps — use on_step_skip for those.

        Args:
            step: The completed step.
            result: The step's final StepResult.
        """

    def on_step_fail(self, step: Step, error: Exception, attempt: int) -> None:
        """Called after each failed attempt (exception or timeout).

        Args:
            step: The step that failed.
            error: The exception that was raised.
            attempt: The 1-based attempt number that failed.
        """

    def on_step_retry(self, step: Step, attempt: int) -> None:
        """Called before each retry attempt, after the delay sleep.

        Fires AFTER the delay sleep and BEFORE the next on_step_start call.

        Args:
            step: The step about to be retried.
            attempt: The 1-based attempt number of the upcoming retry.
        """

    def on_step_skip(self, step: Step, reason: str) -> None:
        """Called when a step is skipped — either via SKIP sentinel or dependency failure.

        Args:
            step: The skipped step.
            reason: Human-readable explanation (e.g. "returned SKIP sentinel",
                "dependency 'X' failed").
        """

    def on_validation_start(self, step: Step, phase: str, attempt: int) -> None:
        """Called just before a contract validation runs on a step.

        Args:
            step: The step whose contract is being validated.
            phase: Either "input" (before action) or "output" (after action).
            attempt: 1-based attempt number for this validation.
        """

    def on_validation_complete(self, step: Step, phase: str, result: ValidationResult) -> None:
        """Called after a contract validation passes (valid=True).

        Not called for invalid results — use on_validation_fail for those.

        Args:
            step: The step whose contract was validated.
            phase: Either "input" or "output".
            result: The passing ValidationResult.
        """

    def on_validation_fail(
        self, step: Step, phase: str, result: ValidationResult, attempt: int
    ) -> None:
        """Called after a contract validation fails (valid=False).

        Args:
            step: The step whose contract failed.
            phase: Either "input" or "output".
            result: The failing ValidationResult.
            attempt: 1-based attempt number for this validation.
        """

    def on_workflow_start(self, graph: TaskGraph) -> None:
        """Called once at the start of StepExecutor.run().

        Args:
            graph: The TaskGraph about to be executed.
        """

    def on_workflow_complete(self, result: WorkflowResult) -> None:
        """Called once after all steps have completed (or the workflow has failed).

        Args:
            result: The final WorkflowResult.
        """


# ---------------------------------------------------------------------------
# WorkflowResult — terminal output of a workflow run
# ---------------------------------------------------------------------------


@dataclass
class WorkflowResult:
    """Terminal result of a complete workflow execution.

    Attributes:
        status: Overall workflow status (COMPLETE or FAILED).
        step_results: Dict mapping step name to its StepResult.
        final_state: Safe snapshot of state after all steps — sensitive keys
            are redacted to "[REDACTED]" via to_safe_dict().
        duration_ms: Total wall-clock time for the entire workflow in milliseconds.
        timestamp: UTC datetime when the workflow started.
        llm_calls: Total LLM invocations made during this run.
    """

    status: WorkflowStatus
    step_results: dict[str, StepResult]
    final_state: dict[str, object]
    duration_ms: float
    timestamp: datetime
    llm_calls: int

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-serializable dict.

        Returns:
            A dict with all fields in JSON-safe form: enums as strings,
            datetimes as ISO 8601 strings, StepResults serialized via their
            own to_dict() method.
        """
        return {
            "status": self.status.value,
            "step_results": {name: result.to_dict() for name, result in self.step_results.items()},
            "final_state": self.final_state,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp.isoformat(),
            "llm_calls": self.llm_calls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowResult:
        """Reconstruct a WorkflowResult from a serialized dict.

        This is the inverse of to_dict(). Step results are reconstructed via
        StepResult.from_dict().

        Args:
            data: A dict previously produced by to_dict().

        Returns:
            A WorkflowResult with all fields restored.
        """
        from kairos.step import StepResult  # local import avoids circular at module level

        step_results_raw: Any = data.get("step_results", {})
        if not isinstance(step_results_raw, dict):
            step_results_raw = {}
        sr_dict = cast(dict[str, Any], step_results_raw)

        step_results: dict[str, StepResult] = {
            name: StepResult.from_dict(sr) for name, sr in sr_dict.items()
        }

        raw_final_state: Any = data.get("final_state", {})
        final_state: dict[str, object] = (
            cast(dict[str, object], raw_final_state) if isinstance(raw_final_state, dict) else {}
        )

        raw_status = data["status"]
        if not isinstance(raw_status, str):
            raise ConfigError(
                f"WorkflowResult.from_dict: 'status' must be a str, got {type(raw_status)}"
            )

        raw_duration = data["duration_ms"]
        if not isinstance(raw_duration, (int, float)):
            raise ConfigError(
                f"WorkflowResult.from_dict: 'duration_ms' must be numeric, got {type(raw_duration)}"
            )

        raw_llm_calls = data["llm_calls"]
        if not isinstance(raw_llm_calls, (int, float)):
            raise ConfigError(
                f"WorkflowResult.from_dict: 'llm_calls' must be numeric, got {type(raw_llm_calls)}"
            )

        return cls(
            status=WorkflowStatus(raw_status),
            step_results=step_results,
            final_state=final_state,
            duration_ms=float(raw_duration),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            llm_calls=int(raw_llm_calls),
        )


# ---------------------------------------------------------------------------
# StepExecutor — the runtime engine
# ---------------------------------------------------------------------------


class StepExecutor:
    """Executes a TaskGraph step by step with retries, foreach, and lifecycle hooks.

    The executor walks the task graph in topologically sorted order and invokes
    each step's action with a ``StepContext``. It handles:
    - Retry loops with configurable backoff and jitter.
    - Timeout enforcement via ``ThreadPoolExecutor``.
    - foreach fan-out over a state collection.
    - Scoped state proxy injection for steps with declared read/write keys.
    - LLM call counting and circuit-breaker.
    - Hook emission at every lifecycle transition.

    Args:
        state: The StateStore shared across all steps in this run.
        hooks: Optional list of ExecutorHooks subscribers. Each receives all events.
        max_llm_calls: Hard limit on total LLM invocations. ExecutionError is
            raised when the limit is reached. Default: 50.
        validator: Optional StructuralValidator for input/output contract validation.
            When None, all contract validation is skipped even if steps declare contracts.
        failure_router: Optional FailureRouter for failure policy resolution.
            When None, the executor falls back to StepConfig.retries (Module 7 behavior).
    """

    def __init__(
        self,
        state: StateStore,
        hooks: list[ExecutorHooks] | None = None,
        max_llm_calls: int = 50,
        validator: StructuralValidator | None = None,
        failure_router: FailureRouter | None = None,
    ) -> None:
        self._state = state
        self._hooks: list[ExecutorHooks] = hooks or []
        self._max_llm_calls = max_llm_calls
        self._llm_call_count: int = 0
        self._validator = validator
        self._failure_router = failure_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def llm_call_count(self) -> int:
        """Current number of LLM calls made during this run.

        Returns:
            The accumulated LLM call count.
        """
        return self._llm_call_count

    def increment_llm_calls(self, count: int = 1) -> None:
        """Increment the LLM call counter, raising ExecutionError at the limit.

        Step actions call this method each time they invoke an LLM to participate
        in the circuit-breaker. Semantic validators and re-planning also call this.

        Args:
            count: Number of LLM calls to add. Defaults to 1.

        Raises:
            ExecutionError: When the new total would reach or exceed max_llm_calls.
        """
        self._llm_call_count += count
        if self._llm_call_count > self._max_llm_calls:
            raise _LLMCircuitBreakerError(
                f"LLM call limit reached: {self._llm_call_count} calls exceed "
                f"the configured maximum of {self._max_llm_calls}."
            )

    def run(self, graph: TaskGraph) -> WorkflowResult:
        """Execute all steps in *graph* in topological order.

        Validates the graph structure first — PlanError is raised before any
        step runs if the graph is invalid (cycles, missing dependencies, etc.).

        Args:
            graph: The TaskGraph to execute.

        Returns:
            A WorkflowResult with the final status, per-step results, and
            the redacted final state.

        Raises:
            PlanError: When the graph fails structural validation (cycles,
                missing deps, duplicate names).
            ExecutionError: When the LLM call limit is exceeded mid-run.
        """
        # --- Validate graph structure first ---
        errors = graph.validate()
        if errors:
            raise errors[0]

        start_time = time.monotonic()
        start_timestamp = datetime.now(tz=UTC)

        self._emit_hook("on_workflow_start", graph)

        step_results: dict[str, StepResult] = {}
        # Track steps whose failure should cascade to dependents.
        # This includes FAILED_FINAL steps AND steps skipped due to a dependency failure
        # (because their dependents should also skip — the failure propagates transitively).
        failed_step_names: set[str] = set()

        # Topological sort guarantees dependency order.
        ordered_steps = graph.topological_sort()

        for step in ordered_steps:
            # --- Dependency cascade: skip if any dependency failed or was cascade-skipped ---
            failed_deps = [dep for dep in step.depends_on if dep in failed_step_names]
            if failed_deps:
                skip_reason = f"dependency '{failed_deps[0]}' failed"
                step_result = self._make_skip_result(step, reason=skip_reason)
                step_results[step.name] = step_result
                self._emit_hook("on_step_skip", step, skip_reason)
                # Store None in state so downstream steps see a value
                self._state.set(step.name, None)
                # Add this step to failed_step_names so its dependents also cascade
                failed_step_names.add(step.name)
                continue

            # --- Execute: foreach or single ---
            if step.config.foreach is not None:
                step_result = self._execute_foreach(step)
            else:
                step_result = self._execute_with_retries(step)

            step_results[step.name] = step_result

            # Mark failed steps so dependents know to skip
            if step_result.status == StepStatus.FAILED_FINAL:
                failed_step_names.add(step.name)

        # --- Compute final workflow status ---
        any_failed = any(r.status == StepStatus.FAILED_FINAL for r in step_results.values())
        workflow_status = WorkflowStatus.FAILED if any_failed else WorkflowStatus.COMPLETE

        duration_ms = (time.monotonic() - start_time) * 1000.0

        result = WorkflowResult(
            status=workflow_status,
            step_results=step_results,
            final_state=self._state.to_safe_dict(),
            duration_ms=duration_ms,
            timestamp=start_timestamp,
            llm_calls=self._llm_call_count,
        )

        self._emit_hook("on_workflow_complete", result)

        return result

    # ------------------------------------------------------------------
    # Private execution methods
    # ------------------------------------------------------------------

    def _execute_with_retries(
        self,
        step: Step,
        item: object = None,
        item_index: int | None = None,
    ) -> StepResult:
        """Execute a step with up to config.retries retry attempts.

        When a failure_router is wired in, max_attempts is derived from the
        resolved policy (policy.max_retries + 1) instead of StepConfig.retries.
        When no router is present, behaviour is identical to Module 7.

        This is the core retry loop. It:
        1. Fires on_step_start at the beginning of each attempt.
        2. Optionally validates inputs against step.input_contract.
        3. Invokes the step action with a fresh StepContext.
        4. On success: optionally validates output against step.output_contract.
        5. On validation/execution failure: routes via failure_router or falls
           back to StepConfig-based retry logic.
        6. Between retries: sleeps with jitter, fires on_step_retry.

        Args:
            step: The step to execute.
            item: Current item if this is a foreach sub-invocation.
            item_index: 0-based index of *item* in the foreach collection.

        Returns:
            A StepResult with COMPLETED, SKIPPED, or FAILED_FINAL status.
        """
        # --- Determine max_attempts from router policy or StepConfig ---
        if self._failure_router is not None:
            from kairos.failure import FailurePolicy  # noqa: PLC0415

            step_policy = (
                step.failure_policy if isinstance(step.failure_policy, FailurePolicy) else None
            )
            effective_policy = self._failure_router.resolve_policy(step_policy)
            max_attempts = effective_policy.max_retries + 1
        else:
            max_attempts = step.config.retries + 1

        attempts: list[AttemptRecord] = []
        retry_context: dict[str, object] | None = None
        step_start_timestamp = datetime.now(tz=UTC)
        step_start_mono = time.monotonic()

        for attempt_num in range(1, max_attempts + 1):
            attempt_timestamp = datetime.now(tz=UTC)
            attempt_start_mono = time.monotonic()

            self._emit_hook("on_step_start", step, attempt_num)

            ctx = self._build_context(step, attempt_num, item, retry_context)

            # --- Input validation (before action) ---
            if step.input_contract is not None:
                input_result = self._validate_contract(
                    ctx.inputs, step.input_contract, step, "input", attempt_num
                )
                if input_result is not None and not input_result.valid:
                    # Input contract failed — route or retry
                    dispatch = self._dispatch_validation_failure(
                        step,
                        input_result,
                        attempt_num,
                        max_attempts,
                        step_start_mono,
                        step_start_timestamp,
                        attempts,
                    )
                    if isinstance(dispatch, StepResult):
                        return dispatch
                    # dispatch is a (retry_context, continue) signal
                    retry_context = dispatch
                    delay = self._calculate_retry_delay(step.config, attempt=attempt_num)
                    if delay > 0:
                        time.sleep(delay)
                    self._emit_hook("on_step_retry", step, attempt_num + 1)
                    continue

            try:
                output = self._invoke_action(step, ctx)
            except _LLMCircuitBreakerError:
                # Re-raise the circuit-breaker sentinel immediately — this is not a
                # retryable step failure but an abort signal for the entire workflow.
                raise
            except Exception as exc:
                # --- Failed attempt ---
                attempt_duration_ms = (time.monotonic() - attempt_start_mono) * 1000.0
                error_type, error_message = sanitize_exception(exc)

                attempt_record = AttemptRecord(
                    attempt_number=attempt_num,
                    status=AttemptStatus.FAILURE,
                    output=None,
                    error_type=error_type,
                    error_message=error_message,
                    duration_ms=attempt_duration_ms,
                    timestamp=attempt_timestamp,
                )
                attempts.append(attempt_record)
                self._emit_hook("on_step_fail", step, exc, attempt_num)

                if self._failure_router is not None:
                    # Route via failure router
                    result_or_ctx = self._handle_failure(
                        step, FailureType.EXECUTION, exc, attempt_num
                    )
                    if isinstance(result_or_ctx, StepResult):
                        return result_or_ctx
                    # RETRY: result_or_ctx is the retry_context dict
                    retry_context = result_or_ctx
                    delay = self._calculate_retry_delay(step.config, attempt=attempt_num)
                    if delay > 0:
                        time.sleep(delay)
                    self._emit_hook("on_step_retry", step, attempt_num + 1)
                elif attempt_num < max_attempts:
                    # No router — fallback to StepConfig retry behavior
                    retry_context = sanitize_retry_context(
                        step_output=None,
                        exception=exc,
                        attempt=attempt_num,
                        failure_type="execution",
                    )
                    delay = self._calculate_retry_delay(step.config, attempt=attempt_num)
                    if delay > 0:
                        time.sleep(delay)
                    self._emit_hook("on_step_retry", step, attempt_num + 1)
                else:
                    # All attempts exhausted (no router path)
                    total_duration_ms = (time.monotonic() - step_start_mono) * 1000.0
                    return StepResult(
                        step_id=step.name,
                        status=StepStatus.FAILED_FINAL,
                        output=None,
                        attempts=attempts,
                        duration_ms=total_duration_ms,
                        timestamp=step_start_timestamp,
                    )
            else:
                # --- Successful action invocation ---
                attempt_duration_ms = (time.monotonic() - attempt_start_mono) * 1000.0

                # Handle SKIP sentinel — bypass output validation
                if output is SKIP:
                    total_duration_ms = (time.monotonic() - step_start_mono) * 1000.0
                    attempt_record = AttemptRecord(
                        attempt_number=attempt_num,
                        status=AttemptStatus.SUCCESS,
                        output=None,
                        error_type=None,
                        error_message=None,
                        duration_ms=attempt_duration_ms,
                        timestamp=attempt_timestamp,
                    )
                    attempts.append(attempt_record)
                    self._state.set(step.name, None)
                    step_result = StepResult(
                        step_id=step.name,
                        status=StepStatus.SKIPPED,
                        output=None,
                        attempts=attempts,
                        duration_ms=total_duration_ms,
                        timestamp=step_start_timestamp,
                    )
                    self._emit_hook("on_step_skip", step, "returned SKIP sentinel")
                    return step_result

                # --- Output validation ---
                if step.output_contract is not None:
                    output_result = self._validate_contract(
                        output, step.output_contract, step, "output", attempt_num
                    )
                    if output_result is not None and not output_result.valid:
                        # Record a failed attempt
                        attempt_record = AttemptRecord(
                            attempt_number=attempt_num,
                            status=AttemptStatus.FAILURE,
                            output=None,
                            error_type="ValidationError",
                            error_message="Output contract validation failed.",
                            duration_ms=attempt_duration_ms,
                            timestamp=attempt_timestamp,
                        )
                        attempts.append(attempt_record)

                        dispatch = self._dispatch_validation_failure(
                            step,
                            output_result,
                            attempt_num,
                            max_attempts,
                            step_start_mono,
                            step_start_timestamp,
                            attempts,
                        )
                        if isinstance(dispatch, StepResult):
                            return dispatch
                        retry_context = dispatch
                        delay = self._calculate_retry_delay(step.config, attempt=attempt_num)
                        if delay > 0:
                            time.sleep(delay)
                        self._emit_hook("on_step_retry", step, attempt_num + 1)
                        continue

                # --- Normal success — store output and complete ---
                # For foreach sub-invocations, storage is handled by _execute_foreach
                if item_index is None:
                    self._state.set(step.name, output)

                attempt_record = AttemptRecord(
                    attempt_number=attempt_num,
                    status=AttemptStatus.SUCCESS,
                    output=output,
                    error_type=None,
                    error_message=None,
                    duration_ms=attempt_duration_ms,
                    timestamp=attempt_timestamp,
                )
                attempts.append(attempt_record)

                total_duration_ms = (time.monotonic() - step_start_mono) * 1000.0
                step_result = StepResult(
                    step_id=step.name,
                    status=StepStatus.COMPLETED,
                    output=output,
                    attempts=attempts,
                    duration_ms=total_duration_ms,
                    timestamp=step_start_timestamp,
                )
                self._emit_hook("on_step_complete", step, step_result)
                return step_result

        # All router-driven or StepConfig-driven attempts exhausted without returning.
        # This can happen when the router always returns RETRY but max_attempts is reached.
        total_duration_ms = (time.monotonic() - step_start_mono) * 1000.0
        return StepResult(
            step_id=step.name,
            status=StepStatus.FAILED_FINAL,
            output=None,
            attempts=attempts,
            duration_ms=total_duration_ms,
            timestamp=step_start_timestamp,
        )

    def _dispatch_validation_failure(
        self,
        step: Step,
        validation_result: ValidationResult,
        attempt_num: int,
        max_attempts: int,
        step_start_mono: float,
        step_start_timestamp: datetime,
        attempts: list[AttemptRecord],
    ) -> StepResult | dict[str, object]:
        """Dispatch a validation failure via router or fallback retry logic.

        Used by both input and output validation failure paths. When a failure
        router is wired in, delegates to _handle_failure. When no router, applies
        StepConfig-based retry/exhaustion logic.

        Args:
            step: The step whose contract failed.
            validation_result: The failing ValidationResult.
            attempt_num: The current 1-based attempt number.
            max_attempts: Maximum attempts for this step.
            step_start_mono: Monotonic time at step start (for duration calc).
            step_start_timestamp: UTC datetime at step start.
            attempts: Running list of AttemptRecords for this step.

        Returns:
            A StepResult when the action is terminal, or a dict (retry_context)
            when the caller should retry.
        """
        if self._failure_router is not None:
            result_or_ctx = self._handle_failure(
                step, FailureType.VALIDATION, validation_result, attempt_num
            )
            return result_or_ctx

        # No router — use StepConfig-based retry/exhaustion
        if attempt_num < max_attempts:
            # Retry: build sanitized context from validation result
            validation_errors = [
                {"field": e.field, "expected": e.expected, "actual": e.actual}
                for e in validation_result.errors
            ]
            return sanitize_retry_context(
                step_output=None,
                exception=None,
                attempt=attempt_num,
                failure_type="validation",
                validation_errors=validation_errors,
            )

        # Exhausted — produce terminal failure
        total_duration_ms = (time.monotonic() - step_start_mono) * 1000.0
        return StepResult(
            step_id=step.name,
            status=StepStatus.FAILED_FINAL,
            output=None,
            attempts=attempts,
            duration_ms=total_duration_ms,
            timestamp=step_start_timestamp,
        )

    def _execute_foreach(self, step: Step) -> StepResult:
        """Fan out a foreach step over a collection from state.

        Reads the collection identified by step.config.foreach from state,
        validates it is a list or tuple (strings and dicts are rejected), then
        executes the step action once per item. Results are collected into a
        list and stored under the step's name.

        REQUIRE_ALL policy: any item failure makes the entire step FAILED_FINAL.
        ALLOW_PARTIAL policy: failed items produce None in the output; the step
        succeeds if at least one item succeeds.

        Args:
            step: The step with a foreach configuration.

        Returns:
            A StepResult with COMPLETED (all/some succeeded) or FAILED_FINAL.
        """
        foreach_key = step.config.foreach
        if foreach_key is None:
            raise ExecutionError(
                "_execute_foreach called with foreach=None",
                step_id=step.name,
            )

        start_timestamp = datetime.now(tz=UTC)
        start_mono = time.monotonic()

        # --- Resolve the collection from state ---
        try:
            collection = self._state.get(foreach_key)
        except StateError as exc:
            error_type, error_message = sanitize_exception(exc)
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            attempt = AttemptRecord(
                attempt_number=1,
                status=AttemptStatus.FAILURE,
                output=None,
                error_type=error_type,
                error_message=error_message,
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            return StepResult(
                step_id=step.name,
                status=StepStatus.FAILED_FINAL,
                output=None,
                attempts=[attempt],
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )

        # --- Validate collection type: must be list or tuple, not str/dict ---
        collection_type_name = type(collection).__name__
        if isinstance(collection, (str, dict)):
            error_msg = (
                f"foreach key {foreach_key!r} has type {collection_type_name!r}; "
                f"only list and tuple are valid foreach targets (strings and dicts are rejected)."
            )
            validation_exc = ExecutionError(error_msg, step_id=step.name)
            error_type, sanitized_msg = sanitize_exception(validation_exc)
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            attempt = AttemptRecord(
                attempt_number=1,
                status=AttemptStatus.FAILURE,
                output=None,
                error_type=error_type,
                error_message=sanitized_msg,
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            return StepResult(
                step_id=step.name,
                status=StepStatus.FAILED_FINAL,
                output=None,
                attempts=[attempt],
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )

        # --- Handle empty collection ---
        try:
            items: list[object] = list(collection)  # type: ignore[call-overload]
        except TypeError as exc:
            error_type, error_message = sanitize_exception(exc)
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            attempt = AttemptRecord(
                attempt_number=1,
                status=AttemptStatus.FAILURE,
                output=None,
                error_type=error_type,
                error_message=error_message,
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            return StepResult(
                step_id=step.name,
                status=StepStatus.FAILED_FINAL,
                output=None,
                attempts=[attempt],
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )

        if not items:
            # Empty collection → COMPLETED with empty list
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            self._state.set(step.name, [])
            attempt = AttemptRecord(
                attempt_number=1,
                status=AttemptStatus.SUCCESS,
                output=[],
                error_type=None,
                error_message=None,
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            step_result = StepResult(
                step_id=step.name,
                status=StepStatus.COMPLETED,
                output=[],
                attempts=[attempt],
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            self._emit_hook("on_step_complete", step, step_result)
            return step_result

        # --- Execute once per item ---
        outputs: list[object] = []
        sub_attempts: list[AttemptRecord] = []
        any_failure = False
        success_count = 0

        for idx, item in enumerate(items):
            sub_result = self._execute_with_retries(step, item=item, item_index=idx)
            sub_attempts.extend(sub_result.attempts)

            if sub_result.status == StepStatus.COMPLETED:
                outputs.append(sub_result.output)
                success_count += 1
            else:
                any_failure = True
                if step.config.foreach_policy == ForeachPolicy.REQUIRE_ALL:
                    # Stop immediately on first failure
                    break
                else:
                    # ALLOW_PARTIAL: record None for this item
                    outputs.append(None)

        policy = step.config.foreach_policy
        duration_ms = (time.monotonic() - start_mono) * 1000.0

        # Determine final status based on policy
        if policy == ForeachPolicy.REQUIRE_ALL and any_failure:
            step_result = StepResult(
                step_id=step.name,
                status=StepStatus.FAILED_FINAL,
                output=None,
                attempts=sub_attempts,
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            return step_result

        if policy == ForeachPolicy.ALLOW_PARTIAL and success_count == 0:
            # All items failed
            step_result = StepResult(
                step_id=step.name,
                status=StepStatus.FAILED_FINAL,
                output=None,
                attempts=sub_attempts,
                duration_ms=duration_ms,
                timestamp=start_timestamp,
            )
            return step_result

        # Success (all or partial)
        self._state.set(step.name, outputs)
        step_result = StepResult(
            step_id=step.name,
            status=StepStatus.COMPLETED,
            output=outputs,
            attempts=sub_attempts,
            duration_ms=duration_ms,
            timestamp=start_timestamp,
        )
        self._emit_hook("on_step_complete", step, step_result)
        return step_result

    def _build_context(
        self,
        step: Step,
        attempt: int,
        item: object,
        retry_context: dict[str, object] | None,
    ) -> StepContext:
        """Construct a StepContext for a step execution attempt.

        Resolves inputs from state (deep-copied via JSON), and provides a
        ScopedStateProxy when the step declares read_keys or write_keys.

        Args:
            step: The step being executed.
            attempt: 1-based attempt number.
            item: Current item for foreach sub-invocations (None otherwise).
            retry_context: Sanitized context from the previous failed attempt.

        Returns:
            A fully configured StepContext ready to pass to step.action.
        """
        inputs = self._resolve_inputs(step)

        # Provide scoped proxy when the step declares access boundaries
        from kairos.state import ScopedStateProxy  # local import avoids circular at module level

        state_view: StateStore | ScopedStateProxy
        if step.read_keys is not None or step.write_keys is not None:
            state_view = self._state.scoped(
                read_keys=step.read_keys,
                write_keys=step.write_keys,
            )
        else:
            state_view = self._state

        return StepContext(
            state=state_view,
            inputs=inputs,
            item=item,
            retry_context=retry_context,
            step_id=step.name,
            attempt=attempt,
        )

    def _resolve_inputs(self, step: Step) -> dict[str, object]:
        """Read dependency outputs from state and return deep copies.

        For each step in step.depends_on, reads the stored output from state
        and returns a deep copy via JSON round-trip. This ensures the step
        action cannot mutate shared state by modifying its inputs.

        Args:
            step: The step whose dependencies should be resolved.

        Returns:
            Dict mapping dependency step name to a deep copy of its output.
            Keys that are absent from state are omitted.
        """
        inputs: dict[str, object] = {}
        for dep_name in step.depends_on:
            try:
                value = self._state.get(dep_name)
                # JSON round-trip deep copy: prevents mutation of state data
                safe_copy = json.loads(json.dumps(value))
                inputs[dep_name] = safe_copy
            except (StateError, TypeError, ValueError):
                # Missing or non-serializable dep output — omit from inputs
                pass
        return inputs

    def _calculate_retry_delay(self, config: StepConfig, attempt: int) -> float:
        """Calculate the delay before the next retry attempt.

        Formula: base * backoff^attempt
        With jitter: delay * random.uniform(0.5, 1.5)

        Args:
            config: The StepConfig containing retry delay settings.
            attempt: The attempt number that just failed (1-based). The delay
                is calculated for the upcoming (attempt+1) retry.

        Returns:
            The delay in seconds. May be 0.0 when retry_delay is 0.
        """
        base = config.retry_delay * (config.retry_backoff**attempt)
        if config.retry_jitter:
            return base * random.uniform(0.5, 1.5)  # noqa: S311 — non-crypto jitter, by design
        return base

    def _invoke_action(self, step: Step, ctx: StepContext) -> object:
        """Invoke step.action(ctx) with optional timeout enforcement.

        When step.config.timeout is set, uses ThreadPoolExecutor to run the
        action in a worker thread and applies future.result(timeout=...).
        A TimeoutError is converted to an ExecutionError.

        Args:
            step: The step whose action to invoke.
            ctx: The StepContext to pass to the action.

        Returns:
            The return value of step.action(ctx).

        Raises:
            ExecutionError: When the step exceeds its configured timeout.
            Any exception raised by the step action propagates unchanged.
        """
        if step.config.timeout is None:
            return step.action(ctx)

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(step.action, ctx)
        try:
            return future.result(timeout=step.config.timeout)
        except FutureTimeoutError:
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise ExecutionError(
                f"Step '{step.name}' timed out after {step.config.timeout} seconds.",
                step_id=step.name,
            ) from None
        else:
            pool.shutdown(wait=False)

    def _emit_hook(self, method_name: str, *args: object, **kwargs: object) -> None:
        """Safely call *method_name* on every registered hook.

        Hook exceptions are caught and logged but never propagated — hooks must
        not crash the executor.

        Args:
            method_name: Name of the ExecutorHooks method to call.
            *args: Positional arguments to pass to the hook method.
            **kwargs: Keyword arguments to pass to the hook method.
        """
        for hook in self._hooks:
            try:
                method = getattr(hook, method_name)
                method(*args, **kwargs)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Hook %r raised an exception in %s; ignoring.",
                    type(hook).__name__,
                    method_name,
                )

    def _validate_contract(
        self,
        data: object,
        contract: object,
        step: Step,
        phase: str,
        attempt: int,
    ) -> ValidationResult | None:
        """Validate *data* against *contract* if validator and contract are configured.

        Emits on_validation_start before, on_validation_complete on pass, and
        on_validation_fail on failure. Validator crashes produce a failed
        ValidationResult (they are never allowed to propagate).

        Args:
            data: The data to validate (step inputs or output).
            contract: The contract to validate against. Must be a Schema instance;
                other types are silently skipped (returns None).
            step: The step being validated (for hook emission).
            phase: "input" or "output" — used in hooks and logging.
            attempt: 1-based attempt number for hook emission.

        Returns:
            A ValidationResult if validation ran, or None if it was skipped
            (no validator, no contract, or non-Schema contract type).
        """
        if self._validator is None or contract is None:
            return None

        # Local import to avoid circular dependency at module load time.
        from kairos.schema import Schema  # noqa: PLC0415

        if not isinstance(contract, Schema):
            return None

        self._emit_hook("on_validation_start", step, phase, attempt)

        try:
            result = self._validator.validate(data, contract)
        except Exception as exc:  # noqa: BLE001
            # SECURITY: validator must never crash the executor.
            # Produce a sanitized failure result — never re-raise.
            error_type, _ = sanitize_exception(exc)
            from kairos.enums import Severity  # noqa: PLC0415
            from kairos.schema import FieldValidationError  # noqa: PLC0415

            result = ValidationResult(
                valid=False,
                errors=[
                    FieldValidationError(
                        field="<internal>",
                        expected="no validator error",
                        actual=f"{error_type} raised",
                        message=f"Validator raised {error_type}.",
                        severity=Severity.ERROR,
                    )
                ],
            )

        if result.valid:
            self._emit_hook("on_validation_complete", step, phase, result)
        else:
            self._emit_hook("on_validation_fail", step, phase, result, attempt)

        return result

    def _handle_failure(
        self,
        step: Step,
        failure_type: FailureType,
        error: Exception | ValidationResult,
        attempt_num: int,
    ) -> StepResult | dict[str, object]:
        """Route a failure event through the failure router and dispatch the action.

        Translates the router's RecoveryDecision into either a terminal StepResult
        (for ABORT / SKIP / REPLAN / CUSTOM) or a retry_context dict (for RETRY).

        Args:
            step: The step that failed.
            failure_type: EXECUTION or VALIDATION.
            error: The exception or ValidationResult that caused the failure.
            attempt_num: 1-based attempt number that failed.

        Returns:
            A StepResult when the action is terminal (ABORT/SKIP/REPLAN/CUSTOM),
            or a dict (the retry_context) when the action is RETRY.
        """
        from kairos.failure import FailureEvent, FailurePolicy  # noqa: PLC0415

        now = datetime.now(tz=UTC)
        event = FailureEvent(
            step_id=step.name,
            failure_type=failure_type,
            error=error,
            attempt_number=attempt_num,
            timestamp=now,
        )

        step_policy = (
            step.failure_policy if isinstance(step.failure_policy, FailurePolicy) else None
        )

        from kairos.enums import FailureAction  # noqa: PLC0415

        assert self._failure_router is not None
        decision = self._failure_router.handle(event, step_policy=step_policy)

        match decision.action:
            case FailureAction.RETRY:
                # Return the retry context dict — caller continues the loop
                return decision.retry_context or {}

            case FailureAction.SKIP:
                self._emit_hook("on_step_skip", step, f"failure router: {decision.reason}")
                now_skip = datetime.now(tz=UTC)
                return StepResult(
                    step_id=step.name,
                    status=StepStatus.SKIPPED,
                    output=None,
                    attempts=[],
                    duration_ms=0.0,
                    timestamp=now_skip,
                )

            case _:
                # ABORT, REPLAN, CUSTOM — all are terminal in MVP
                now_fail = datetime.now(tz=UTC)
                return StepResult(
                    step_id=step.name,
                    status=StepStatus.FAILED_FINAL,
                    output=None,
                    attempts=[],
                    duration_ms=0.0,
                    timestamp=now_fail,
                )

    @staticmethod
    def _make_skip_result(step: Step, reason: str) -> StepResult:
        """Create a SKIPPED StepResult with no attempts (dependency cascade).

        Args:
            step: The step being skipped.
            reason: Human-readable skip reason.

        Returns:
            A StepResult with SKIPPED status and an empty attempts list.
        """
        now = datetime.now(tz=UTC)
        return StepResult(
            step_id=step.name,
            status=StepStatus.SKIPPED,
            output=None,
            attempts=[],
            duration_ms=0.0,
            timestamp=now,
        )
