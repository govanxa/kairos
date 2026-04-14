"""Run Logger demo — structured logging for Kairos workflows.

Demonstrates:
- RunLogger subscribing to executor lifecycle hooks
- All 4 sink types: ConsoleSink, JSONLinesSink, FileSink, CallbackSink
- Verbosity levels: minimal, normal, verbose
- Sensitive key redaction (api_key never appears in logs)
- Retrieving and inspecting the RunLog after execution
- Reading JSON Lines output from disk

No API keys required — runs entirely locally with mock step actions.
"""

from __future__ import annotations

import json
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, cast

from kairos import (
    FailureAction,
    FailurePolicy,
    LogVerbosity,
    RunLogger,
    Step,
    StepContext,
    Workflow,
)
from kairos.logger import (
    CallbackSink,
    ConsoleSink,
    FileSink,
    JSONLinesSink,
    LogEvent,
)

# ---------------------------------------------------------------------------
# Step actions — each simulates a stage in a data pipeline
# ---------------------------------------------------------------------------


def fetch_data(ctx: StepContext) -> dict[str, Any]:
    """Simulate fetching data from an external source."""
    query = ctx.state.get("query", "AI orchestration")
    # Store a sensitive key — the logger should redact this
    ctx.state.set("api_key", "sk-secret-12345-never-log-this")
    return {
        "query": query,
        "records": [
            {"id": 1, "title": "Workflow engines", "score": 0.95},
            {"id": 2, "title": "Agent frameworks", "score": 0.87},
            {"id": 3, "title": "Validation layers", "score": 0.72},
        ],
    }


def analyze(ctx: StepContext) -> dict[str, Any]:
    """Analyze the fetched records."""
    fetched = cast(dict[str, Any], ctx.inputs["fetch_data"])
    records = cast(list[dict[str, Any]], fetched["records"])
    top = max(records, key=lambda r: r["score"])
    return {
        "total_records": len(records),
        "top_result": top["title"],
        "top_score": top["score"],
        "avg_score": sum(r["score"] for r in records) / len(records),
    }


def summarize(ctx: StepContext) -> dict[str, Any]:
    """Produce a final summary."""
    fetched = cast(dict[str, Any], ctx.inputs["fetch_data"])
    analysis = cast(dict[str, Any], ctx.inputs["analyze"])
    return {
        "summary": (
            f"Query '{fetched['query']}' returned {analysis['total_records']} results. "
            f"Top match: {analysis['top_result']} (score: {analysis['top_score']:.2f}). "
            f"Average score: {analysis['avg_score']:.2f}."
        ),
    }


def flaky_step(ctx: StepContext) -> dict[str, Any]:
    """A step that always fails — demonstrates error logging."""
    msg = "Simulated transient failure for logging demo"
    raise ConnectionError(msg)


# ---------------------------------------------------------------------------
# Callback sink — collect events programmatically
# ---------------------------------------------------------------------------


def my_callback(event: LogEvent) -> None:
    """Custom callback that prints a one-line summary for each event."""
    prefix = f"  [CALLBACK] {event.event_type}"
    if event.step_id:
        prefix += f" ({event.step_id})"
    print(prefix)


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------


