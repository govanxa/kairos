"""Kairos CLI — command-line runner for workflow execution and validation.

Provides three commands for v0.4.1:
- ``kairos run <module>``      Execute a workflow module.
- ``kairos validate <module>`` Dry-run plan and contract validation.
- ``kairos version``           Print the Kairos SDK version.

Security contracts (S13):
- Module paths are validated with a strict regex before import_module() is called.
- The resolved module.__file__ is verified to lie within allowed directories via
  os.path.realpath() containment checks.
- --input JSON is parsed with json.loads() only — never eval(), exec(), or compile().
- --input-file paths are validated to exist within allowed directories.
- Absolute paths and paths containing '..' or '/' are rejected.
- An audit log line is emitted to stderr on every module load.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated, cast

import typer

import kairos as _kairos_sdk
from kairos.enums import LogVerbosity, WorkflowStatus
from kairos.exceptions import ConfigError, SecurityError
from kairos.logger import ConsoleSink, JSONLinesSink, RunLogger
from kairos.security import sanitize_exception
from kairos.workflow import Workflow

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Only allow Python dotted-identifier module paths.
# Rejects: absolute paths, paths with / or \, spaces, .., and other non-identifier chars.
_MODULE_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")

# Environment variable for specifying additional allowed workflow directories.
_ENV_VAR = "KAIROS_WORKFLOWS_DIR"

# ---------------------------------------------------------------------------
# Typer app — only constructed when typer is available
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="kairos",
    help="Kairos -- Security-hardened AI agent workflow SDK.",
    no_args_is_help=True,
    add_completion=False,
)

# ---------------------------------------------------------------------------
# Internal helpers — testable as pure functions
# ---------------------------------------------------------------------------


def _get_cwd() -> str:
    """Return the current working directory.

    Extracted as a separate function so tests can patch it cleanly.

    Returns:
        The absolute path of the current working directory.
    """
    return os.getcwd()


def _resolve_allowed_dirs(workflows_dir: str | None) -> list[str]:
    """Build the list of canonicalized allowed directory paths.

    Always includes the current working directory.  If *workflows_dir* is
    provided, it is added after CWD.  If the ``KAIROS_WORKFLOWS_DIR``
    environment variable is set it is split on ``os.pathsep`` and each
    entry is added in order.

    Args:
        workflows_dir: Optional directory supplied via ``--workflows-dir``.

    Returns:
        List of canonicalized (via ``os.path.realpath``) directory strings,
        starting with CWD.
    """
    dirs: list[str] = [os.path.realpath(_get_cwd())]

    # --workflows-dir flag
    if workflows_dir is not None:
        dirs.append(os.path.realpath(workflows_dir))

    # KAIROS_WORKFLOWS_DIR environment variable (colon/semicolon separated)
    env_val = os.environ.get(_ENV_VAR, "")
    if env_val:
        for entry in env_val.split(os.pathsep):
            entry = entry.strip()
            if entry:
                dirs.append(os.path.realpath(entry))

    return dirs


def _validate_module_path_string(module_path: str) -> None:
    """Validate that *module_path* is a safe Python dotted identifier.

    Rejects any path containing ``..``, ``/``, ``\\``, spaces, or characters
    not allowed in Python module names.

    Args:
        module_path: The raw module path argument from the CLI.

    Raises:
        SecurityError: If the path contains suspicious characters.
        ConfigError: If the path is empty or does not match the expected format.
    """
    if not module_path:
        raise ConfigError("Module path must not be empty.")

    # Hard-reject traversal patterns first (before the regex check)
    if ".." in module_path:
        raise SecurityError(
            f"Module path {module_path!r} contains '..'. "
            "Only Python dotted identifiers are allowed."
        )
    if "/" in module_path or "\\" in module_path:
        raise SecurityError(
            f"Module path {module_path!r} contains a path separator. "
            "Use Python dotted notation (e.g. 'my_workflows.analysis')."
        )
    if not _MODULE_PATH_RE.match(module_path):
        raise SecurityError(
            f"Module path {module_path!r} contains characters not allowed in "
            "Python module names. Only [a-zA-Z0-9_.] and leading [a-zA-Z_] are permitted."
        )


def _validate_module_file_within_dirs(module_file: str, allowed_dirs: list[str]) -> None:
    """Verify that *module_file* lies within one of *allowed_dirs*.

    Args:
        module_file: Canonicalized absolute path of the loaded module's __file__.
        allowed_dirs: List of canonicalized allowed directory paths.

    Raises:
        SecurityError: If *module_file* is not within any allowed directory.
    """
    for allowed in allowed_dirs:
        # A file is "within" a dir if its path starts with dir + separator
        if module_file.startswith(allowed + os.sep) or module_file == allowed:
            return
    raise SecurityError(
        "Module file is outside allowed directories. "
        "Use --workflows-dir or KAIROS_WORKFLOWS_DIR to expand the allowed set."
    )


class _SuppressValueError:
    """Context manager that suppresses ValueError (for sys.path.remove failures)."""

    def __enter__(self) -> _SuppressValueError:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        return exc_type is ValueError


def _load_workflow_from_module(module_path: str, workflows_dir: str | None) -> Workflow:
    """Load and return the ``workflow`` object from a Python module path.

    Security steps (S13):
    1. Validate the module path string (regex + traversal rejection).
    2. Resolve allowed directories.
    3. Add the first allowed directory to sys.path temporarily.
    4. importlib.import_module() the module.
    5. Canonicalize module.__file__ and verify it is within allowed dirs.
    6. getattr(module, "workflow") — ConfigError if missing.
    7. isinstance check — ConfigError if not a Workflow.
    8. Log the resolved path to stderr as audit trail.

    Args:
        module_path: Python dotted module identifier (e.g. ``"my_wf.analysis"``).
        workflows_dir: Optional extra directory to allow, or ``None``.

    Returns:
        The ``Workflow`` instance found in the module.

    Raises:
        SecurityError: If the path is unsafe or the module file is outside allowed dirs.
        ConfigError: If the module has no ``workflow`` attribute or it is not a Workflow.
        ModuleNotFoundError: If the module cannot be found on sys.path.
    """
    # Step 1 — validate the module path string
    _validate_module_path_string(module_path)

    # Step 2 — resolve allowed directories
    allowed_dirs = _resolve_allowed_dirs(workflows_dir)

    # Step 3 — temporarily add each allowed dir to sys.path for import
    added_paths: list[str] = []
    for d in allowed_dirs:
        if d not in sys.path:
            sys.path.insert(0, d)
            added_paths.append(d)

    try:
        # Step 4 — import
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        raise
    finally:
        # Clean added paths whether import succeeded or not
        for d in added_paths:
            with _SuppressValueError():
                sys.path.remove(d)

    # Step 5 — verify module file location
    raw_file = getattr(module, "__file__", None)
    if raw_file is None:
        raise ConfigError(
            f"Module {module_path!r} has no __file__ attribute. "
            "Only file-based modules are supported."
        )
    module_file = os.path.realpath(raw_file)
    _validate_module_file_within_dirs(module_file, allowed_dirs)

    # Step 6 & 7 — get the workflow attribute
    workflow_obj = getattr(module, "workflow", None)
    if workflow_obj is None:
        raise ConfigError(
            f"Module {module_path!r} has no 'workflow' attribute. "
            "Define a top-level variable: workflow = Workflow(...)"
        )
    if not isinstance(workflow_obj, Workflow):
        raise ConfigError(
            f"Module {module_path!r}: 'workflow' must be a kairos.Workflow instance, "
            f"got {type(workflow_obj).__name__!r}."
        )

    # Step 8 — audit log (dim gray if terminal, plain if piped)
    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        print(  # noqa: T20
            f"\033[2mLoading workflow from: {module_file}\033[0m",
            file=sys.stderr,
        )
    else:
        print(f"Loading workflow from: {module_file}", file=sys.stderr)  # noqa: T20

    return workflow_obj


def _parse_input(
    input_str: str | None,
    input_file: str | None,
    allowed_dirs: list[str],
) -> dict[str, object]:
    """Parse JSON workflow input from a string or file.

    Uses ``json.loads()`` exclusively — never ``eval()``.

    Args:
        input_str: Raw JSON string from ``--input``, or ``None``.
        input_file: Path to a JSON file from ``--input-file``, or ``None``.
        allowed_dirs: Canonicalized allowed directories — input_file must be within these.

    Returns:
        Parsed dict.  Empty dict ``{}`` if both arguments are ``None``.

    Raises:
        ConfigError: If both are provided, if JSON is invalid, or if the
            parsed value is not a dict.
        SecurityError: If *input_file* is outside allowed dirs.
        FileNotFoundError: If *input_file* does not exist.
    """
    if input_str is not None and input_file is not None:
        raise ConfigError("Provide --input OR --input-file, not both.")

    if input_str is None and input_file is None:
        return {}

    raw: str
    if input_file is not None:
        file_path = os.path.realpath(input_file)
        # Verify within allowed dirs
        try:
            _validate_module_file_within_dirs(file_path, allowed_dirs)
        except SecurityError as exc:
            raise SecurityError(
                f"--input-file {input_file!r} is outside allowed directories. "
                "Use --workflows-dir or KAIROS_WORKFLOWS_DIR to expand the allowed set."
            ) from exc
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"--input-file not found: {input_file!r}")
        raw = Path(file_path).read_text(encoding="utf-8")
    else:
        raw = input_str  # type: ignore[assignment]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON input: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(f"JSON input must be a dict (object), got {type(parsed).__name__!r}.")

    return cast(dict[str, object], parsed)


def _build_logger(
    verbose: bool,
    log_format: str,
    log_file: str | None,
) -> RunLogger:
    """Construct a RunLogger with the appropriate sinks.

    Args:
        verbose: When True, use VERBOSE verbosity; otherwise NORMAL.
        log_format: Either ``"console"`` or ``"jsonl"``.
        log_file: Base directory for the JSONL sink.  Required when
            *log_format* is ``"jsonl"``.

    Returns:
        A configured RunLogger.

    Raises:
        ConfigError: If *log_format* is ``"jsonl"`` but *log_file* is ``None``.
    """
    verbosity = LogVerbosity.VERBOSE if verbose else LogVerbosity.NORMAL
    sinks: list[ConsoleSink | JSONLinesSink] = [ConsoleSink(stream=sys.stderr, verbosity=verbosity)]

    if log_format == "jsonl":
        if log_file is None:
            raise ConfigError("--log-file is required when --log-format is 'jsonl'.")
        sinks.append(JSONLinesSink(base_dir=log_file))

    return RunLogger(sinks=sinks, verbosity=verbosity)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CLI Commands — only defined when typer is available
# ---------------------------------------------------------------------------

_HELP_WORKFLOWS_DIR = "Additional directory to search for workflow modules."


@app.command()  # type: ignore[misc]
def run(
    module_path: Annotated[
        str,
        typer.Argument(help="Python dotted module path (e.g. my_wf.analysis)"),
    ],
    input: Annotated[  # noqa: A002
        str | None,
        typer.Option("--input", "-i", help="JSON string of initial workflow inputs."),
    ] = None,
    input_file: Annotated[
        str | None,
        typer.Option("--input-file", help="Path to a JSON file of initial workflow inputs."),
    ] = None,
    workflows_dir: Annotated[
        str | None,
        typer.Option("--workflows-dir", help=_HELP_WORKFLOWS_DIR),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose log output."),
    ] = False,
    log_format: Annotated[
        str,
        typer.Option("--log-format", help="Log output format: 'console' or 'jsonl'."),
    ] = "console",
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            help="Base directory for JSONL log output (required with --log-format=jsonl).",
        ),
    ] = None,
) -> None:
    """Execute a workflow from a Python module.

    The module must export a top-level ``workflow`` variable that is a
    ``kairos.Workflow`` instance.  The module must be within the current
    working directory or a directory specified by ``--workflows-dir`` /
    ``KAIROS_WORKFLOWS_DIR``.
    """
    # Validate --workflows-dir exists if provided
    if workflows_dir is not None and not os.path.isdir(workflows_dir):
        typer.echo(f"Error: --workflows-dir does not exist: {workflows_dir!r}", err=True)
        raise typer.Exit(code=2)

    # Load workflow (security checks happen inside)
    try:
        workflow = _load_workflow_from_module(module_path, workflows_dir)
    except (SecurityError, ConfigError, ModuleNotFoundError) as exc:
        err_type, err_msg = sanitize_exception(exc)
        typer.echo(f"Error loading module: {err_type}: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    # Resolve allowed dirs for input file validation
    allowed_dirs = _resolve_allowed_dirs(workflows_dir)

    # Parse input
    try:
        initial_inputs = _parse_input(input, input_file, allowed_dirs)
    except (ConfigError, SecurityError, FileNotFoundError) as exc:
        err_type, err_msg = sanitize_exception(exc)
        typer.echo(f"Error: {err_type}: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    # Build logger
    try:
        logger = _build_logger(verbose=verbose, log_format=log_format, log_file=log_file)
    except ConfigError as exc:
        err_type, err_msg = sanitize_exception(exc)
        typer.echo(f"Error: {err_type}: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    # Wire logger into workflow
    workflow.add_hook(logger)

    # Execute
    try:
        result = workflow.run(initial_inputs=initial_inputs)
    except Exception as exc:
        err_type, err_msg = sanitize_exception(exc)
        typer.echo(f"Workflow execution error: {err_type}: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    if result.status == WorkflowStatus.COMPLETE:
        typer.echo(f"\033[32mWorkflow '{workflow.name}' completed successfully.\033[0m")
        raise typer.Exit(code=0)
    else:
        msg = f"\033[31mWorkflow '{workflow.name}' failed (status={result.status}).\033[0m"
        typer.echo(msg, err=True)
        raise typer.Exit(code=1)


@app.command()  # type: ignore[misc]
def validate(
    module_path: Annotated[str, typer.Argument(help="Python dotted module path.")],
    input: Annotated[  # noqa: A002
        str | None,
        typer.Option(
            "--input",
            "-i",
            help="JSON string to validate against first step's input contract.",
        ),
    ] = None,
    input_file: Annotated[
        str | None,
        typer.Option(
            "--input-file",
            help="JSON file to validate against first step's input contract.",
        ),
    ] = None,
    workflows_dir: Annotated[
        str | None,
        typer.Option("--workflows-dir", help=_HELP_WORKFLOWS_DIR),
    ] = None,
) -> None:
    """Dry-run plan structure and contract validation without executing steps."""
    # Validate --workflows-dir exists if provided
    if workflows_dir is not None and not os.path.isdir(workflows_dir):
        typer.echo(f"Error: --workflows-dir does not exist: {workflows_dir!r}", err=True)
        raise typer.Exit(code=2)

    # Load workflow
    try:
        workflow = _load_workflow_from_module(module_path, workflows_dir)
    except (SecurityError, ConfigError, ModuleNotFoundError) as exc:
        err_type, err_msg = sanitize_exception(exc)
        typer.echo(f"Error loading module: {err_type}: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    # Report plan structure
    graph = workflow.graph
    step_count = len(graph.steps)
    plural = "s" if step_count != 1 else ""
    typer.echo(f"Plan structure valid ({step_count} step{plural}, graph validated)")

    # Report per-step contract status
    issues: list[str] = []
    for step in workflow.steps:
        has_input = step.input_contract is not None
        has_output = step.output_contract is not None
        if has_input and has_output:
            typer.echo(f"  Step {step.name!r} — input/output contracts defined")
        elif has_output:
            typer.echo(f"  Step {step.name!r} — output contract defined")
        elif has_input:
            typer.echo(f"  Step {step.name!r} — input contract defined")
        else:
            typer.echo(f"  Step {step.name!r} — no contracts")

    # Optionally validate input against first step's input contract
    if input is not None or input_file is not None:
        allowed_dirs = _resolve_allowed_dirs(workflows_dir)
        try:
            initial_inputs = _parse_input(input, input_file, allowed_dirs)
        except (ConfigError, SecurityError, FileNotFoundError) as exc:
            err_type, err_msg = sanitize_exception(exc)
            typer.echo(f"Error parsing input: {err_type}: {err_msg}", err=True)
            issues.append(f"{err_type}: {err_msg}")
        else:
            first_step = workflow.steps[0] if workflow.steps else None
            if first_step is not None and first_step.input_contract is not None:
                from kairos.validators import StructuralValidator

                validator = StructuralValidator()
                vresult = validator.validate(
                    data=initial_inputs,
                    schema=first_step.input_contract,  # type: ignore[arg-type]
                )
                if vresult.valid:
                    typer.echo("  Input validation: PASS")
                else:
                    typer.echo(f"  Input validation: FAIL ({len(vresult.errors)} error(s))")
                    for err in vresult.errors:
                        issues.append(f"    {err.field}: {err.message}")
                        typer.echo(f"    {err.field}: {err.message}")

    if issues:
        raise typer.Exit(code=1)

    typer.echo("Validation complete.")
    raise typer.Exit(code=0)


@app.command()  # type: ignore[misc]
def version() -> None:
    """Print the Kairos SDK version."""
    typer.echo(_kairos_sdk.__version__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the ``kairos`` CLI command."""
    app()


if __name__ == "__main__":
    main()
