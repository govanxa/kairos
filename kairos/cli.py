"""Kairos CLI — command-line runner for workflow execution and validation.

Provides four commands for v0.4.1:
- ``kairos run <module>``      Execute a workflow module.
- ``kairos validate <module>`` Dry-run plan and contract validation.
- ``kairos version``           Print the Kairos SDK version.
- ``kairos inspect <target>``  Inspect a .jsonl run log file.

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

# ANSI escape codes used by the inspect command (duplicated from ConsoleSink deliberately
# — no coupling between inspect output and the logger module).
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"
_ANSI_BOLD = "\033[1m"
_ANSI_CYAN = "\033[36m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RED = "\033[31m"
_ANSI_GREEN = "\033[32m"

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
        # Auto-create the log directory if it doesn't exist
        log_dir = Path(log_file)
        log_dir.mkdir(parents=True, exist_ok=True)
        sinks.append(JSONLinesSink(base_dir=str(log_dir)))

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

    # ANSI helpers
    _g = "\033[32m"  # green
    _y = "\033[33m"  # yellow
    _b = "\033[1m"  # bold
    _d = "\033[2m"  # dim
    _r = "\033[0m"  # reset

    # Report plan structure
    graph = workflow.graph
    step_count = len(graph.steps)
    plural = "s" if step_count != 1 else ""
    typer.echo(f"{_g}Plan structure valid{_r} ({step_count} step{plural}, graph validated)")

    # Report per-step contract status
    issues: list[str] = []
    for step in workflow.steps:
        has_input = step.input_contract is not None
        has_output = step.output_contract is not None
        name = f"Step {_b}{step.name!r}{_r}"
        if has_input and has_output:
            typer.echo(f"  {name}  {_g}input/output contracts{_r}")
        elif has_output:
            typer.echo(f"  {name}  {_g}output contract{_r}")
        elif has_input:
            typer.echo(f"  {name}  {_g}input contract{_r}")
        else:
            typer.echo(f"  {name}  {_d}no contracts{_r}")

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
                    typer.echo(f"  Input validation: {_g}PASS{_r}")
                else:
                    typer.echo(f"  Input validation: {_y}FAIL{_r} ({len(vresult.errors)} error(s))")
                    for err in vresult.errors:
                        issues.append(f"    {err.field}: {err.message}")
                        typer.echo(f"    {_y}{err.field}{_r}: {err.message}")

    if issues:
        raise typer.Exit(code=1)

    typer.echo(f"\n{_g}Validation complete.{_r}")
    raise typer.Exit(code=0)


@app.command()  # type: ignore[misc]
def version() -> None:
    """Print the Kairos SDK version."""
    typer.echo(_kairos_sdk.__version__)


# ---------------------------------------------------------------------------
# inspect helpers — pure functions, testable in isolation
# ---------------------------------------------------------------------------


def _inspect_resolve_jsonl_path(target: str) -> Path:
    """Resolve *target* to a ``.jsonl`` file path.

    If *target* is a file, it must exist and have a ``.jsonl`` extension.
    If *target* is a directory, all ``*.jsonl`` files inside are globbed;
    the most recently modified file is returned.

    Args:
        target: A file path or directory path (as a string).

    Returns:
        Resolved ``Path`` object pointing to the chosen ``.jsonl`` file.

    Raises:
        ConfigError: If the target does not exist, is not a ``.jsonl`` file,
            or is a directory containing no ``.jsonl`` files.
    """
    p = Path(target).resolve()

    if p.is_file():
        if p.suffix != ".jsonl":
            raise ConfigError(
                f"Target {target!r} is not a .jsonl file. "
                "The inspect command only reads .jsonl run logs."
            )
        return p

    if p.is_dir():
        candidates = sorted(p.glob("*.jsonl"), key=lambda f: os.path.getmtime(f))
        if not candidates:
            raise ConfigError(
                f"Directory {target!r} contains no .jsonl files. "
                "Run a workflow with --log-format=jsonl to produce a run log."
            )
        return candidates[-1]  # most recently modified

    raise ConfigError(
        f"Target {target!r} does not exist. "
        "Provide a path to a .jsonl run log or a directory containing one."
    )


def _inspect_read_events(file_path: Path) -> list[dict[str, object]]:
    """Read a ``.jsonl`` file and return a list of parsed event dicts.

    Each line is parsed independently with ``json.loads()``.  Malformed lines
    are skipped with a warning to stderr.

    Args:
        file_path: Absolute path to the ``.jsonl`` file.

    Returns:
        List of parsed event dicts (may be empty only if every line is malformed).

    Raises:
        ConfigError: If the file contains no valid events.
    """
    events: list[dict[str, object]] = []
    raw_text = file_path.read_text(encoding="utf-8")

    for lineno, line in enumerate(raw_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                events.append(cast(dict[str, object], parsed))
            else:
                print(  # noqa: T20
                    f"Warning: line {lineno} in {file_path.name!r} is not a JSON object — skipped.",
                    file=sys.stderr,
                )
        except json.JSONDecodeError:
            print(  # noqa: T20
                f"Warning: line {lineno} in {file_path.name!r} is malformed JSON — skipped.",
                file=sys.stderr,
            )

    if not events:
        raise ConfigError(
            f"No valid events found in {str(file_path)!r}. "  # str() avoids WindowsPath repr
            "The file may be empty or contain only malformed JSON lines."
        )

    return events


def _inspect_extract_summary(events: list[dict[str, object]]) -> dict[str, object]:
    """Extract a run summary from the event list.

    Derives: workflow_name, run_id, status, started_at, duration_ms, and
    step counts.  Authoritative step counts come from ``workflow_complete``'s
    ``data.summary`` when present; otherwise they are counted from step events.

    Args:
        events: List of parsed event dicts from a ``.jsonl`` file.

    Returns:
        Summary dict with keys: ``workflow_name``, ``run_id``, ``status``,
        ``started_at``, ``duration_ms``, ``total_steps``, ``completed_steps``,
        ``failed_steps``, ``skipped_steps``.
    """
    workflow_name: str = "unknown"
    run_id: str = ""
    status: str = "incomplete"
    started_at: str = ""
    duration_ms: float = 0.0
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0

    # Scan once — collect what we need from known event types
    for event in events:
        etype = event.get("event_type", "")
        raw_data: object = event.get("data") or {}
        data: dict[str, object] = (
            cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        )

        if etype == "workflow_start":
            workflow_name = str(data.get("workflow_name", "unknown"))
            run_id = str(data.get("run_id", ""))
            started_at = str(event.get("timestamp", ""))
            total_steps = int(str(data.get("total_steps", 0)))

        elif etype == "workflow_complete":
            status = str(data.get("status", "unknown"))
            raw_summary: object = data.get("summary")
            if isinstance(raw_summary, dict):
                sd = cast(dict[str, object], raw_summary)
                total_steps = int(str(sd.get("total_steps", total_steps)))
                completed_steps = int(str(sd.get("completed_steps", 0)))
                failed_steps = int(str(sd.get("failed_steps", 0)))
                skipped_steps = int(str(sd.get("skipped_steps", 0)))
                duration_ms = float(str(sd.get("total_duration_ms", 0.0)))

    # If no workflow_complete was found, count steps from individual events
    if status == "incomplete":
        seen_completed: set[str] = set()
        seen_failed: set[str] = set()
        seen_skipped: set[str] = set()
        for event in events:
            etype = event.get("event_type", "")
            step_id = event.get("step_id")
            if step_id is None:
                continue
            if etype == "step_complete":
                seen_completed.add(str(step_id))
            elif etype == "step_fail":
                seen_failed.add(str(step_id))
            elif etype == "step_skip":
                seen_skipped.add(str(step_id))
        completed_steps = len(seen_completed)
        failed_steps = len(seen_failed)
        skipped_steps = len(seen_skipped)

    return {
        "workflow_name": workflow_name,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "total_steps": total_steps,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "skipped_steps": skipped_steps,
    }


def _inspect_filter_events(
    events: list[dict[str, object]],
    failures_only: bool,
    step_name: str | None,
) -> list[dict[str, object]]:
    """Filter the event list by failure level and/or step name.

    Workflow-level events (``step_id`` is ``None``) always pass through.
    When ``failures_only`` is True, step-level events must be ``error`` or
    ``warn`` level.  When ``step_name`` is provided, step-level events must
    have a matching ``step_id``.

    Args:
        events: List of parsed event dicts.
        failures_only: Keep only error/warn-level step events.
        step_name: If provided, keep only events for this step_id.

    Returns:
        Filtered list of event dicts.
    """
    result: list[dict[str, object]] = []
    for event in events:
        step_id = event.get("step_id")
        is_workflow_level = step_id is None

        if is_workflow_level:
            # Workflow-level events always pass through
            result.append(event)
            continue

        # Apply step-name filter
        if step_name is not None and str(step_id) != step_name:
            continue

        # Apply failures filter — check level for error/warn
        if failures_only:
            level = str(event.get("level", "")).lower()
            if "error" not in level and "warn" not in level:
                continue

        result.append(event)

    return result


def _inspect_format_header(summary: dict[str, object], color: bool) -> str:
    """Build the multi-line header block for an inspect run.

    Args:
        summary: Dict produced by ``_inspect_extract_summary()``.
        color: When True, ANSI escape codes are included.

    Returns:
        Multi-line string with Run, Status, Started, Duration, and Steps lines.
    """

    def bold(text: str) -> str:
        return f"{_ANSI_BOLD}{text}{_ANSI_RESET}" if color else text

    def cyan(text: str) -> str:
        return f"{_ANSI_CYAN}{text}{_ANSI_RESET}" if color else text

    def green(text: str) -> str:
        return f"{_ANSI_GREEN}{text}{_ANSI_RESET}" if color else text

    def red(text: str) -> str:
        return f"{_ANSI_RED}{text}{_ANSI_RESET}" if color else text

    def yellow(text: str) -> str:
        return f"{_ANSI_YELLOW}{text}{_ANSI_RESET}" if color else text

    def dim(text: str) -> str:
        return f"{_ANSI_DIM}{text}{_ANSI_RESET}" if color else text

    name = str(summary.get("workflow_name", "unknown"))
    run_id = str(summary.get("run_id", ""))
    run_id_short = run_id[:8] if run_id else "--------"
    status = str(summary.get("status", "unknown"))
    started_at = str(summary.get("started_at", ""))
    duration_ms = float(str(summary.get("duration_ms", 0.0)))
    total_steps = int(str(summary.get("total_steps", 0)))
    completed = int(str(summary.get("completed_steps", 0)))
    failed = int(str(summary.get("failed_steps", 0)))
    skipped = int(str(summary.get("skipped_steps", 0)))

    # Status color
    if status == "complete":
        status_str = green(status)
    elif status in ("failed", "error"):
        status_str = red(status)
    elif status == "incomplete":
        status_str = yellow(status)
    else:
        status_str = status

    lines = [
        f"Run: {bold(name)} {dim(f'(run {run_id_short})')}",
        f"Status: {status_str}",
        f"Started: {dim(started_at)}",
        f"Duration: {duration_ms:.1f}ms",
        f"Steps: {cyan(f'{completed}/{total_steps}')} completed, "
        f"{red(str(failed)) if failed else str(failed)} failed, "
        f"{str(skipped)} skipped",
    ]
    return "\n".join(lines)


def _inspect_format_event_line(event: dict[str, object], color: bool) -> str:
    """Format a single event dict as a human-readable line.

    Matches the ConsoleSink format: timestamp, level, event_type, detail.

    Args:
        event: Parsed event dict from a ``.jsonl`` file.
        color: When True, ANSI escape codes are included.

    Returns:
        A single formatted line string (no trailing newline).
    """

    def dim(text: str) -> str:
        return f"{_ANSI_DIM}{text}{_ANSI_RESET}" if color else text

    def bold(text: str) -> str:
        return f"{_ANSI_BOLD}{text}{_ANSI_RESET}" if color else text

    def red_txt(text: str) -> str:
        return f"{_ANSI_RED}{text}{_ANSI_RESET}" if color else text

    def green_txt(text: str) -> str:
        return f"{_ANSI_GREEN}{text}{_ANSI_RESET}" if color else text

    # Parse timestamp to HH:MM:SS
    ts_raw = str(event.get("timestamp", ""))
    # ISO 8601: 2024-01-15T10:00:00+00:00 → extract time portion
    ts_part = ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw

    # Level formatting
    level_raw = str(event.get("level", "info")).lower()
    if "error" in level_raw:
        level_str = (f"{_ANSI_RED}ERROR{_ANSI_RESET}" if color else "ERROR").ljust(5)
    elif "warn" in level_raw:
        level_str = (f"{_ANSI_YELLOW}WARN {_ANSI_RESET}" if color else "WARN ").ljust(5)
    else:
        level_str = (f"{_ANSI_CYAN}INFO {_ANSI_RESET}" if color else "INFO ").ljust(5)

    etype = str(event.get("event_type", "unknown"))
    raw_data: object = event.get("data") or {}
    data: dict[str, object] = (
        cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
    )

    match etype:
        case "workflow_start":
            name = data.get("workflow_name", "?")
            run_id = str(data.get("run_id", ""))[:8]
            steps = data.get("total_steps", "?")
            detail = f"{bold(str(name))} ({steps} steps, run {run_id})"
        case "workflow_complete":
            status = data.get("status", "?")
            raw_s: object = data.get("summary", {})
            if isinstance(raw_s, dict):
                s = cast(dict[str, object], raw_s)
                done = s.get("completed_steps", "?")
                total = s.get("total_steps", "?")
                dur = s.get("total_duration_ms", 0)
                dur_str = f"{float(str(dur)):.1f}ms"
                marker = green_txt("ok") if status == "complete" else red_txt(str(status))
                detail = f"{marker} {done}/{total} steps, {dur_str}"
            else:
                detail = str(status)
        case "step_start":
            step = data.get("step_id", "?")
            attempt = data.get("attempt", 1)
            detail = bold(str(step))
            try:
                attempt_int = int(str(attempt))
            except (ValueError, TypeError):
                attempt_int = 1
            if attempt_int > 1:
                detail += f" (attempt {attempt})"
        case "step_complete":
            step = data.get("step_id", "?")
            dur = data.get("duration_ms", 0)
            dur_str = f"{float(str(dur)):.1f}ms"
            detail = f"{bold(str(step))} {green_txt('ok')} {dur_str}"
        case "step_fail":
            step = data.get("step_id", "?")
            err_type = data.get("error_type", "Error")
            err_msg = data.get("error_message", "")
            msg = f"{err_type}: {err_msg}" if err_msg else str(err_type)
            detail = f"{bold(str(step))} {red_txt('FAILED')} {msg}"
        case "step_retry":
            step = data.get("step_id", "?")
            attempt = data.get("attempt", "?")
            detail = f"{bold(str(step))} retry (attempt {attempt})"
        case "step_skip":
            step = data.get("step_id", "?")
            reason = data.get("reason", "")
            detail = f"{bold(str(step))} skipped"
            if reason:
                detail += f" ({reason})"
        case "validation_complete":
            step = data.get("step_id", "?")
            phase = data.get("phase", "?")
            detail = f"{step} {phase} {green_txt('pass')}"
        case "validation_fail":
            step = data.get("step_id", "?")
            phase = data.get("phase", "?")
            detail = f"{step} {phase} {red_txt('FAIL')}"
        case "validation_start":
            step = data.get("step_id", "?")
            phase = data.get("phase", "?")
            detail = f"{step} {phase}"
        case _:
            data_parts = ", ".join(f"{k}={v}" for k, v in data.items())
            detail = data_parts

    ts_fmt = dim(ts_part)
    return f"  {ts_fmt} {level_str} {etype:<22s} {detail}"


# ---------------------------------------------------------------------------
# inspect command
# ---------------------------------------------------------------------------


@app.command()  # type: ignore[misc]
def inspect(
    target: Annotated[
        str,
        typer.Argument(
            help="Path to a .jsonl run log file, or a directory containing .jsonl files."
        ),
    ],
    failures: Annotated[
        bool,
        typer.Option("--failures", "-f", help="Show only error/warning events."),
    ] = False,
    step: Annotated[
        str | None,
        typer.Option("--step", "-s", help="Filter events to a specific step name."),
    ] = None,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable ANSI color output."),
    ] = False,
) -> None:
    """Inspect a Kairos run log (.jsonl) file.

    Displays a summary header (workflow name, run ID, status, duration, step
    counts) followed by a timestamped event timeline.  Optionally filters to
    failure events only (``--failures``) or a specific step (``--step``).

    The *target* may be a path to a ``.jsonl`` file or a directory; when a
    directory is given, the most recently modified ``.jsonl`` file is used.
    """
    # Auto-detect color unless --no-color is set
    use_color = (not no_color) and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    # Resolve the .jsonl file
    try:
        file_path = _inspect_resolve_jsonl_path(target)
    except ConfigError as exc:
        _, err_msg = sanitize_exception(exc)
        typer.echo(f"Error: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    # Read events
    try:
        events = _inspect_read_events(file_path)
    except ConfigError as exc:
        _, err_msg = sanitize_exception(exc)
        typer.echo(f"Error: {err_msg}", err=True)
        raise typer.Exit(code=1) from None

    # Extract summary
    summary = _inspect_extract_summary(events)

    # Print header
    header = _inspect_format_header(summary, color=use_color)
    typer.echo(header)
    typer.echo("")  # blank line between header and timeline

    # Filter events
    filtered = _inspect_filter_events(events, failures_only=failures, step_name=step)

    # Determine if the step filter found any step-level events (not just workflow-level)
    if step is not None:
        step_level_events = [e for e in filtered if e.get("step_id") is not None]
        if not step_level_events:
            typer.echo(
                f"No events found for step {step!r}. "
                "Use --step with the exact step name as it appears in the log.",
                err=True,
            )
            raise typer.Exit(code=1) from None

    # Print timeline
    for event in filtered:
        line = _inspect_format_event_line(event, color=use_color)
        typer.echo(line)

    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the ``kairos`` CLI command."""
    app()


if __name__ == "__main__":
    main()