def run_demo() -> None:
    """Run the full logger demo."""
    # Suppress the expected CallbackSink trust boundary warnings for cleaner output
    warnings.filterwarnings("ignore", message="CallbackSink")

    # Create a temp directory for file-based sinks
    tmp_dir = Path(tempfile.mkdtemp(prefix="kairos_logs_"))
    print(f"Log output directory: {tmp_dir}\n")

    # -----------------------------------------------------------------------
    # Demo 1: All 4 sinks at NORMAL verbosity
    # -----------------------------------------------------------------------
    print("=" * 70)
    print("DEMO 1: All 4 sinks - ConsoleSink + JSONLinesSink + FileSink + Callback")
    print("=" * 70)
    print()

    logger = RunLogger(
        sinks=[
            ConsoleSink(stream=sys.stdout, verbosity=LogVerbosity.NORMAL),
            JSONLinesSink(str(tmp_dir)),
            FileSink(str(tmp_dir)),
            CallbackSink(my_callback),
        ],
        verbosity=LogVerbosity.VERBOSE,
        sensitive_patterns=["*_key", "*_secret"],
    )

    workflow = Workflow(
        name="logger-demo",
        steps=[
            Step(name="fetch_data", action=fetch_data),
            Step(name="analyze", action=analyze, depends_on=["fetch_data"]),
            Step(name="summarize", action=summarize, depends_on=["fetch_data", "analyze"]),
        ],
        hooks=[logger],
    )

    print("--- Console output (NORMAL verbosity): ---\n")
    result = workflow.run({"query": "structured logging"})
    print()

    # Show workflow result
    print(f"Workflow status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.1f}ms")
    print()

    # -----------------------------------------------------------------------
    # Inspect the RunLog
    # -----------------------------------------------------------------------
    print("-" * 70)
    print("INSPECTING THE RUNLOG")
    print("-" * 70)
    print()

    run_log = logger.get_run_log()
    print(f"Run ID:        {run_log.run_id}")
    print(f"Workflow:      {run_log.workflow_name}")
    print(f"Status:        {run_log.status}")
    print(f"Started at:    {run_log.started_at}")
    print(f"Completed at:  {run_log.completed_at}")
    print(f"Total events:  {len(run_log.events)}")
    print()

    # Summary
    s = run_log.summary
    print("Run Summary:")
    print(f"  Total steps:         {s.total_steps}")
    print(f"  Completed:           {s.completed_steps}")
    print(f"  Failed:              {s.failed_steps}")
    print(f"  Skipped:             {s.skipped_steps}")
    print(f"  Total retries:       {s.total_retries}")
    print(f"  Validations passed:  {s.validations_passed}")
    print(f"  Validations failed:  {s.validations_failed}")
    print(f"  Duration:            {s.total_duration_ms:.1f}ms")
    print()

    # List all events
    print("All events captured:")
    for i, event in enumerate(run_log.events, 1):
        step = event.step_id or "(workflow)"
        print(f"  {i:2d}. [{event.level}] {event.event_type:25s}  step={step}")
    print()

    # -----------------------------------------------------------------------
    # Verify sensitive key redaction
    # -----------------------------------------------------------------------
    print("-" * 70)
    print("SENSITIVE KEY REDACTION")
    print("-" * 70)
    print()

    # The api_key was stored in state, but should be redacted in final_state
    raw_key = "sk-secret-12345-never-log-this"
    print(f"Raw api_key in state:          {raw_key}")
    print(f"api_key in final_state:        {result.final_state.get('api_key', 'N/A')}")

    # Check that the raw key never appears in any log event
    log_json = json.dumps(run_log.to_dict())
    if raw_key in log_json:
        print("WARNING: Raw API key found in log output!")
    else:
        print("Confirmed: Raw API key does NOT appear in any log event.")
    print()

    # -----------------------------------------------------------------------
    # Read the JSON Lines file
    # -----------------------------------------------------------------------
    print("-" * 70)
    print("JSON LINES OUTPUT (from disk)")
    print("-" * 70)
    print()

    jsonl_files = list(tmp_dir.glob("*.jsonl"))
    if jsonl_files:
        jsonl_file = jsonl_files[0]
        print(f"File: {jsonl_file.name}")
        lines = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
        print(f"Total lines: {len(lines)}")
        print()
        # Show first 3 lines as pretty JSON
        for i, line in enumerate(lines[:3], 1):
            parsed = json.loads(line)
            print(f"  Line {i}: {json.dumps(parsed, indent=2)[:200]}...")
        if len(lines) > 3:
            print(f"  ... and {len(lines) - 3} more lines")
    print()

    # -----------------------------------------------------------------------
    # Read the full JSON file (FileSink output)
    # -----------------------------------------------------------------------
    json_files = list(tmp_dir.glob("*.json"))
    if json_files:
        json_file = json_files[0]
        print(f"Full RunLog file: {json_file.name}")
        content = json.loads(json_file.read_text(encoding="utf-8"))
        print(f"  run_id:    {content['run_id']}")
        print(f"  events:    {len(content['events'])}")
        print(f"  status:    {content['status']}")
    print()

    # -----------------------------------------------------------------------
    # Demo 2: Workflow with a failing step (error logging)
    # -----------------------------------------------------------------------
    print("=" * 70)
    print("DEMO 2: Error logging - a step that fails with retry")
    print("=" * 70)
    print()

    error_logger = RunLogger(
        sinks=[ConsoleSink(stream=sys.stdout, verbosity=LogVerbosity.NORMAL)],
        verbosity=LogVerbosity.NORMAL,
    )

    error_workflow = Workflow(
        name="error-demo",
        steps=[
            Step(
                name="flaky_step",
                action=flaky_step,
                failure_policy=FailurePolicy(
                    on_execution_fail=FailureAction.RETRY,
                    max_retries=2,
                ),
            ),
        ],
        hooks=[error_logger],
    )

    print("--- Console output (retries + failure): ---\n")
    error_result = error_workflow.run({})
    print()

    print(f"Workflow status: {error_result.status.value}")
    error_log = error_logger.get_run_log()
    print(f"Events captured: {len(error_log.events)}")
    print(f"Total retries:   {error_log.summary.total_retries}")
    print(f"Failed steps:    {error_log.summary.failed_steps}")
    print()

    # Show error events
    error_events = [e for e in error_log.events if e.level.value == "error"]
    for evt in error_events:
        print(f"  Error event: {evt.event_type} - {evt.data}")
    print()

    # -----------------------------------------------------------------------
    # Demo 3: Verbosity comparison
    # -----------------------------------------------------------------------
    print("=" * 70)
    print("DEMO 3: Verbosity comparison - same workflow, 3 verbosity levels")
    print("=" * 70)
    print()

    for level in [LogVerbosity.MINIMAL, LogVerbosity.NORMAL, LogVerbosity.VERBOSE]:
        vlg = RunLogger(sinks=[], verbosity=level)
        vwf = Workflow(
            name="verbosity-test",
            steps=[
                Step(name="step_a", action=fetch_data),
                Step(name="step_b", action=analyze, depends_on=["step_a"]),
            ],
            hooks=[vlg],
        )
        vwf.run({"query": "test"})
        vlog = vlg.get_run_log()
        event_types = [e.event_type for e in vlog.events]
        print(f"  {level.value:8s} -> {len(vlog.events):2d} events: {event_types}")

    print()
    print("Done! All demos complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_demo()
