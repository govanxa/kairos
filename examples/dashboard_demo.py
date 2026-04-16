"""Dashboard demo — run a workflow with logging, then launch the dashboard.

Demonstrates:
- Running a multi-step workflow with JSONLinesSink logging
- Generating .jsonl run log files in a temporary directory
- Launching the Kairos dashboard to visualize the runs
- Token-authenticated localhost web UI

No API keys required — runs entirely locally with mock step actions.

Usage:
    # Step 1: Run this script to generate log data
    python examples/dashboard_demo.py

    # Step 2: Launch the dashboard to view the runs
    kairos dashboard --log-dir ./dashboard_logs --open

    # Or without auto-opening the browser:
    kairos dashboard --log-dir ./dashboard_logs
    # Then open the printed URL in your browser
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

from kairos import (
    FailureAction,
    FailurePolicy,
    LogVerbosity,
    RunLogger,
    Schema,
    Step,
    StepContext,
    Workflow,
    WorkflowStatus,
)
from kairos import validators as v
from kairos.logger import ConsoleSink, JSONLinesSink

# ---------------------------------------------------------------------------
# Step actions — mock functions (no API keys needed)
# ---------------------------------------------------------------------------


def fetch_competitors(ctx: StepContext) -> dict[str, object]:
    """Fetch a list of competitors from state."""
    industry = ctx.state.get("industry", "technology")
    return {
        "companies": ["Acme Corp", "Globex Industries", "Initech Solutions"],
        "industry": industry,
    }


def analyze_competitor(ctx: StepContext) -> dict[str, object]:
    """Analyze a single competitor (runs once per item via foreach)."""
    company = ctx.item
    return {
        "name": company,
        "products": [f"{company} Pro", f"{company} Lite"],
        "strength": f"{company} has strong market presence in its segment",
        "score": round(len(str(company)) * 0.08, 2),
    }


def write_report(ctx: StepContext) -> dict[str, object]:
    """Synthesize all analyses into a final report."""
    analyses = cast(list[dict[str, object]], ctx.inputs["analyze"])
    names = [str(a["name"]) for a in analyses]
    avg_score = sum(float(str(a["score"])) for a in analyses) / len(analyses)
    return {
        "title": "Competitive Analysis Report",
        "companies_analyzed": len(names),
        "average_score": round(avg_score, 2),
        "recommendation": "Focus on differentiation through security-first design.",
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

analysis_schema = Schema(
    {"name": str, "products": list, "strength": str, "score": float},
    validators={
        "name": [v.not_empty()],
        "strength": [v.not_empty()],
        "score": [v.range(min=0.0, max=1.0)],
    },
)

report_schema = Schema(
    {"title": str, "companies_analyzed": int, "average_score": float, "recommendation": str},
    validators={
        "title": [v.not_empty()],
        "recommendation": [v.length(min=10)],
    },
)

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

LOG_DIR = Path("dashboard_logs")


def run_workflow(name: str, inputs: dict[str, object], fail_step: str | None = None) -> None:
    """Run a workflow variant and log to the dashboard_logs directory."""

    def maybe_fail(ctx: StepContext) -> dict[str, object]:
        """A step that raises an error (for demonstrating failures)."""
        raise RuntimeError("Simulated failure for dashboard demo")

    steps = [
        Step(name="fetch", action=fetch_competitors, read_keys=["industry"]),
        Step(
            name="analyze",
            action=analyze_competitor if fail_step != "analyze" else maybe_fail,
            depends_on=["fetch"],
            foreach="companies",
            output_contract=analysis_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                on_validation_fail=FailureAction.ABORT,
                max_retries=2,
            ),
        ),
        Step(
            name="report",
            action=write_report if fail_step != "report" else maybe_fail,
            depends_on=["analyze"],
            output_contract=report_schema,
        ),
    ]

    # Set up logging — console + JSONL file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(
        verbosity=LogVerbosity.NORMAL,
        sinks=[
            ConsoleSink(stream=sys.stderr, verbosity=LogVerbosity.NORMAL),
            JSONLinesSink(base_dir=str(LOG_DIR)),
        ],
        sensitive_patterns=["*api_key*", "*password*"],
    )

    wf = Workflow(
        name=name,
        steps=steps,
        hooks=[logger],
        sensitive_keys=["*api_key*", "*password*"],
    )

    result = wf.run(initial_inputs=inputs)

    status_icon = "ok" if result.status == WorkflowStatus.COMPLETE else "FAILED"
    print(f"\n  [{status_icon}] {name} — {result.status.value}")  # noqa: T20
    print(f"      Steps: {len(result.step_results)}, LLM calls: {result.llm_calls}")  # noqa: T20


# ---------------------------------------------------------------------------
# Main — run multiple workflows to generate dashboard data
# ---------------------------------------------------------------------------


def main() -> None:
    """Run several workflow variants to populate the dashboard."""
    print("=" * 60)  # noqa: T20
    print("  Kairos Dashboard Demo")  # noqa: T20
    print("  Generating run history for the dashboard...")  # noqa: T20
    print("=" * 60)  # noqa: T20

    # Run 1: Successful competitive analysis
    print("\n--- Run 1: Successful analysis ---")  # noqa: T20
    run_workflow(
        name="competitive-analysis",
        inputs={"industry": "fintech", "companies": ["Acme", "Globex", "Initech"]},
    )

    # Run 2: Different industry
    print("\n--- Run 2: Healthcare industry ---")  # noqa: T20
    run_workflow(
        name="competitive-analysis",
        inputs={"industry": "healthcare", "companies": ["MedCorp", "HealthTech"]},
    )

    # Run 3: Intentional failure (report step fails)
    print("\n--- Run 3: Report step failure ---")  # noqa: T20
    run_workflow(
        name="competitive-analysis",
        inputs={"industry": "retail", "companies": ["ShopCo", "MartInc"]},
        fail_step="report",
    )

    # Summary
    log_files = list(LOG_DIR.glob("*.jsonl"))
    print(f"\n{'=' * 60}")  # noqa: T20
    print(f"  Done! {len(log_files)} run log(s) written to: {LOG_DIR}/")  # noqa: T20
    print()  # noqa: T20
    print("  To view the dashboard:")  # noqa: T20
    print(f"    kairos dashboard --log-dir {LOG_DIR} --open")  # noqa: T20
    print()  # noqa: T20
    print("  Or without auto-open:")  # noqa: T20
    print(f"    kairos dashboard --log-dir {LOG_DIR}")  # noqa: T20
    print("    (then open the printed URL in your browser)")  # noqa: T20
    print(f"{'=' * 60}")  # noqa: T20


if __name__ == "__main__":
    main()
