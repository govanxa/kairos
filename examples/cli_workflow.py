"""Example workflow designed to be run via the Kairos CLI.

This module exports a ``workflow`` variable — the convention the CLI expects.
No ``if __name__`` block needed. Just run:

    kairos run examples.cli_workflow --input '{"topic": "AI safety"}'

Or validate without executing:

    kairos validate examples.cli_workflow --input '{"topic": "test"}'

Or run with verbose logging:

    kairos run examples.cli_workflow --input '{"topic": "AI safety"}' --verbose

Or output structured logs to a file:

    kairos run examples.cli_workflow --input '{"topic": "AI safety"}' \
        --log-format jsonl --log-file ./logs
"""

from __future__ import annotations

from typing import Any, cast

from kairos import (
    FailureAction,
    FailurePolicy,
    Schema,
    Step,
    StepContext,
    Workflow,
)
from kairos import validators as v

# ---------------------------------------------------------------------------
# Contracts — the CLI's validate command checks these without executing
# ---------------------------------------------------------------------------

research_output = Schema(
    {"findings": list, "source_count": int},
    validators={"source_count": [v.range(min=1)]},
)

summary_output = Schema(
    {"title": str, "summary": str, "confidence": float},
    validators={
        "title": [v.not_empty()],
        "summary": [v.not_empty()],
        "confidence": [v.range(min=0.0, max=1.0)],
    },
)

# ---------------------------------------------------------------------------
# Step actions
# ---------------------------------------------------------------------------


def research(ctx: StepContext) -> dict[str, Any]:
    """Gather research findings on the given topic."""
    topic = cast(str, ctx.state.get("topic", "general AI"))
    return {
        "findings": [
            f"Finding 1: {topic} has significant industry momentum",
            f"Finding 2: Regulatory frameworks for {topic} are evolving",
            f"Finding 3: Open-source contributions to {topic} are accelerating",
        ],
        "source_count": 3,
    }


def analyze(ctx: StepContext) -> dict[str, Any]:
    """Analyze the research findings."""
    findings = cast(dict[str, Any], ctx.inputs["research"])
    items = cast(list[str], findings["findings"])
    return {
        "themes": [f"Theme from: {item[:40]}" for item in items],
        "total_findings": len(items),
    }


def summarize(ctx: StepContext) -> dict[str, Any]:
    """Produce a final summary from the analysis."""
    topic = cast(str, ctx.state.get("topic", "general AI"))
    analysis = cast(dict[str, Any], ctx.inputs["analyze"])
    total = cast(int, analysis["total_findings"])
    return {
        "title": f"Research Summary: {topic}",
        "summary": (
            f"Analysis of {total} findings on '{topic}' reveals strong industry momentum, "
            f"evolving regulatory frameworks, and accelerating open-source contributions."
        ),
        "confidence": 0.85,
    }


# ---------------------------------------------------------------------------
# Workflow — this is what the CLI discovers and runs
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="cli-research",
    steps=[
        Step(
            name="research",
            action=research,
            output_contract=research_output,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
            ),
        ),
        Step(
            name="analyze",
            action=analyze,
            depends_on=["research"],
        ),
        Step(
            name="summarize",
            action=summarize,
            depends_on=["research", "analyze"],
            output_contract=summary_output,
        ),
    ],
    sensitive_keys=["*_key", "*_token"],
)
