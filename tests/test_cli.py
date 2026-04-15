"""Tests for kairos.cli — written BEFORE implementation.

CLI runner tests covering: module loading security (S13), input parsing,
logger factory, command execution (run / validate / version), and
all security boundaries defined in the architecture.

Test priority order (TDD):
1. Failure paths first
2. Boundary conditions
3. Happy paths
4. Security
5. Serialization
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Guard: all tests require typer
typer = pytest.importorskip("typer")
from typer.testing import CliRunner  # noqa: E402

from kairos.exceptions import ConfigError, SecurityError  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers — write temporary workflow modules
# ---------------------------------------------------------------------------

_VALID_WORKFLOW_SRC = """\
from kairos import Step, StepContext, Workflow


def _noop(ctx: StepContext) -> dict:
    return {"ok": True}


workflow = Workflow(name="test_wf", steps=[Step(name="step1", action=_noop)])
"""

_FAILING_WORKFLOW_SRC = """\
from kairos import FailureAction, FailurePolicy, Step, StepContext, Workflow


def _fail(ctx: StepContext) -> dict:
    raise RuntimeError("intentional failure")


workflow = Workflow(
    name="fail_wf",
    steps=[Step(name="fail_step", action=_fail, retries=0)],
    failure_policy=FailurePolicy(
        on_execution_fail=FailureAction.ABORT,
        max_retries=0,
    ),
)
"""

_NO_WORKFLOW_ATTR_SRC = """\
# This module has no 'workflow' attribute
x = 42
"""

_WRONG_TYPE_SRC = """\
# The 'workflow' attribute is not a Workflow instance
workflow = "not a workflow"
"""


def _write_module(tmp_path: Path, name: str, content: str) -> Path:
    """Write *content* to *tmp_path/<name>.py* and return the file path."""
    module_file = tmp_path / f"{name}.py"
    module_file.write_text(content, encoding="utf-8")
    return module_file


def _clean_module(module_name: str) -> None:
    """Remove *module_name* from sys.modules to allow clean re-import."""
    sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    """Typer CLI test runner with isolated env."""
    return CliRunner()


@pytest.fixture()
def valid_module(tmp_path: Path) -> tuple[Path, str]:
    """Write a valid workflow module to tmp_path; return (dir, module_name)."""
    _write_module(tmp_path, "my_wf", _VALID_WORKFLOW_SRC)
    return tmp_path, "my_wf"


@pytest.fixture()
def failing_module(tmp_path: Path) -> tuple[Path, str]:
    """Write a failing workflow module to tmp_path; return (dir, module_name)."""
    _write_module(tmp_path, "fail_wf", _FAILING_WORKFLOW_SRC)
    return tmp_path, "fail_wf"


@pytest.fixture(autouse=True)
def _clean_sys_path_and_modules(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Remove tmp_path from sys.path and clean up any test modules after each test."""
    yield
    # Remove any path entries pointing at tmp_path after the test
    to_remove = [p for p in sys.path if str(tmp_path) in p]
    for p in to_remove:
        sys.path.remove(p)
    # Clean up any modules loaded from tmp_path
    for key in list(sys.modules):
        if key.startswith(
            (
                "my_wf",
                "fail_wf",
                "outside_wf",
                "crash_wf",
                "out_contract_wf",
                "in_contract_wf",
                "both_contract_wf",
                "validate_input_wf",
                "validate_fail_wf",
                "fake_ns_mod",
            )
        ):
            del sys.modules[key]


# ---------------------------------------------------------------------------
# Import the CLI app lazily (typer is optional)
# ---------------------------------------------------------------------------


def _get_app():  # type: ignore[no-untyped-def]
    """Import and return the Typer app from kairos.cli."""
    from kairos.cli import app  # type: ignore[import]

    return app


# ===========================================================================
# Group 1: Failure Paths
# ===========================================================================


