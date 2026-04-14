"""Tests for kairos.plan — written BEFORE implementation.

Covers: TaskGraph construction, validate(), topological_sort(), execution_order(),
get_step(), get_dependencies(), get_dependents(), to_dict(), from_dict().

TDD priority order:
1. Failure paths (cycles, missing deps, duplicates, self-deps, from_dict errors)
2. Boundary conditions (empty graph, single step, linear chain, diamond, all independent)
3. Happy paths (basic sort, validate returns empty list, get_step/deps/dependents, alias)
4. Security (from_dict never reconstructs actions, no pickle/eval/exec, ignores unknown keys)
5. Serialization (to_dict/from_dict round-trip, JSON-serializable, config preserved, metadata)
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kairos.enums import ForeachPolicy
from kairos.exceptions import ConfigError, PlanError
from kairos.plan import TaskGraph, _noop_action  # pyright: ignore[reportPrivateUsage]
from kairos.step import Step, StepConfig

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _noop(ctx: object) -> None:
    """A minimal callable used in tests to satisfy Step action requirement."""


def _make_step(
    name: str,
    depends_on: list[str] | None = None,
    *,
    retries: int = 0,
    timeout: float | None = None,
    foreach: str | None = None,
    foreach_policy: ForeachPolicy = ForeachPolicy.REQUIRE_ALL,
    parallel: bool = False,
    max_concurrency: int | None = None,
    retry_delay: float = 0.0,
    retry_backoff: float = 1.0,
    retry_jitter: bool = True,
    validation_timeout: float = 30.0,
) -> Step:
    """Create a Step with the given name, dependencies, and config overrides."""
    config = StepConfig(
        retries=retries,
        timeout=timeout,
        foreach=foreach,
        foreach_policy=foreach_policy,
        parallel=parallel,
        max_concurrency=max_concurrency,
        retry_delay=retry_delay,
        retry_backoff=retry_backoff,
        retry_jitter=retry_jitter,
        validation_timeout=validation_timeout,
    )
    return Step(name=name, action=_noop, depends_on=depends_on or [], config=config)


@pytest.fixture
def linear_graph() -> TaskGraph:
    """Three-step linear chain: a → b → c."""
    steps = [
        _make_step("a"),
        _make_step("b", depends_on=["a"]),
        _make_step("c", depends_on=["b"]),
    ]
    return TaskGraph(name="linear", steps=steps)


@pytest.fixture
def diamond_graph() -> TaskGraph:
    """Diamond graph: a → b, a → c, b → d, c → d."""
    steps = [
        _make_step("a"),
        _make_step("b", depends_on=["a"]),
        _make_step("c", depends_on=["a"]),
        _make_step("d", depends_on=["b", "c"]),
    ]
    return TaskGraph(name="diamond", steps=steps)


@pytest.fixture
def independent_graph() -> TaskGraph:
    """Three independent steps with no dependencies."""
    steps = [_make_step("x"), _make_step("y"), _make_step("z")]
    return TaskGraph(name="independent", steps=steps)


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Tests for invalid graphs and construction errors — written FIRST."""

    def test_validate_detects_duplicate_step_names(self) -> None:
        """Duplicate step names produce a PlanError per duplicate."""
        steps = [_make_step("a"), _make_step("a"), _make_step("b")]
        graph = TaskGraph(name="dupe", steps=steps)
        errors = graph.validate()
        assert len(errors) >= 1
        assert any(isinstance(e, PlanError) for e in errors)

    def test_validate_detects_self_dependency(self) -> None:
        """A step that lists itself in depends_on is a self-dependency error."""
        steps = [_make_step("a", depends_on=["a"])]
        graph = TaskGraph(name="self_dep", steps=steps)
        errors = graph.validate()
        assert len(errors) >= 1
        assert any(e.step_id == "a" for e in errors)

    def test_validate_detects_missing_dependency_reference(self) -> None:
        """Referencing a step that does not exist raises PlanError."""
        steps = [_make_step("a", depends_on=["nonexistent"])]
        graph = TaskGraph(name="missing_dep", steps=steps)
        errors = graph.validate()
        assert len(errors) >= 1
        assert any(e.step_id == "a" for e in errors)

    def test_validate_detects_simple_cycle(self) -> None:
        """Two steps that depend on each other form a cycle."""
        steps = [
            _make_step("a", depends_on=["b"]),
            _make_step("b", depends_on=["a"]),
        ]
        graph = TaskGraph(name="cycle", steps=steps)
        errors = graph.validate()
        assert len(errors) >= 1
        assert any(isinstance(e, PlanError) for e in errors)

    def test_validate_detects_three_step_cycle(self) -> None:
        """Cycle spanning three steps: a → b → c → a."""
        steps = [
            _make_step("a", depends_on=["c"]),
            _make_step("b", depends_on=["a"]),
            _make_step("c", depends_on=["b"]),
        ]
        graph = TaskGraph(name="three_cycle", steps=steps)
        errors = graph.validate()
        assert len(errors) >= 1

    def test_validate_skips_cycle_detection_when_duplicates_present(self) -> None:
        """Cycle detection is skipped when duplicates exist (would confuse the graph)."""
        # Duplicate 'a' means cycle detection results would be unreliable.
        steps = [_make_step("a"), _make_step("a", depends_on=["b"]), _make_step("b")]
        graph = TaskGraph(name="dupe_with_cycle", steps=steps)
        errors = graph.validate()
        # Should get duplicate errors, not necessarily cycle errors
        assert len(errors) >= 1

    def test_topological_sort_raises_on_cycle(self) -> None:
        """topological_sort() raises PlanError when a cycle exists."""
        steps = [
            _make_step("a", depends_on=["b"]),
            _make_step("b", depends_on=["a"]),
        ]
        graph = TaskGraph(name="cycle", steps=steps)
        with pytest.raises(PlanError):
            graph.topological_sort()

    def test_get_step_raises_for_unknown_name(self) -> None:
        """get_step() raises PlanError when the name is not in the graph."""
        graph = TaskGraph(name="g", steps=[_make_step("a")])
        with pytest.raises(PlanError):
            graph.get_step("nonexistent")

    def test_get_dependencies_raises_for_unknown_name(self) -> None:
        """get_dependencies() raises PlanError when the step is not in the graph."""
        graph = TaskGraph(name="g", steps=[_make_step("a")])
        with pytest.raises(PlanError):
            graph.get_dependencies("nonexistent")

    def test_get_dependents_raises_for_unknown_name(self) -> None:
        """get_dependents() raises PlanError when the step is not in the graph."""
        graph = TaskGraph(name="g", steps=[_make_step("a")])
        with pytest.raises(PlanError):
            graph.get_dependents("nonexistent")

    def test_construction_rejects_empty_name(self) -> None:
        """TaskGraph raises ConfigError when name is empty string."""
        with pytest.raises(ConfigError):
            TaskGraph(name="", steps=[])

    def test_construction_rejects_whitespace_only_name(self) -> None:
        """TaskGraph raises ConfigError when name is only whitespace."""
        with pytest.raises(ConfigError):
            TaskGraph(name="   ", steps=[])

    def test_from_dict_raises_on_missing_name_key(self) -> None:
        """from_dict() raises ConfigError when 'name' key is absent."""
        with pytest.raises(ConfigError):
            TaskGraph.from_dict({"steps": []})

    def test_from_dict_raises_on_missing_steps_key(self) -> None:
        """from_dict() raises ConfigError when 'steps' key is absent."""
        with pytest.raises(ConfigError):
            TaskGraph.from_dict({"name": "g"})

    def test_from_dict_raises_when_name_not_a_string(self) -> None:
        """from_dict() raises ConfigError when 'name' is not a string."""
        with pytest.raises(ConfigError):
            TaskGraph.from_dict({"name": 123, "steps": []})

    def test_from_dict_raises_when_steps_not_a_list(self) -> None:
        """from_dict() raises ConfigError when 'steps' is not a list."""
        with pytest.raises(ConfigError):
            TaskGraph.from_dict({"name": "g", "steps": "not-a-list"})

    def test_from_dict_raises_on_invalid_graph(self) -> None:
        """from_dict() raises PlanError when the reconstructed graph has errors."""
        data: dict[str, Any] = {
            "name": "bad",
            "steps": [
                {"name": "a", "depends_on": ["nonexistent"], "config": {}},
            ],
        }
        with pytest.raises(PlanError):
            TaskGraph.from_dict(data)

    def test_validate_accumulates_all_errors(self) -> None:
        """validate() collects ALL errors, not just the first one."""
        steps = [
            _make_step("a", depends_on=["missing1"]),
            _make_step("b", depends_on=["missing2"]),
        ]
        graph = TaskGraph(name="multi_error", steps=steps)
        errors = graph.validate()
        assert len(errors) >= 2

    def test_validate_duplicate_reports_step_id(self) -> None:
        """PlanError from duplicate check includes the step_id attribute."""
        steps = [_make_step("dup"), _make_step("dup")]
        graph = TaskGraph(name="g", steps=steps)
        errors = graph.validate()
        assert any(e.step_id == "dup" for e in errors)

    def test_validate_missing_dep_reports_step_id(self) -> None:
        """PlanError from missing dep check includes the step_id of the offending step."""
        steps = [_make_step("a", depends_on=["ghost"])]
        graph = TaskGraph(name="g", steps=steps)
        errors = graph.validate()
        assert any(e.step_id == "a" for e in errors)


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge-case inputs — empty graph, single step, large chains."""

    def test_empty_steps_list_is_valid(self) -> None:
        """A graph with no steps has no validation errors."""
        graph = TaskGraph(name="empty", steps=[])
        assert graph.validate() == []

    def test_empty_steps_topological_sort_returns_empty_list(self) -> None:
        """topological_sort() on empty graph returns empty list."""
        graph = TaskGraph(name="empty", steps=[])
        assert graph.topological_sort() == []

    def test_single_step_graph(self) -> None:
        """Single step with no deps validates and sorts correctly."""
        graph = TaskGraph(name="single", steps=[_make_step("only")])
        assert graph.validate() == []
        order = graph.topological_sort()
        assert len(order) == 1
        assert order[0].name == "only"

    def test_linear_chain_correct_order(self, linear_graph: TaskGraph) -> None:
        """Three-step linear chain sorts to insertion order [a, b, c]."""
        order = linear_graph.topological_sort()
        names = [s.name for s in order]
        assert names == ["a", "b", "c"]

    def test_all_independent_preserves_insertion_order(self, independent_graph: TaskGraph) -> None:
        """Independent steps return in their insertion order."""
        order = independent_graph.topological_sort()
        names = [s.name for s in order]
        assert names == ["x", "y", "z"]

    def test_diamond_graph_a_before_d(self, diamond_graph: TaskGraph) -> None:
        """In a diamond, 'a' must come first and 'd' must come last."""
        order = diamond_graph.topological_sort()
        names = [s.name for s in order]
        assert names[0] == "a"
        assert names[-1] == "d"

    def test_diamond_graph_b_and_c_before_d(self, diamond_graph: TaskGraph) -> None:
        """In a diamond, both 'b' and 'c' appear before 'd'."""
        order = diamond_graph.topological_sort()
        names = [s.name for s in order]
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")

    def test_duplicate_depends_on_entries_handled(self) -> None:
        """Steps that list the same dependency twice are handled gracefully."""
        steps = [
            _make_step("a"),
            _make_step("b", depends_on=["a", "a"]),
        ]
        graph = TaskGraph(name="dup_dep", steps=steps)
        # Duplicates in depends_on do not cause errors
        assert graph.validate() == []
        order = graph.topological_sort()
        assert [s.name for s in order] == ["a", "b"]

    def test_get_dependencies_deduplicates_when_depends_on_has_duplicates(self) -> None:
        """get_dependencies() returns unique steps even when depends_on has duplicate entries."""
        steps = [
            _make_step("a"),
            _make_step("b", depends_on=["a", "a"]),
        ]
        graph = TaskGraph(name="dup_dep", steps=steps)
        deps = graph.get_dependencies("b")
        assert len(deps) == 1
        assert deps[0].name == "a"

    def test_get_dependencies_returns_empty_for_root(self) -> None:
        """A step with no dependencies returns an empty list from get_dependencies()."""
        graph = TaskGraph(name="g", steps=[_make_step("root")])
        assert graph.get_dependencies("root") == []

    def test_get_dependents_returns_empty_for_leaf(self, linear_graph: TaskGraph) -> None:
        """The last step in a chain has no dependents."""
        assert linear_graph.get_dependents("c") == []

    def test_from_dict_with_empty_steps(self) -> None:
        """from_dict() accepts a valid dict with an empty steps list."""
        graph = TaskGraph.from_dict({"name": "empty", "steps": []})
        assert graph.name == "empty"
        assert graph.steps == []

    def test_metadata_defaults_to_empty_dict(self) -> None:
        """metadata field defaults to empty dict when not provided."""
        graph = TaskGraph(name="g", steps=[])
        assert graph.metadata == {}


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    """Core functionality — creation, validation, sorting, accessors."""

    def test_task_graph_construction(self) -> None:
        """TaskGraph stores name and steps correctly."""
        steps = [_make_step("a"), _make_step("b")]
        graph = TaskGraph(name="myworkflow", steps=steps)
        assert graph.name == "myworkflow"
        assert len(graph.steps) == 2

    def test_validate_returns_empty_list_on_valid_graph(self, linear_graph: TaskGraph) -> None:
        """validate() returns an empty list when the graph is valid."""
        assert linear_graph.validate() == []

    def test_topological_sort_linear(self, linear_graph: TaskGraph) -> None:
        """Linear chain a→b→c sorts to [a, b, c]."""
        order = linear_graph.topological_sort()
        assert [s.name for s in order] == ["a", "b", "c"]

    def test_execution_order_is_alias_for_topological_sort(self, linear_graph: TaskGraph) -> None:
        """execution_order() returns same result as topological_sort()."""
        assert linear_graph.execution_order() == linear_graph.topological_sort()

    def test_get_step_returns_correct_step(self, linear_graph: TaskGraph) -> None:
        """get_step() returns the Step object with the matching name."""
        step = linear_graph.get_step("b")
        assert step.name == "b"

    def test_get_dependencies_returns_dep_steps(self, linear_graph: TaskGraph) -> None:
        """get_dependencies('c') returns the Step for 'b'."""
        deps = linear_graph.get_dependencies("c")
        assert len(deps) == 1
        assert deps[0].name == "b"

    def test_get_dependents_returns_dependent_steps(self, linear_graph: TaskGraph) -> None:
        """get_dependents('a') returns the Step for 'b'."""
        deps = linear_graph.get_dependents("a")
        assert len(deps) == 1
        assert deps[0].name == "b"

    def test_get_dependencies_diamond(self, diamond_graph: TaskGraph) -> None:
        """'d' in a diamond graph depends on both 'b' and 'c'."""
        deps = diamond_graph.get_dependencies("d")
        dep_names = {s.name for s in deps}
        assert dep_names == {"b", "c"}

    def test_get_dependents_diamond_a(self, diamond_graph: TaskGraph) -> None:
        """'a' in a diamond graph has dependents 'b' and 'c'."""
        deps = diamond_graph.get_dependents("a")
        dep_names = {s.name for s in deps}
        assert dep_names == {"b", "c"}

    def test_metadata_stored_correctly(self) -> None:
        """TaskGraph stores metadata dict as provided."""
        meta: dict[str, object] = {"author": "test", "version": 1}
        graph = TaskGraph(name="g", steps=[], metadata=meta)
        assert graph.metadata == meta

    def test_task_graph_returns_steps_in_definition_order(self) -> None:
        """graph.steps preserves insertion order from construction."""
        names = ["z", "y", "x"]
        steps = [_make_step(n) for n in names]
        graph = TaskGraph(name="g", steps=steps)
        assert [s.name for s in graph.steps] == names


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestSecurity:
    """Security constraints — from_dict never reconstructs actions, no eval/pickle/exec."""

    def test_from_dict_does_not_reconstruct_actions(self) -> None:
        """Deserialized steps have _noop_action, never original callables."""
        data: dict[str, Any] = {
            "name": "safe",
            "steps": [{"name": "step1", "depends_on": [], "config": {}}],
        }
        graph = TaskGraph.from_dict(data)
        step = graph.get_step("step1")
        # The action must be the _noop_action placeholder
        assert step.action is _noop_action

    def test_noop_action_raises_plan_error_when_called(self) -> None:
        """_noop_action raises PlanError if accidentally called at runtime."""
        with pytest.raises(PlanError):
            _noop_action(object())

    def test_from_dict_ignores_unknown_step_keys(self) -> None:
        """Unknown keys in step dicts are silently ignored (no crash, no eval)."""
        data: dict[str, Any] = {
            "name": "safe",
            "steps": [
                {
                    "name": "s",
                    "depends_on": [],
                    "config": {},
                    "action": "os.system('rm -rf /')",  # malicious key — must be ignored
                    "__import__": "evil",
                }
            ],
        }
        graph = TaskGraph.from_dict(data)
        step = graph.get_step("s")
        assert step.action is _noop_action

    def test_from_dict_ignores_unknown_config_keys(self) -> None:
        """Unknown keys in config dicts are silently ignored."""
        data: dict[str, Any] = {
            "name": "g",
            "steps": [
                {
                    "name": "s",
                    "depends_on": [],
                    "config": {
                        "retries": 2,
                        "evil_key": "payload",  # unknown — must be ignored
                    },
                }
            ],
        }
        graph = TaskGraph.from_dict(data)
        step = graph.get_step("s")
        assert step.config.retries == 2

    def test_plan_source_contains_no_eval(self) -> None:
        """The plan.py source module object must not expose eval in its globals/code."""
        import types

        import kairos.plan as plan_module

        # Check the module's compiled bytecode does not reference the 'eval' builtin
        # as a name load — this is more reliable than text scraping.
        # We verify by confirming 'eval' is not in the module's __dict__ as a function
        # and that the module does not call builtins.eval by checking co_names
        # across all code objects in the compiled module.
        def collect_names(code: types.CodeType) -> set[str]:
            names: set[str] = set(code.co_names)
            for const in code.co_consts:
                if isinstance(const, types.CodeType):
                    names.update(collect_names(const))
            return names

        loader = plan_module.__loader__  # type: ignore[attr-defined]
        all_names = collect_names(loader.get_code(plan_module.__name__))
        assert "eval" not in all_names, (
            "plan.py must not reference eval — forbidden on untrusted data"
        )

    def test_plan_source_contains_no_exec(self) -> None:
        """The plan.py source module object must not expose exec in its bytecode."""
        import types

        import kairos.plan as plan_module

        def collect_names(code: types.CodeType) -> set[str]:
            names: set[str] = set(code.co_names)
            for const in code.co_consts:
                if isinstance(const, types.CodeType):
                    names.update(collect_names(const))
            return names

        loader = plan_module.__loader__  # type: ignore[attr-defined]
        all_names = collect_names(loader.get_code(plan_module.__name__))
        assert "exec" not in all_names, (
            "plan.py must not reference exec — forbidden on untrusted data"
        )

    def test_plan_source_contains_no_pickle(self) -> None:
        """The plan.py source must not import pickle."""
        import inspect

        import kairos.plan as plan_module

        source = inspect.getsource(plan_module)
        assert "import pickle" not in source
        assert "pickle.loads" not in source

    def test_plan_source_contains_no_importlib(self) -> None:
        """The plan.py bytecode must not reference importlib.import_module."""
        import types

        import kairos.plan as plan_module

        def collect_names(code: types.CodeType) -> set[str]:
            names: set[str] = set(code.co_names)
            for const in code.co_consts:
                if isinstance(const, types.CodeType):
                    names.update(collect_names(const))
            return names

        loader = plan_module.__loader__  # type: ignore[attr-defined]
        all_names = collect_names(loader.get_code(plan_module.__name__))
        assert "import_module" not in all_names, (
            "plan.py must not reference import_module — forbidden on untrusted data"
        )

    def test_from_dict_with_injected_name_constructs_safely(self) -> None:
        """A name that looks like a path injection is stored as-is (no execution)."""
        data: dict[str, Any] = {
            "name": "../../etc/passwd",
            "steps": [],
        }
        # from_dict creates a TaskGraph — the name value is stored, not executed.
        # (Path sanitization is applied at the file-sink layer, not here.)
        graph = TaskGraph.from_dict(data)
        assert graph.name == "../../etc/passwd"


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict / from_dict round-trip, JSON-serializable output, config preservation."""

    def test_to_dict_is_json_serializable(self, linear_graph: TaskGraph) -> None:
        """to_dict() output round-trips through json.dumps without error."""
        d = linear_graph.to_dict()
        serialized = json.dumps(d)  # must not raise
        assert isinstance(serialized, str)

    def test_to_dict_contains_name_and_steps(self, linear_graph: TaskGraph) -> None:
        """to_dict() output contains 'name' and 'steps' keys."""
        d = linear_graph.to_dict()
        assert "name" in d
        assert "steps" in d
        assert d["name"] == "linear"

    def test_to_dict_steps_have_required_keys(self, linear_graph: TaskGraph) -> None:
        """Each step entry in to_dict() has 'name', 'depends_on', 'config'."""
        d = linear_graph.to_dict()
        for step_dict in d["steps"]:  # type: ignore[union-attr]
            assert "name" in step_dict
            assert "depends_on" in step_dict
            assert "config" in step_dict

    def test_to_dict_does_not_include_action(self, linear_graph: TaskGraph) -> None:
        """to_dict() must NOT include step actions (callables are not serializable)."""
        d = linear_graph.to_dict()
        for step_dict in d["steps"]:  # type: ignore[union-attr]
            assert "action" not in step_dict

    def test_to_dict_does_not_include_contracts(self, linear_graph: TaskGraph) -> None:
        """to_dict() must NOT include input_contract or output_contract."""
        d = linear_graph.to_dict()
        for step_dict in d["steps"]:  # type: ignore[union-attr]
            assert "input_contract" not in step_dict
            assert "output_contract" not in step_dict

    def test_to_dict_does_not_include_read_write_keys(self, linear_graph: TaskGraph) -> None:
        """to_dict() must NOT include read_keys or write_keys."""
        d = linear_graph.to_dict()
        for step_dict in d["steps"]:  # type: ignore[union-attr]
            assert "read_keys" not in step_dict
            assert "write_keys" not in step_dict

    def test_to_dict_does_not_include_failure_policy(self, linear_graph: TaskGraph) -> None:
        """to_dict() must NOT include failure_policy."""
        d = linear_graph.to_dict()
        for step_dict in d["steps"]:  # type: ignore[union-attr]
            assert "failure_policy" not in step_dict

    def test_round_trip_preserves_step_names(self, linear_graph: TaskGraph) -> None:
        """from_dict(to_dict()) preserves all step names."""
        restored = TaskGraph.from_dict(linear_graph.to_dict())
        original_names = [s.name for s in linear_graph.steps]
        restored_names = [s.name for s in restored.steps]
        assert original_names == restored_names

    def test_round_trip_preserves_graph_name(self, linear_graph: TaskGraph) -> None:
        """from_dict(to_dict()) preserves the graph name."""
        restored = TaskGraph.from_dict(linear_graph.to_dict())
        assert restored.name == linear_graph.name

    def test_round_trip_preserves_depends_on(self, diamond_graph: TaskGraph) -> None:
        """from_dict(to_dict()) preserves dependency relationships."""
        restored = TaskGraph.from_dict(diamond_graph.to_dict())
        original_d = diamond_graph.get_step("d")
        restored_d = restored.get_step("d")
        assert set(original_d.depends_on) == set(restored_d.depends_on)

    def test_round_trip_preserves_config_retries(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.retries."""
        steps = [_make_step("a", retries=3)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.retries == 3

    def test_round_trip_preserves_config_timeout(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.timeout."""
        steps = [_make_step("a", timeout=10.5)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.timeout == 10.5

    def test_round_trip_preserves_config_foreach(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.foreach."""
        steps = [_make_step("a", foreach="items")]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.foreach == "items"

    def test_round_trip_preserves_foreach_policy(self) -> None:
        """from_dict(to_dict()) preserves ForeachPolicy enum value."""
        steps = [_make_step("a", foreach_policy=ForeachPolicy.ALLOW_PARTIAL)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.foreach_policy == ForeachPolicy.ALLOW_PARTIAL

    def test_round_trip_preserves_metadata(self) -> None:
        """from_dict(to_dict()) preserves graph metadata."""
        meta: dict[str, object] = {"created_by": "test", "version": 42}
        graph = TaskGraph(name="g", steps=[], metadata=meta)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.metadata == meta

    def test_to_dict_config_contains_all_known_fields(self, linear_graph: TaskGraph) -> None:
        """Each step's config dict contains all expected StepConfig fields."""
        d = linear_graph.to_dict()
        step_dict = d["steps"][0]  # type: ignore[index]
        config = step_dict["config"]  # type: ignore[index]
        expected_keys = {
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
        assert expected_keys == set(config.keys())  # type: ignore[arg-type]

    def test_round_trip_deserialized_step_has_noop_action(self) -> None:
        """Deserialized steps always carry _noop_action, never original callable."""
        steps = [_make_step("s")]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("s").action is _noop_action

    def test_round_trip_preserves_retry_jitter(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.retry_jitter."""
        steps = [_make_step("a", retry_jitter=False)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.retry_jitter is False

    def test_round_trip_preserves_parallel(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.parallel."""
        steps = [_make_step("a", parallel=True)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.parallel is True

    def test_round_trip_preserves_retry_delay_and_backoff(self) -> None:
        """from_dict(to_dict()) preserves retry_delay and retry_backoff."""
        steps = [_make_step("a", retry_delay=1.5, retry_backoff=2.0)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.retry_delay == 1.5
        assert restored.get_step("a").config.retry_backoff == 2.0

    def test_round_trip_preserves_validation_timeout(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.validation_timeout."""
        steps = [_make_step("a", validation_timeout=60.0)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.validation_timeout == 60.0

    def test_round_trip_preserves_max_concurrency(self) -> None:
        """from_dict(to_dict()) preserves StepConfig.max_concurrency."""
        steps = [_make_step("a", max_concurrency=4)]
        graph = TaskGraph(name="g", steps=steps)
        restored = TaskGraph.from_dict(graph.to_dict())
        assert restored.get_step("a").config.max_concurrency == 4


# ---------------------------------------------------------------------------
# Group 6: get_ready_steps — concurrent scheduling helper
# ---------------------------------------------------------------------------


class TestGetReadySteps:
    """Tests for TaskGraph.get_ready_steps(completed, failed)."""

    def test_missing_dependency_not_in_ready(self) -> None:
        """Step with unsatisfied dependency is not returned as ready."""
        steps = [_make_step("a"), _make_step("b", depends_on=["a"])]
        graph = TaskGraph(name="g", steps=steps)
        ready = graph.get_ready_steps(completed=set(), failed=set())
        names = [s.name for s in ready]
        assert "b" not in names

    def test_failed_dependency_excludes_step(self) -> None:
        """Step with a failed dependency is never in the ready set."""
        steps = [_make_step("a"), _make_step("b", depends_on=["a"])]
        graph = TaskGraph(name="g", steps=steps)
        # 'a' is failed — 'b' should NOT be ready
        ready = graph.get_ready_steps(completed=set(), failed={"a"})
        names = [s.name for s in ready]
        assert "b" not in names

    def test_empty_graph_returns_empty(self) -> None:
        """get_ready_steps on empty graph returns []."""
        graph = TaskGraph(name="g", steps=[])
        assert graph.get_ready_steps(completed=set(), failed=set()) == []

    def test_all_steps_completed_returns_empty(self) -> None:
        """get_ready_steps when all steps are in completed returns []."""
        steps = [_make_step("a"), _make_step("b")]
        graph = TaskGraph(name="g", steps=steps)
        ready = graph.get_ready_steps(completed={"a", "b"}, failed=set())
        assert ready == []

    def test_single_step_no_deps_is_ready(self) -> None:
        """Single step with no dependencies is immediately ready."""
        graph = TaskGraph(name="g", steps=[_make_step("only")])
        ready = graph.get_ready_steps(completed=set(), failed=set())
        assert len(ready) == 1
        assert ready[0].name == "only"

    def test_diamond_dependency_ready_set(self, diamond_graph: TaskGraph) -> None:
        """In A -> B, A -> C, B+C -> D: after A completes, B and C are ready. D is not."""
        ready = diamond_graph.get_ready_steps(completed={"a"}, failed=set())
        names = {s.name for s in ready}
        assert "b" in names
        assert "c" in names
        assert "d" not in names
        assert "a" not in names

    def test_independent_steps_all_ready(self, independent_graph: TaskGraph) -> None:
        """Three steps with no deps: all three are ready initially."""
        ready = independent_graph.get_ready_steps(completed=set(), failed=set())
        names = {s.name for s in ready}
        assert names == {"x", "y", "z"}

    def test_linear_chain_one_ready_at_a_time(self, linear_graph: TaskGraph) -> None:
        """A -> B -> C: only A is ready initially. After A, only B. After B, only C."""
        ready_init = linear_graph.get_ready_steps(completed=set(), failed=set())
        assert [s.name for s in ready_init] == ["a"]

        ready_after_a = linear_graph.get_ready_steps(completed={"a"}, failed=set())
        assert [s.name for s in ready_after_a] == ["b"]

        ready_after_b = linear_graph.get_ready_steps(completed={"a", "b"}, failed=set())
        assert [s.name for s in ready_after_b] == ["c"]

    def test_ready_steps_insertion_order(self) -> None:
        """Ready steps are returned in the same order as self.steps."""
        steps = [_make_step("z"), _make_step("y"), _make_step("x")]
        graph = TaskGraph(name="g", steps=steps)
        ready = graph.get_ready_steps(completed=set(), failed=set())
        assert [s.name for s in ready] == ["z", "y", "x"]

    def test_step_already_in_failed_not_in_ready(self) -> None:
        """Step that is already in the failed set is not returned as ready."""
        steps = [_make_step("a"), _make_step("b")]
        graph = TaskGraph(name="g", steps=steps)
        # 'a' is in failed — it should NOT appear in ready
        ready = graph.get_ready_steps(completed=set(), failed={"a"})
        names = [s.name for s in ready]
        assert "a" not in names


# ---------------------------------------------------------------------------
# Group 7: get_cascade_skip_steps — cascade failure helper
# ---------------------------------------------------------------------------


class TestGetCascadeSkipSteps:
    """Tests for TaskGraph.get_cascade_skip_steps(failed, completed)."""

    def test_step_with_failed_dep_is_skippable(self) -> None:
        """Step whose dependency failed should be in cascade skip list."""
        steps = [_make_step("a"), _make_step("b", depends_on=["a"])]
        graph = TaskGraph(name="g", steps=steps)
        skippable = graph.get_cascade_skip_steps(failed={"a"}, completed=set())
        names = [s.name for s in skippable]
        assert "b" in names

    def test_step_with_all_deps_completed_not_skippable(self) -> None:
        """Step whose deps are all completed is NOT in cascade skip list."""
        steps = [_make_step("a"), _make_step("b", depends_on=["a"])]
        graph = TaskGraph(name="g", steps=steps)
        skippable = graph.get_cascade_skip_steps(failed=set(), completed={"a"})
        names = [s.name for s in skippable]
        assert "b" not in names

    def test_transitive_cascade(self) -> None:
        """A fails -> B skippable -> C (depends on B) also skippable once B added to failed set."""
        steps = [
            _make_step("a"),
            _make_step("b", depends_on=["a"]),
            _make_step("c", depends_on=["b"]),
        ]
        graph = TaskGraph(name="g", steps=steps)
        # A failed, B should be skippable
        skippable_round1 = graph.get_cascade_skip_steps(failed={"a"}, completed=set())
        names1 = [s.name for s in skippable_round1]
        assert "b" in names1

        # Now B is also failed (cascade-skipped) — C should be skippable
        skippable_round2 = graph.get_cascade_skip_steps(failed={"a", "b"}, completed=set())
        names2 = [s.name for s in skippable_round2]
        assert "c" in names2

    def test_no_failed_returns_empty(self) -> None:
        """When no steps have failed, cascade skip list is empty."""
        steps = [_make_step("a"), _make_step("b", depends_on=["a"])]
        graph = TaskGraph(name="g", steps=steps)
        skippable = graph.get_cascade_skip_steps(failed=set(), completed=set())
        assert skippable == []

    def test_already_processed_step_excluded(self) -> None:
        """Steps already in completed or failed are not returned as skippable."""
        steps = [_make_step("a"), _make_step("b", depends_on=["a"])]
        graph = TaskGraph(name="g", steps=steps)
        # 'b' is already in completed — should not be in skippable list
        skippable = graph.get_cascade_skip_steps(failed={"a"}, completed={"b"})
        names = [s.name for s in skippable]
        assert "b" not in names

    def test_from_dict_raises_when_step_entry_not_dict(self) -> None:
        """from_dict() raises ConfigError when a step list entry is not a dict."""
        data: dict[str, Any] = {"name": "g", "steps": ["not-a-dict"]}
        with pytest.raises(ConfigError):
            TaskGraph.from_dict(data)

    def test_from_dict_raises_when_step_name_not_string(self) -> None:
        """from_dict() raises ConfigError when a step's name field is not a string."""
        data: dict[str, Any] = {
            "name": "g",
            "steps": [{"name": 42, "depends_on": [], "config": {}}],
        }
        with pytest.raises(ConfigError):
            TaskGraph.from_dict(data)

    def test_from_dict_raises_when_depends_on_not_list(self) -> None:
        """from_dict() raises ConfigError when step's depends_on is not a list."""
        data: dict[str, Any] = {
            "name": "g",
            "steps": [{"name": "s", "depends_on": "not-a-list", "config": {}}],
        }
        with pytest.raises(ConfigError):
            TaskGraph.from_dict(data)

    def test_from_dict_ignores_invalid_foreach_policy_value(self) -> None:
        """from_dict() falls back to default ForeachPolicy for unrecognized enum values."""
        data: dict[str, Any] = {
            "name": "g",
            "steps": [
                {
                    "name": "s",
                    "depends_on": [],
                    "config": {"foreach_policy": "invalid_value"},
                }
            ],
        }
        graph = TaskGraph.from_dict(data)
        # Falls back to default — REQUIRE_ALL
        assert graph.get_step("s").config.foreach_policy == ForeachPolicy.REQUIRE_ALL

    def test_repr_contains_name_and_step_names(self) -> None:
        """TaskGraph.__repr__() includes the graph name and step names."""
        graph = TaskGraph(name="mygraph", steps=[_make_step("s1"), _make_step("s2")])
        r = repr(graph)
        assert "mygraph" in r
        assert "s1" in r
        assert "s2" in r
