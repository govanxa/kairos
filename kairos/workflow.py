"""Kairos workflow — top-level Workflow class that wires all SDK components together.

Provides the Workflow class: the primary developer-facing entry point for
defining and running contract-enforced, security-hardened AI agent workflows.

The Workflow class:
1. Validates name and steps at construction time (ConfigError / PlanError).
2. Builds a TaskGraph and validates its structure immediately.
3. Populates a SchemaRegistry from any step input/output contracts.
4. Exposes run() — a synchronous orchestration method that creates fresh
   StateStore, StructuralValidator, FailureRouter, and StepExecutor instances
   per run to guarantee full isolation between calls.
5. Exposes run_async() — a stub that raises NotImplementedError (MVP).
6. Provides to_dict() for JSON-safe serialization (omits callables and hooks).

Security contracts:
- sensitive_keys are forwarded to StateStore — matching keys are redacted in
  WorkflowResult.final_state, logs, and to_safe_dict(). Raw values accessible
  via state.get() inside step actions.
- run() creates a new StateStore per invocation — no state leaks between runs.
- to_dict() never includes callables, hooks, or sensitive_keys.
- Initial inputs are stored via StateStore.set() — non-serializable values raise
  StateError before any step runs.
"""

from __future__ import annotations

from kairos.exceptions import ConfigError
from kairos.executor import ExecutorHooks, StepExecutor, WorkflowResult
from kairos.failure import FailurePolicy, FailureRouter
from kairos.plan import TaskGraph
from kairos.schema import Schema, SchemaRegistry
from kairos.state import StateStore
from kairos.step import Step
from kairos.validators import StructuralValidator

# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class Workflow:
    """Top-level orchestrator for a contract-enforced AI agent workflow.

    Defines a named, ordered collection of Steps with dependency relationships,
    optional failure policies, validation contracts, and sensitive key handling.

    Construction validates name, steps, and graph structure eagerly — any
    PlanError or ConfigError surfaces at ``__init__`` time, not at run time.

    Each call to ``run()`` is fully isolated: a fresh StateStore, Validator,
    FailureRouter, and StepExecutor are created. State never leaks between runs.

    Args:
        name: Non-empty, non-whitespace workflow identifier.
        steps: Non-empty list of Step definitions.
        failure_policy: Optional workflow-level FailurePolicy. Used as the
            second level in the three-level policy hierarchy
            (step → workflow → KAIROS_DEFAULTS).
        hooks: Optional lifecycle hooks subscribers (ExecutorHooks instances).
        max_llm_calls: Hard circuit-breaker limit on total LLM invocations
            across all steps in a single run. Default: 50.
        sensitive_keys: Additional state key patterns to redact in
            WorkflowResult.final_state and logs. Combined with the SDK defaults
            (``DEFAULT_SENSITIVE_PATTERNS``).
        strict: Reserved for future stricter validation mode. Accepted at
            construction time but has no behavioural effect in MVP. Do not
            rely on this parameter — its semantics may change.
        metadata: Arbitrary JSON-serializable key-value metadata attached to
            the workflow (author, version, description, etc.).

    Raises:
        ConfigError: If name is empty/whitespace or steps is empty.
        PlanError: If the step graph is invalid (cycles, missing deps,
            duplicate names).
    """

    def __init__(
        self,
        name: str,
        steps: list[Step],
        *,
        failure_policy: FailurePolicy | None = None,
        hooks: list[ExecutorHooks] | None = None,
        max_llm_calls: int = 50,
        sensitive_keys: list[str] | None = None,
        strict: bool = False,
        metadata: dict[str, object] | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        # --- Validate name ---
        if not name or not name.strip():
            raise ConfigError(f"Workflow name must be a non-empty string, got {name!r}.")

        # --- Validate steps ---
        if not steps:
            raise ConfigError("Workflow requires at least one step. Got an empty steps list.")

        # --- Store private attributes ---
        self._name = name
        self._steps = list(steps)
        self._failure_policy = failure_policy
        self._hooks: list[ExecutorHooks] = hooks or []
        self._max_llm_calls = max_llm_calls
        self._sensitive_keys = sensitive_keys
        self._strict = strict
        self._metadata: dict[str, object] = dict(metadata) if metadata else {}
        self._max_concurrency = max_concurrency

        # --- Build TaskGraph (validates name, then validate() checks structure) ---
        self._graph = TaskGraph(name=name, steps=self._steps, metadata=self._metadata)

        # Validate graph structure — raise the first error immediately
        errors = self._graph.validate()
        if errors:
            raise errors[0]

        # --- Populate SchemaRegistry from step contracts ---
        self._registry = SchemaRegistry()
        for step in self._steps:
            input_contract = (
                step.input_contract if isinstance(step.input_contract, Schema) else None
            )
            output_contract = (
                step.output_contract if isinstance(step.output_contract, Schema) else None
            )
            if input_contract is not None or output_contract is not None:
                self._registry.register(
                    step_id=step.name,
                    input_schema=input_contract,
                    output_schema=output_contract,
                )

    # ------------------------------------------------------------------
    # Run methods
    # ------------------------------------------------------------------

    def run(self, initial_inputs: dict[str, object] | None = None) -> WorkflowResult:
        """Execute the workflow synchronously.

        Creates a fresh, isolated runtime environment for each call:
        - New StateStore (with sensitive key redaction configured)
        - New StructuralValidator
        - New FailureRouter (with workflow-level policy if provided)
        - New StepExecutor (with circuit breaker and hooks)

        Args:
            initial_inputs: Optional dict of key-value pairs merged into the
                fresh StateStore before any step runs. All values must be
                JSON-serializable — non-serializable values raise StateError
                before execution begins.

        Returns:
            A WorkflowResult with the final status, per-step results,
            duration, and the redacted final state.

        Raises:
            StateError: If any value in initial_inputs is not JSON-serializable.
            ExecutionError: If the LLM call circuit breaker triggers mid-run.
        """
        # Fresh StateStore per run — ensures full isolation between calls
        state = StateStore(sensitive_keys=self._sensitive_keys)

        # Merge initial inputs into state (StateStore.set validates serializability)
        if initial_inputs:
            for key, value in initial_inputs.items():
                state.set(key, value)

        # Wire up the components
        validator = StructuralValidator()
        failure_router = FailureRouter(workflow_policy=self._failure_policy)
        executor = StepExecutor(
            state=state,
            hooks=self._hooks,
            max_llm_calls=self._max_llm_calls,
            validator=validator,
            failure_router=failure_router,
            max_concurrency=self._max_concurrency,
        )

        return executor.run(self._graph)

    async def run_async(self, initial_inputs: dict[str, object] | None = None) -> WorkflowResult:
        """Execute the workflow asynchronously.

        Not implemented in MVP. Async support is planned for a future phase.

        Args:
            initial_inputs: Ignored — provided for API symmetry with run().

        Raises:
            NotImplementedError: Always. This method is a stub in MVP.
        """
        raise NotImplementedError(
            "run_async() is not implemented in MVP. Use run() for synchronous execution. "
            "Async support is planned for a future release."
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """Serialize the Workflow to a JSON-safe dict.

        Includes: name, graph (structural data only, via TaskGraph.to_dict()),
        metadata, failure_policy (if set), max_llm_calls, and strict.

        Excludes: hooks (not serializable), sensitive_keys (security — omitting
        the list prevents leaking what patterns are considered sensitive),
        and all callables (step actions, contracts as callables).

        Returns:
            A JSON-serializable dict. Guaranteed to round-trip through
            json.dumps / json.loads without error.
        """
        d: dict[str, object] = {
            "name": self._name,
            "graph": self._graph.to_dict(),
            "metadata": self._metadata,
            "max_llm_calls": self._max_llm_calls,
            "strict": self._strict,
            "failure_policy": self._failure_policy.to_dict() if self._failure_policy else None,
            "max_concurrency": self._max_concurrency,
        }
        return d

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """The workflow name.

        Returns:
            Non-empty string identifier for this workflow.
        """
        return self._name

    @property
    def steps(self) -> list[Step]:
        """A copy of the step list.

        Returns a shallow copy — mutating the returned list does not affect
        the workflow's internal step collection.

        Returns:
            List of Step objects as passed to the constructor.
        """
        return list(self._steps)

    @property
    def graph(self) -> TaskGraph:
        """The TaskGraph built from the workflow's steps.

        Returns:
            The TaskGraph used for execution and serialization.
        """
        return self._graph

    @property
    def registry(self) -> SchemaRegistry:
        """The SchemaRegistry populated from step input/output contracts.

        Returns:
            SchemaRegistry with all registered step contracts.
        """
        return self._registry

    @property
    def failure_policy(self) -> FailurePolicy | None:
        """The workflow-level FailurePolicy, or None if not configured.

        Returns:
            The FailurePolicy passed to the constructor, or None.
        """
        return self._failure_policy

    @property
    def max_concurrency(self) -> int | None:
        """The maximum concurrent parallel step count, or None for no explicit limit.

        When None, the executor derives an effective limit from the number of
        parallel steps in the graph (capped at 32). When set, the executor takes
        min(parallel_count, 32, max_concurrency) as the thread pool size.

        Returns:
            The max_concurrency value passed to the constructor, or None.
        """
        return self._max_concurrency

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        step_names = [s.name for s in self._steps]
        return f"Workflow(name={self._name!r}, steps={step_names!r})"