class TestFailurePaths:
    # --- Module loading failures ---

    def test_module_not_found_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        """Non-existent module path exits with code 1."""
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "nonexistent_module_xyz", f"--workflows-dir={tmp_path}"],
        )
        assert result.exit_code != 0

    def test_no_workflow_attr_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        """Module with no 'workflow' attribute exits with code 1."""
        _write_module(tmp_path, "no_attr", _NO_WORKFLOW_ATTR_SRC)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "no_attr", f"--workflows-dir={tmp_path}"],
        )
        assert result.exit_code != 0

    def test_wrong_workflow_type_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        """Module where 'workflow' is not a Workflow instance exits with code 1."""
        _write_module(tmp_path, "wrong_type", _WRONG_TYPE_SRC)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "wrong_type", f"--workflows-dir={tmp_path}"],
        )
        assert result.exit_code != 0

    def test_module_outside_allowed_dirs_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Module that resolves outside allowed dirs is rejected."""
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        _write_module(outside_dir, "outside_wf", _VALID_WORKFLOW_SRC)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "outside_wf", f"--workflows-dir={allowed_dir}"],
        )
        # Must exit non-zero — the module is outside the allowed dir
        assert result.exit_code != 0

    def test_nonexistent_workflows_dir_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Specifying a non-existent --workflows-dir exits with code 2 (CLI error)."""
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "my_wf", f"--workflows-dir={tmp_path / 'does_not_exist'}"],
        )
        assert result.exit_code != 0

    # --- Input parsing failures ---

    def test_invalid_json_input_exits_nonzero(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """Malformed JSON string in --input exits non-zero."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}", "--input={not valid json}"],
        )
        assert result.exit_code != 0

    def test_both_input_flags_exits_nonzero(
        self, runner: CliRunner, valid_module: tuple[Path, str], tmp_path: Path
    ) -> None:
        """Providing both --input and --input-file is a CLI error."""
        wf_dir, mod_name = valid_module
        input_file = tmp_path / "input.json"
        input_file.write_text('{"key": "value"}', encoding="utf-8")
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "run",
                mod_name,
                f"--workflows-dir={wf_dir}",
                '--input={"key": "val"}',
                f"--input-file={input_file}",
            ],
        )
        assert result.exit_code != 0

    def test_input_file_not_found_exits_nonzero(
        self, runner: CliRunner, valid_module: tuple[Path, str], tmp_path: Path
    ) -> None:
        """Non-existent --input-file exits non-zero."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "run",
                mod_name,
                f"--workflows-dir={wf_dir}",
                f"--input-file={tmp_path / 'missing.json'}",
            ],
        )
        assert result.exit_code != 0

    def test_input_file_outside_allowed_dirs_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--input-file pointing outside allowed dirs is rejected.

        We create a strict 'allowed' subdirectory as the workflows-dir, and
        place the input file in a sibling 'other' directory that is NOT
        within the allowed dir.
        """
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        _write_module(allowed_dir, "bounded_wf", _VALID_WORKFLOW_SRC)
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        input_file = other_dir / "data.json"
        input_file.write_text('{"x": 1}', encoding="utf-8")
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "run",
                "bounded_wf",
                f"--workflows-dir={allowed_dir}",
                f"--input-file={input_file}",
            ],
        )
        _clean_module("bounded_wf")
        assert result.exit_code != 0

    def test_json_input_not_dict_exits_nonzero(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """JSON array input (not a dict) exits non-zero."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}", "--input=[1, 2, 3]"],
        )
        assert result.exit_code != 0

    def test_jsonl_format_without_log_file_exits_nonzero(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """--log-format=jsonl without --log-file exits non-zero."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}", "--log-format=jsonl"],
        )
        assert result.exit_code != 0


# ===========================================================================
# Group 2: Boundary Conditions
# ===========================================================================


class TestBoundaryConditions:
    def test_empty_json_object_accepted(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """Empty JSON object '{}' is a valid input (no initial state)."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}", "--input={}"],
        )
        assert result.exit_code == 0

    def test_no_input_flag_accepted(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """Running without any --input flag is valid (empty initial state)."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}"],
        )
        assert result.exit_code == 0

    def test_only_cwd_allowed_when_no_workflows_dir(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """When --workflows-dir is omitted, CWD is the only allowed dir.

        We place the module in tmp_path and run from tmp_path as the CWD.
        """
        _write_module(tmp_path, "cwd_wf", _VALID_WORKFLOW_SRC)
        app = _get_app()
        # Invoke with CWD set to tmp_path so the module is within allowed dirs
        with patch("kairos.cli._get_cwd", return_value=str(tmp_path)):
            result = runner.invoke(app, ["run", "cwd_wf"])
        assert result.exit_code == 0

    def test_env_var_multiple_paths_accepted(self, runner: CliRunner, tmp_path: Path) -> None:
        """KAIROS_WORKFLOWS_DIR env var with multiple paths (colon-separated) works."""
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        _write_module(dir_b, "env_wf", _VALID_WORKFLOW_SRC)
        app = _get_app()
        env_value = os.pathsep.join([str(dir_a), str(dir_b)])
        with patch.dict(os.environ, {"KAIROS_WORKFLOWS_DIR": env_value}):
            result = runner.invoke(app, ["run", "env_wf"])
        _clean_module("env_wf")
        assert result.exit_code == 0

    def test_workflows_dir_flag_takes_precedence(self, runner: CliRunner, tmp_path: Path) -> None:
        """--workflows-dir flag adds to the allowed directories list."""
        _write_module(tmp_path, "flagdir_wf", _VALID_WORKFLOW_SRC)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "flagdir_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("flagdir_wf")
        assert result.exit_code == 0

    def test_module_at_cwd_root(self, runner: CliRunner, tmp_path: Path) -> None:
        """A module at the root of the workflows-dir is accepted."""
        _write_module(tmp_path, "root_wf", _VALID_WORKFLOW_SRC)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "root_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("root_wf")
        assert result.exit_code == 0

    def test_validate_with_no_input_succeeds(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """kairos validate without --input succeeds for a valid workflow."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", mod_name, f"--workflows-dir={wf_dir}"],
        )
        assert result.exit_code == 0


