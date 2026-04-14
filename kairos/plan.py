"""Kairos plan — TaskGraph for structured workflow execution.

Provides TaskGraph: a dataclass that holds an ordered list of Steps with
dependency metadata. Supports topological sort (Kahn's algorithm),
DFS-based cycle detection, structural serialization (to_dict / from_dict),
and query helpers (get_step, get_dependencies, get_dependents).

Security contracts:
- from_dict() NEVER reconstructs step actions (callables). Deserialized steps
  carry _noop_action, a placeholder that raises PlanError if called at runtime.
- from_dict() filters config dicts to known fields only — unknown keys are
  silently ignored. This prevents injection via unexpected key names.
- No eval(), exec(), pickle, or importlib.import_module on untrusted data.
- YAML loading elsewhere in the SDK uses yaml.safe_load() exclusively.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

from kairos.enums import ForeachPolicy
from kairos.exceptions import ConfigError, PlanError
from kairos.step import Step, StepConfig

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# DFS three-color markers for cycle detection
_WHITE = 0  # unvisited
_GRAY = 1  # in progress (on current DFS path)
_BLACK = 2  # fully explored

# Known StepConfig field names — used to filter from_dict config dicts.
_KNOWN_CONFIG_KEYS: frozenset[str] = frozenset(
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
# Placeholder action for deserialized steps
# ---------------------------------------------------------------------------


def _noop_action(ctx: object) -> None:
    """Placeholder for deserialized steps. Raises PlanError if called.

    Deserialized steps produced by TaskGraph.from_dict() carry this function
    as their action. If any code path accidentally tries to execute a
    deserialized step without rebinding its action, this raises loudly.

    Args:
        ctx: Ignored — present only to satisfy the step action signature.

    Raises:
        PlanError: Always. Deserialized steps have no bound action.
    """
    raise PlanError(
        "This step was deserialized and has no action bound. "
        "Re-bind the step's action before executing the workflow."
    )


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------


@dataclass
class TaskGraph:
    """An execution plan: an ordered collection of Steps with dependencies.

    TaskGraph stores steps and their dependency relationships. It validates
    the graph structure (duplicates, missing deps, cycles), sorts steps
    into executable order via topological sort (Kahn's algorithm), and
    serializes to/from JSON-safe dicts for persistence.

    Serialization contract:
        to_dict() produces a JSON-safe dict that omits callables (action,
        input_contract, output_contract, failure_policy, read_keys, write_keys).
        from_dict() reconstructs structural data only — deserialized steps
        always carry _noop_action, never original callables.

    Attributes:
        name: Non-empty identifier for this workflow plan.
        steps: Ordered list of Step definitions.
        metadata: Arbitrary JSON-serializable metadata (author, version, etc.).

    Raises:
        ConfigError: On construction if name is empty or whitespace-only.
    """

    name: str
    steps: list[Step]
    metadata: dict[str, object] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        """Validate that name is a non-empty string.

        Raises:
            ConfigError: If name is empty or whitespace-only.
        """
        if not self.name.strip():
            raise ConfigError(f"TaskGraph name must be a non-empty string, got {self.name!r}.")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[PlanError]:
        """Validate the graph structure and accumulate ALL errors.

        Checks performed (in order):
        1. Duplicate step names — each duplicate is a separate PlanError.
        2. Self-dependencies — a step that lists itself in depends_on.
        3. Missing dependency references — depends_on names not in the graph.
        4. Circular dependencies (DFS three-color) — only when no duplicates
           found, because duplicates would confuse the adjacency representation.

        Returns:
            A list of PlanError instances. Empty list means the graph is valid.
        """
        errors: list[PlanError] = []
        seen_names: set[str] = set()
        duplicate_names: set[str] = set()

        # --- Pass 1: duplicate names ---
        for step in self.steps:
            if step.name in seen_names:
                duplicate_names.add(step.name)
                errors.append(
                    PlanError(
                        f"Duplicate step name {step.name!r}.",
                        step_id=step.name,
                    )
                )
            seen_names.add(step.name)

        # --- Build name → step map for dependency checks ---
        # Only use unique names to avoid confusion from duplicates.
        step_map: dict[str, Step] = {}
        for step in self.steps:
            if step.name not in step_map:
                step_map[step.name] = step

        # --- Pass 2: self-dependencies and missing references ---
        for step in self.steps:
            seen_deps: set[str] = set()
            for dep in step.depends_on:
                if dep in seen_deps:
                    # Duplicate depends_on entry — not an error, skip
                    continue
                seen_deps.add(dep)

                if dep == step.name:
                    errors.append(
                        PlanError(
                            f"Step {step.name!r} depends on itself.",
                            step_id=step.name,
                        )
                    )
                elif dep not in step_map:
                    errors.append(
                        PlanError(
                            f"Step {step.name!r} depends on unknown step {dep!r}.",
                            step_id=step.name,
                        )
                    )

        # --- Pass 3: cycle detection (DFS three-color) ---
        # Skip if duplicates were found — the graph is already ill-formed.
        if not duplicate_names:
            cycle_errors = self._detect_cycles(step_map)
            errors.extend(cycle_errors)

        return errors

    def _detect_cycles(self, step_map: dict[str, Step]) -> list[PlanError]:
        """Run DFS three-color cycle detection on the dependency graph.

        Uses WHITE/GRAY/BLACK coloring. GRAY nodes are on the current DFS
        path — finding a GRAY node means a cycle is present.

        Args:
            step_map: Mapping from step name to Step (unique names only).

        Returns:
            A list of PlanError instances describing any cycles found.
        """
        color: dict[str, int] = {name: _WHITE for name in step_map}
        errors: list[PlanError] = []

        def dfs(node: str, path: list[str]) -> None:
            color[node] = _GRAY
            path.append(node)

            for dep in step_map[node].depends_on:
                # Skip deps that are not in step_map (already reported above)
                if dep not in step_map:
                    continue
                if dep == node:
                    # Self-dep already reported
                    continue

                if color[dep] == _GRAY:
                    # Found a back edge — reconstruct cycle path
                    cycle_start = path.index(dep)
                    cycle_path = path[cycle_start:] + [dep]
                    errors.append(
                        PlanError(
                            f"Circular dependency detected: {' → '.join(cycle_path)}",
                        )
                    )
                elif color[dep] == _WHITE:
                    dfs(dep, path)

            path.pop()
            color[node] = _BLACK

        for name in step_map:
            if color[name] == _WHITE:
                dfs(name, [])

        return errors

    # ------------------------------------------------------------------
    # Topological sort (Kahn's algorithm)
    # ------------------------------------------------------------------

    def topological_sort(self) -> list[Step]:
        """Return steps in dependency-respecting order (Kahn's BFS algorithm).

        Uses insertion-order tiebreaking for deterministic output: when multiple
        steps become available at the same time, they appear in the order they
        were defined in self.steps.

        Precondition: the graph should have no duplicate step names. Call
        validate() first to ensure the graph is structurally valid. Behavior
        is undefined for graphs with duplicate names.

        Returns:
            Ordered list of Step objects ready for sequential execution.

        Raises:
            PlanError: If the graph contains a cycle (result length != input length).
        """
        if not self.steps:
            return []

        # Map name → step, preserving insertion order for tie-breaking
        name_to_step: dict[str, Step] = {}
        for step in self.steps:
            if step.name not in name_to_step:
                name_to_step[step.name] = step

        # Compute in-degree from unique dependencies only
        in_degree: dict[str, int] = {name: 0 for name in name_to_step}
        for step in self.steps:
            seen_deps: set[str] = set()
            for dep in step.depends_on:
                if dep in seen_deps or dep not in name_to_step:
                    continue
                seen_deps.add(dep)
                in_degree[step.name] += 1

        # Seed queue with all zero-in-degree steps (insertion order)
        queue: deque[str] = deque(name for name in name_to_step if in_degree[name] == 0)

        result: list[Step] = []

        while queue:
            current_name = queue.popleft()
            result.append(name_to_step[current_name])

            # Decrease in-degree for all steps that depend on current_name
            # Maintain insertion order when adding newly unblocked steps
            for step in self.steps:
                unique_deps = set(step.depends_on)
                if current_name in unique_deps:
                    in_degree[step.name] -= 1
                    if in_degree[step.name] == 0:
                        queue.append(step.name)

        if len(result) != len(name_to_step):
            raise PlanError(
                "Circular dependency detected: topological sort did not consume all steps."
            )

        return result

    def execution_order(self) -> list[Step]:
        """Alias for topological_sort().

        Returns:
            Steps in dependency-respecting execution order.

        Raises:
            PlanError: If the graph contains a cycle.
        """
        return self.topological_sort()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_step(self, name: str) -> Step:
        """Return the Step with the given name.

        Args:
            name: The step name to look up.

        Returns:
            The matching Step object.

        Raises:
            PlanError: If no step with the given name exists.
        """
        for step in self.steps:
            if step.name == name:
                return step
        raise PlanError(
            f"Step {name!r} not found in TaskGraph {self.name!r}.",
            step_id=name,
        )

    def get_dependencies(self, name: str) -> list[Step]:
        """Return Step objects that the named step depends on.

        Args:
            name: The step name whose dependencies to retrieve.

        Returns:
            List of Step objects listed in the step's depends_on.

        Raises:
            PlanError: If no step with the given name exists.
        """
        target = self.get_step(name)  # raises PlanError if not found
        result: list[Step] = []
        seen: set[str] = set()
        for dep_name in target.depends_on:
            if dep_name in seen:
                continue
            seen.add(dep_name)
            for step in self.steps:
                if step.name == dep_name:
                    result.append(step)
                    break
        return result

    def get_dependents(self, name: str) -> list[Step]:
        """Return Step objects that depend on the named step.

        Args:
            name: The step name to look up.

        Returns:
            List of Step objects that list the named step in their depends_on.

        Raises:
            PlanError: If no step with the given name exists.
        """
        self.get_step(name)  # validate existence, raises PlanError if missing
        return [step for step in self.steps if name in step.depends_on]

    def get_ready_steps(self, completed: set[str], failed: set[str]) -> list[Step]:
        """Return steps whose dependencies are all completed and none have failed.

        A step is "ready" when:
        - It is not already processed (neither in completed nor failed).
        - All of its dependencies are in completed.
        - None of its dependencies are in failed.

        Steps are returned in the same order they appear in self.steps (insertion
        order), which provides deterministic scheduling.

        Args:
            completed: Set of step names that have completed successfully.
            failed: Set of step names that have failed (or been cascade-skipped).

        Returns:
            Ordered list of Step objects that are ready to execute.
        """
        processed = completed | failed
        ready: list[Step] = []
        for step in self.steps:
            if step.name in processed:
                continue
            deps = set(step.depends_on)
            if deps <= completed and not (deps & failed):
                ready.append(step)
        return ready

    def get_cascade_skip_steps(self, failed: set[str], completed: set[str]) -> list[Step]:
        """Return steps that should be cascade-skipped because a dependency has failed.

        A step is "cascade-skippable" when:
        - It is not already processed (neither in completed nor failed).
        - At least one of its direct dependencies is in the failed set.

        Note: this only returns *direct* dependency failures. For transitive
        cascading (A fails → B skipped → C skippable because B is now in
        failed), the caller must add cascade-skipped steps to the failed set
        and call this method again in a loop.

        Args:
            failed: Set of step names that have failed or been cascade-skipped.
            completed: Set of step names that have completed successfully.

        Returns:
            List of Step objects that should be cascade-skipped in this round.
        """
        processed = completed | failed
        skippable: list[Step] = []
        for step in self.steps:
            if step.name in processed:
                continue
            if any(dep in failed for dep in step.depends_on):
                skippable.append(step)
        return skippable

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """Serialize the TaskGraph to a JSON-safe dict.

        Omits callables and runtime-only fields: action, input_contract,
        output_contract, failure_policy, read_keys, write_keys.

        Returns:
            A dict with 'name', 'steps', and 'metadata'. The 'steps' list
            contains one dict per step with 'name', 'depends_on', and 'config'.
            All values are JSON-serializable (no callables, no custom types).
        """
        steps_data: list[dict[str, object]] = []
        for step in self.steps:
            cfg = step.config
            steps_data.append(
                {
                    "name": step.name,
                    "depends_on": list(step.depends_on),
                    "config": {
                        "retries": cfg.retries,
                        "timeout": cfg.timeout,
                        "foreach": cfg.foreach,
                        "foreach_policy": cfg.foreach_policy.value,
                        "parallel": cfg.parallel,
                        "max_concurrency": cfg.max_concurrency,
                        "retry_delay": cfg.retry_delay,
                        "retry_backoff": cfg.retry_backoff,
                        "retry_jitter": cfg.retry_jitter,
                        "validation_timeout": cfg.validation_timeout,
                    },
                }
            )
        return {
            "name": self.name,
            "steps": steps_data,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskGraph:
        """Reconstruct a TaskGraph from a plain dict (structural data only).

        Security contract: this method NEVER reconstructs step actions.
        All deserialized steps carry _noop_action as their action. Unknown
        keys in step dicts and config dicts are silently ignored — they are
        never evaluated or executed.

        Args:
            data: A dict as produced by to_dict() or equivalent. Must contain
                'name' (str) and 'steps' (list).

        Returns:
            A reconstructed TaskGraph. Steps carry _noop_action as their action.

        Raises:
            ConfigError: When 'name' or 'steps' keys are missing or have
                incorrect types.
            PlanError: When the reconstructed graph fails validation (cycles,
                missing deps, etc.).
        """
        # --- Validate top-level structure ---
        if "name" not in data:
            raise ConfigError("TaskGraph.from_dict: missing required key 'name'.")
        if "steps" not in data:
            raise ConfigError("TaskGraph.from_dict: missing required key 'steps'.")

        raw_name = data["name"]
        raw_steps = data["steps"]

        if not isinstance(raw_name, str):
            raise ConfigError(
                f"TaskGraph.from_dict: 'name' must be a string, got {type(raw_name).__name__!r}."
            )
        if not isinstance(raw_steps, list):
            raise ConfigError(
                f"TaskGraph.from_dict: 'steps' must be a list, got {type(raw_steps).__name__!r}."
            )

        raw_metadata = data.get("metadata", {})
        metadata: dict[str, object] = (
            dict(cast(dict[str, object], raw_metadata)) if isinstance(raw_metadata, dict) else {}
        )

        # --- Reconstruct steps ---
        steps: list[Step] = []
        for entry in cast(list[Any], raw_steps):  # type: ignore[redundant-cast]
            if not isinstance(entry, dict):
                raise ConfigError("TaskGraph.from_dict: each step entry must be a dict.")
            step_dict = cast(dict[str, Any], entry)

            step_name: Any = step_dict.get("name")
            if not isinstance(step_name, str):
                got = type(step_name).__name__
                raise ConfigError(
                    f"TaskGraph.from_dict: step 'name' must be a string, got {got!r}."
                )

            raw_depends_on: Any = step_dict.get("depends_on", [])
            if not isinstance(raw_depends_on, list):
                raise ConfigError(
                    f"TaskGraph.from_dict: step 'depends_on' must be a list for step {step_name!r}."
                )
            depends_on: list[str] = [
                str(d)
                for d in cast(list[Any], raw_depends_on)  # type: ignore[redundant-cast]
            ]

            raw_config: Any = step_dict.get("config", {})
            config_dict: dict[str, object] = (
                {
                    str(k): v
                    for k, v in cast(dict[str, Any], raw_config).items()
                    if k in _KNOWN_CONFIG_KEYS
                }
                if isinstance(raw_config, dict)
                else {}
            )

            # Reconstruct ForeachPolicy from string value
            if "foreach_policy" in config_dict:
                raw_policy = config_dict["foreach_policy"]
                try:
                    config_dict["foreach_policy"] = ForeachPolicy(str(raw_policy))
                except ValueError:
                    # Invalid enum value — fall back to default
                    config_dict.pop("foreach_policy")

            config = StepConfig(**config_dict)  # type: ignore[arg-type]

            # SECURITY: _noop_action is always used — never reconstruct callables
            steps.append(
                Step(
                    name=step_name,
                    action=_noop_action,
                    depends_on=depends_on,
                    config=config,
                )
            )

        # Build the graph — __post_init__ will validate name
        graph = cls(name=raw_name, steps=steps, metadata=metadata)

        # Validate structure — raise on any error
        errors = graph.validate()
        if errors:
            # Raise the first error; callers can call validate() for all errors
            raise errors[0]

        return graph

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"TaskGraph(name={self.name!r}, steps={[s.name for s in self.steps]!r})"