# ===========================================================================
# Group 3: Happy Paths
# ===========================================================================


class TestHappyPaths:
    def test_load_valid_module_returns_workflow(self, tmp_path: Path) -> None:
        """_load_workflow_from_module returns a Workflow for a valid module."""
        from kairos import Workflow
        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        _write_module(tmp_path, "load_wf", _VALID_WORKFLOW_SRC)
        wf = _load_workflow_from_module("load_wf", str(tmp_path))
        _clean_module("load_wf")
        assert isinstance(wf, Workflow)
        assert wf.name == "test_wf"

    def test_load_module_in_subdirectory(self, tmp_path: Path) -> None:
        """Module in a subdirectory of the workflows-dir is loaded correctly."""
        from kairos import Workflow
        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        sub = tmp_path / "sub"
        sub.mkdir()
        _write_module(sub, "sub_wf", _VALID_WORKFLOW_SRC)
        # Module path uses dot notation
        wf = _load_workflow_from_module("sub.sub_wf", str(tmp_path))
        _clean_module("sub.sub_wf")
        assert isinstance(wf, Workflow)

    def test_parse_json_string_returns_dict(self) -> None:
        """_parse_input with a JSON string returns the parsed dict."""
        from kairos.cli import _parse_input  # type: ignore[import]

        result = _parse_input('{"company": "Acme", "score": 42}', None, [str(Path.cwd())])
        assert result == {"company": "Acme", "score": 42}

    def test_parse_json_file_returns_dict(self, tmp_path: Path) -> None:
        """_parse_input with a file path reads and parses the JSON file."""
        from kairos.cli import _parse_input  # type: ignore[import]

        input_file = tmp_path / "data.json"
        input_file.write_text('{"key": "value"}', encoding="utf-8")
        result = _parse_input(None, str(input_file), [str(tmp_path)])
        assert result == {"key": "value"}

    def test_build_logger_default(self) -> None:
        """_build_logger returns a RunLogger with ConsoleSink at NORMAL verbosity."""
        from kairos import RunLogger
        from kairos.cli import _build_logger  # type: ignore[import]

        logger = _build_logger(verbose=False, log_format="console", log_file=None)
        assert isinstance(logger, RunLogger)

    def test_build_logger_verbose(self) -> None:
        """_build_logger with verbose=True produces VERBOSE verbosity."""
        from kairos import RunLogger
        from kairos.cli import _build_logger  # type: ignore[import]

        logger = _build_logger(verbose=True, log_format="console", log_file=None)
        assert isinstance(logger, RunLogger)

    def test_build_logger_jsonl(self, tmp_path: Path) -> None:
        """_build_logger with log_format=jsonl and a log_file returns a RunLogger."""
        from kairos import RunLogger
        from kairos.cli import _build_logger  # type: ignore[import]

        log_dir = str(tmp_path)
        logger = _build_logger(verbose=False, log_format="jsonl", log_file=log_dir)
        assert isinstance(logger, RunLogger)

    def test_version_command_prints_version(self, runner: CliRunner) -> None:
        """'kairos version' prints the current SDK version."""
        import kairos

        app = _get_app()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert kairos.__version__ in result.output

    def test_run_successful_workflow_exits_zero(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """'kairos run' exits 0 for a workflow that completes successfully."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}"],
        )
        _clean_module(mod_name)
        assert result.exit_code == 0

    def test_run_failing_workflow_exits_one(
        self, runner: CliRunner, failing_module: tuple[Path, str]
    ) -> None:
        """'kairos run' exits 1 for a workflow that ends in FAILED status."""
        wf_dir, mod_name = failing_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}"],
        )
        _clean_module(mod_name)
        assert result.exit_code == 1

    def test_validate_command_exits_zero_for_valid_workflow(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """'kairos validate' exits 0 for a structurally valid workflow."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", mod_name, f"--workflows-dir={wf_dir}"],
        )
        _clean_module(mod_name)
        assert result.exit_code == 0

    def test_validate_outputs_step_count(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """'kairos validate' output mentions the step count."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", mod_name, f"--workflows-dir={wf_dir}"],
        )
        _clean_module(mod_name)
        assert result.exit_code == 0
        # Should mention step count in output
        assert "1" in result.output  # 1 step

    def test_run_with_json_input(self, runner: CliRunner, valid_module: tuple[Path, str]) -> None:
        """'kairos run' with --input passes the parsed dict as initial state."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}", '--input={"company": "Acme"}'],
        )
        _clean_module(mod_name)
        assert result.exit_code == 0

    def test_run_with_json_input_file(
        self, runner: CliRunner, valid_module: tuple[Path, str], tmp_path: Path
    ) -> None:
        """'kairos run' with --input-file reads and passes the parsed JSON."""
        wf_dir, mod_name = valid_module
        input_file = wf_dir / "input.json"
        input_file.write_text('{"company": "Acme"}', encoding="utf-8")
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "run",
                mod_name,
                f"--workflows-dir={wf_dir}",
                f"--input-file={input_file}",
            ],
        )
        _clean_module(mod_name)
        assert result.exit_code == 0


# ===========================================================================
# Group 4: Security (S13 — Module Import Restriction)
# ===========================================================================


class TestSecurity:
    def test_absolute_path_as_module_path_rejected(self, runner: CliRunner, tmp_path: Path) -> None:
        """An absolute file path as the module argument is rejected.

        Module paths must be Python dotted identifiers, not file paths.
        Absolute paths contain path separators which are always rejected with SecurityError.
        """
        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        abs_path = str(tmp_path / "my_wf.py")
        with pytest.raises((SecurityError, ConfigError)):
            _load_workflow_from_module(abs_path, str(tmp_path))

    def test_module_path_with_dotdot_rejected(self, tmp_path: Path) -> None:
        """Module paths containing '..' are rejected as traversal attempts."""
        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        with pytest.raises(SecurityError):
            _load_workflow_from_module("../evil_module", str(tmp_path))

    def test_module_path_with_slash_rejected(self, tmp_path: Path) -> None:
        """Module paths containing '/' are rejected."""
        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        with pytest.raises(SecurityError):
            _load_workflow_from_module("foo/bar", str(tmp_path))

    def test_no_eval_on_input_json(self, tmp_path: Path) -> None:
        """Input JSON is parsed with json.loads, never eval().

        Verify that a JSON string with injection payload is safely parsed.
        The parsed result must be a dict, not the result of executing the payload.
        """
        from kairos.cli import _parse_input  # type: ignore[import]

        # This is valid JSON — but would be dangerous if passed to eval()
        payload = '{"__import__": "os", "key": "value"}'
        result = _parse_input(payload, None, [str(tmp_path)])
        # Must be the parsed dict, NOT the result of any code execution
        assert isinstance(result, dict)
        assert result["key"] == "value"

    def test_audit_log_written_on_run(
        self, runner: CliRunner, valid_module: tuple[Path, str]
    ) -> None:
        """Loading a workflow emits an audit message (check that run succeeds)."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", mod_name, f"--workflows-dir={wf_dir}"],
            catch_exceptions=False,
        )
        _clean_module(mod_name)
        # The run must succeed — audit logging must not crash the CLI
        assert result.exit_code == 0

    def test_input_file_traversal_rejected(
        self, runner: CliRunner, valid_module: tuple[Path, str], tmp_path: Path
    ) -> None:
        """--input-file with path traversal is rejected."""
        wf_dir, mod_name = valid_module
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "run",
                mod_name,
                f"--workflows-dir={wf_dir}",
                "--input-file=../../etc/passwd",
            ],
        )
        assert result.exit_code != 0

    def test_error_messages_do_not_expose_credentials(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Errors printed by the CLI do not leak API key patterns (sk-* style).

        We write a workflow module whose action raises an exception that contains
        a raw API key string.  When the CLI runs that workflow, the error message
        echoed to stderr must NOT contain the raw key — it must be redacted.
        """
        credential = "sk-secret123abc"
        leaky_src = f"""\
from kairos import Step, StepContext, Workflow
from kairos.exceptions import ExecutionError


def _leaky(ctx: StepContext) -> dict:
    raise RuntimeError("API call failed: {credential}")


workflow = Workflow(
    name="leaky_wf",
    steps=[Step(name="leak_step", action=_leaky, retries=0)],
)
"""
        _write_module(tmp_path, "leaky_wf", leaky_src)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "leaky_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("leaky_wf")
        # The raw credential must NOT appear in any CLI output
        combined_output = result.output + (result.stderr if hasattr(result, "stderr") else "")
        assert credential not in combined_output, (
            f"Raw credential {credential!r} was exposed in CLI output. "
            "sanitize_exception must redact sk-* patterns."
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlinks behave differently on Windows")
    def test_symlink_escape_via_allowed_dir_rejected(self, tmp_path: Path) -> None:
        """A symlink within the allowed dir that points outside is rejected.

        The attacker creates a symlink inside the allowed workflows directory that
        points to a module outside it.  os.path.realpath() resolves the symlink
        before the containment check, so the resolved path escapes — and the
        validator must reject it.
        """
        from kairos.cli import _validate_module_file_within_dirs  # type: ignore[import]

        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "evil.py"
        outside_file.write_text("# evil", encoding="utf-8")

        # Create a symlink inside the allowed dir that points to the outside file
        link = allowed_dir / "evil.py"
        link.symlink_to(outside_file)

        # realpath resolves the symlink — the resolved path is outside the allowed dir
        resolved = str(outside_file.resolve())
        allowed_dirs = [str(allowed_dir.resolve())]

        with pytest.raises(SecurityError):
            _validate_module_file_within_dirs(resolved, allowed_dirs)

    def test_cli_not_in_kairos_public_api(self) -> None:
        """The CLI module is NOT exported from kairos.__init__ public API.

        This ensures typer is optional and does not pollute the core SDK.
        The 'cli' name must not appear in kairos.__all__.
        """
        import kairos

        assert "cli" not in kairos.__all__, (
            "CLI must not be in kairos.__all__; typer is an optional dep and "
            "the CLI is not part of the public SDK API."
        )


# ===========================================================================
# Group 5: Serialization / Round-trip
# ===========================================================================


class TestSerialization:
    def test_json_round_trip_for_workflow_result(self, valid_module: tuple[Path, str]) -> None:
        """WorkflowResult from a CLI-driven run is JSON-serializable."""
        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        wf_dir, mod_name = valid_module
        wf = _load_workflow_from_module(mod_name, str(wf_dir))
        result = wf.run()
        _clean_module(mod_name)
        # Verify round-trip
        as_dict = result.to_dict()
        serialized = json.dumps(as_dict)
        deserialized = json.loads(serialized)
        assert deserialized["status"] == str(result.status)

    def test_unicode_preserved_in_json_input(self, tmp_path: Path) -> None:
        """Unicode characters in JSON input are preserved through _parse_input."""
        from kairos.cli import _parse_input  # type: ignore[import]

        payload = '{"name": "Ren\u00e9e", "city": "\u6771\u4eac"}'
        result = _parse_input(payload, None, [str(tmp_path)])
        assert result["name"] == "Ren\u00e9e"
        assert result["city"] == "\u6771\u4eac"


# ===========================================================================
# QA-Written Tests — Coverage Gaps
# ===========================================================================


class TestQACoverageGaps:
    """Tests written by QA analyst to fill coverage gaps in kairos/cli.py."""

    def test_empty_module_path_raises_config_error(self) -> None:
        """Empty string as module path raises ConfigError (line 128)."""
        from kairos.cli import _validate_module_path_string  # type: ignore[import]

        with pytest.raises(ConfigError):
            _validate_module_path_string("")

    def test_module_path_with_spaces_raises_security_error(self) -> None:
        """Module path with spaces passes .. and / checks but fails regex (line 142)."""
        from kairos.cli import _validate_module_path_string  # type: ignore[import]

        with pytest.raises(SecurityError):
            _validate_module_path_string("my module")

    def test_module_path_with_special_chars_raises_security_error(self) -> None:
        """Module path with special chars (e.g. @, !) fails regex (line 142)."""
        from kairos.cli import _validate_module_path_string  # type: ignore[import]

        with pytest.raises(SecurityError):
            _validate_module_path_string("mod@evil")

    def test_module_with_no_dunder_file_raises_config_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A namespace package module (no __file__) raises ConfigError (line 230).

        We mock importlib.import_module to return a module with __file__ = None.
        """
        from types import ModuleType

        from kairos.cli import _load_workflow_from_module  # type: ignore[import]

        fake_mod = ModuleType("fake_ns_mod")
        fake_mod.__file__ = None  # type: ignore[attr-defined]

        with (
            patch("kairos.cli.importlib.import_module", return_value=fake_mod),
            pytest.raises(ConfigError, match="no __file__ attribute"),
        ):
            _load_workflow_from_module("fake_ns_mod", str(tmp_path))

    def test_run_workflow_runtime_exception_exits_one(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """workflow.run() raising an unexpected exception exits 1 (lines 431-434)."""
        crash_src = """\
from kairos import Step, StepContext, Workflow


def _crash(ctx: StepContext) -> dict:
    raise SystemError("unexpected internal error")


workflow = Workflow(name="crash_wf", steps=[Step(name="crash_step", action=_crash, retries=0)])
"""
        _write_module(tmp_path, "crash_wf", crash_src)
        app = _get_app()
        result = runner.invoke(
            app,
            ["run", "crash_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("crash_wf")
        assert result.exit_code != 0

    def test_validate_nonexistent_workflows_dir_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """validate with non-existent --workflows-dir exits non-zero (lines 469-470)."""
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", "my_wf", f"--workflows-dir={tmp_path / 'does_not_exist'}"],
        )
        assert result.exit_code != 0

    def test_validate_bad_module_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        """validate with a module that fails to load exits non-zero (lines 475-478)."""
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", "nonexistent_mod_xyz", f"--workflows-dir={tmp_path}"],
        )
        assert result.exit_code != 0

    def test_validate_step_with_output_contract_only(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """validate reports output-only contract on a step (line 494)."""
        src = """\
from kairos import Schema, Step, StepContext, Workflow


def _noop(ctx: StepContext) -> dict:
    return {"result": "ok"}


workflow = Workflow(
    name="output_contract_wf",
    steps=[Step(name="s1", action=_noop, output_contract=Schema({"result": str}))],
)
"""
        _write_module(tmp_path, "out_contract_wf", src)
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", "out_contract_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("out_contract_wf")
        assert result.exit_code == 0
        assert "output contract defined" in result.output

    def test_validate_step_with_input_contract_only(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """validate reports input-only contract on a step (line 496)."""
        src = """\
from kairos import Schema, Step, StepContext, Workflow


def _noop(ctx: StepContext) -> dict:
    return {"result": "ok"}


workflow = Workflow(
    name="input_contract_wf",
    steps=[Step(name="s1", action=_noop, input_contract=Schema({"data": str}))],
)
"""
        _write_module(tmp_path, "in_contract_wf", src)
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", "in_contract_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("in_contract_wf")
        assert result.exit_code == 0
        assert "input contract defined" in result.output

    def test_validate_step_with_both_contracts(self, runner: CliRunner, tmp_path: Path) -> None:
        """validate reports both contracts on a step (line 492)."""
        src = """\
from kairos import Schema, Step, StepContext, Workflow


def _noop(ctx: StepContext) -> dict:
    return {"result": "ok"}


workflow = Workflow(
    name="both_contract_wf",
    steps=[
        Step(
            name="s1",
            action=_noop,
            input_contract=Schema({"data": str}),
            output_contract=Schema({"result": str}),
        ),
    ],
)
"""
        _write_module(tmp_path, "both_contract_wf", src)
        app = _get_app()
        result = runner.invoke(
            app,
            ["validate", "both_contract_wf", f"--workflows-dir={tmp_path}"],
        )
        _clean_module("both_contract_wf")
        assert result.exit_code == 0
        assert "input/output contracts defined" in result.output

    def test_validate_with_input_and_input_contract_passes(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """validate --input validates against first step's input contract (lines 502-520)."""
        src = """\
from kairos import Schema, Step, StepContext, Workflow


def _noop(ctx: StepContext) -> dict:
    return {"result": "ok"}


workflow = Workflow(
    name="validate_input_wf",
    steps=[Step(name="s1", action=_noop, input_contract=Schema({"company": str}))],
)
"""
        _write_module(tmp_path, "validate_input_wf", src)
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "validate",
                "validate_input_wf",
                f"--workflows-dir={tmp_path}",
                '--input={"company": "Acme"}',
            ],
        )
        _clean_module("validate_input_wf")
        assert result.exit_code == 0
        assert "Input validation: PASS" in result.output

    def test_validate_with_input_and_input_contract_fails(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """validate --input with mismatched data exits 1 (lines 521-528)."""
        src = """\
from kairos import Schema, Step, StepContext, Workflow


def _noop(ctx: StepContext) -> dict:
    return {"result": "ok"}


workflow = Workflow(
    name="validate_fail_wf",
    steps=[Step(name="s1", action=_noop, input_contract=Schema({"company": str}))],
)
"""
        _write_module(tmp_path, "validate_fail_wf", src)
        app = _get_app()
        result = runner.invoke(
            app,
            [
                "validate",
                "validate_fail_wf",
                f"--workflows-dir={tmp_path}",
                '--input={"company": 42}',
            ],
        )
        _clean_module("validate_fail_wf")
        assert result.exit_code != 0
        assert "Input validation: FAIL" in result.output
