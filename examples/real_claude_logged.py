"""Real Claude API example with structured logging.

Runs the same concurrent research workflow as real_claude_concurrent.py,
but with a RunLogger wired in. Shows how every lifecycle event (step start,
step complete, retries, failures) is captured in real time and available
as structured data after execution.

Workflow graph:
    plan_research --> analyze_security  (parallel=True) -->
                 --> analyze_market     (parallel=True) --> synthesize
                 --> analyze_technology (parallel=True) -->

Logging output:
    - ConsoleSink prints each event to stdout as it happens
    - JSONLinesSink writes every event to a .jsonl file on disk
    - RunLog is inspected after the workflow completes

REQUIRES:
    1. pip install kairos-ai[anthropic]
    2. Set ANTHROPIC_API_KEY in your environment:
       Windows PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-your-key"
       Linux/Mac: export ANTHROPIC_API_KEY="sk-ant-your-key"

To run:
    py examples/real_claude_logged.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

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
from kairos.adapters.claude import ClaudeAdapter
from kairos.logger import ConsoleSink, JSONLinesSink

MODEL = "claude-sonnet-4-20250514"
RETRY_POLICY = FailurePolicy(on_execution_fail=FailureAction.RETRY, max_retries=1)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

analysis_schema = Schema(
    {"analysis": str, "key_finding": str, "tokens_used": int},
    validators={"analysis": [v.not_empty()], "key_finding": [v.not_empty()]},
)

synthesis_schema = Schema(
    {"summary": str, "total_tokens": int},
    validators={"summary": [v.not_empty()]},
)

# ---------------------------------------------------------------------------
# Step actions (identical to real_claude_concurrent.py)
# ---------------------------------------------------------------------------


def plan_research(ctx: StepContext) -> dict[str, Any]:
    """Generate a focused research question from the user query."""
    adapter = ClaudeAdapter(model=MODEL)
    query = cast(str, ctx.state.get("query"))

    response = adapter.call(
        f"You are a research planner. Given this query: '{query}'\n\n"
        f"Produce a one-sentence research focus for each of these three angles:\n"
        f"1. Security implications\n"
        f"2. Market opportunity\n"
        f"3. Technology landscape\n\n"
        f"Format: one sentence per line, no numbering.",
        max_tokens=200,
    )
    ctx.increment_llm_calls()

    lines = [line.strip() for line in response.text.strip().splitlines() if line.strip()]
    while len(lines) < 3:
        lines.append(f"Analyze {query} from a general perspective.")

    return {
        "security_focus": lines[0],
        "market_focus": lines[1],
        "tech_focus": lines[2],
        "tokens_used": response.usage.total_tokens,
    }


def analyze_security(ctx: StepContext) -> dict[str, Any]:
    """Analyze security implications - runs IN PARALLEL with market and tech."""
    adapter = ClaudeAdapter(model=MODEL)
    plan = cast(dict[str, Any], ctx.inputs["plan_research"])

    response = adapter.call(
        f"You are a cybersecurity analyst. In 3-4 sentences, analyze:\n"
        f"{plan['security_focus']}\n\n"
        f"End with one key finding sentence starting with 'KEY FINDING:'",
        max_tokens=300,
    )
    ctx.increment_llm_calls()

    text = response.text
    key = _extract_key_finding(text)
    return {"analysis": text, "key_finding": key, "tokens_used": response.usage.total_tokens}


def analyze_market(ctx: StepContext) -> dict[str, Any]:
    """Analyze market opportunity - runs IN PARALLEL with security and tech."""
    adapter = ClaudeAdapter(model=MODEL)
    plan = cast(dict[str, Any], ctx.inputs["plan_research"])

    response = adapter.call(
        f"You are a market analyst. In 3-4 sentences, analyze:\n"
        f"{plan['market_focus']}\n\n"
        f"End with one key finding sentence starting with 'KEY FINDING:'",
        max_tokens=300,
    )
    ctx.increment_llm_calls()

    text = response.text
    key = _extract_key_finding(text)
    return {"analysis": text, "key_finding": key, "tokens_used": response.usage.total_tokens}


def analyze_technology(ctx: StepContext) -> dict[str, Any]:
    """Analyze technology landscape - runs IN PARALLEL with security and market."""
    adapter = ClaudeAdapter(model=MODEL)
    plan = cast(dict[str, Any], ctx.inputs["plan_research"])

    response = adapter.call(
        f"You are a technology analyst. In 3-4 sentences, analyze:\n"
        f"{plan['tech_focus']}\n\n"
        f"End with one key finding sentence starting with 'KEY FINDING:'",
        max_tokens=300,
    )
    ctx.increment_llm_calls()

    text = response.text
    key = _extract_key_finding(text)
    return {"analysis": text, "key_finding": key, "tokens_used": response.usage.total_tokens}


def synthesize(ctx: StepContext) -> dict[str, Any]:
    """Combine all three parallel analyses into a final summary."""
    adapter = ClaudeAdapter(model=MODEL)

    security = cast(dict[str, Any], ctx.inputs["analyze_security"])
    market = cast(dict[str, Any], ctx.inputs["analyze_market"])
    tech = cast(dict[str, Any], ctx.inputs["analyze_technology"])

    response = adapter.call(
        f"You are a senior analyst. Synthesize these three analyses into a "
        f"4-sentence executive summary:\n\n"
        f"SECURITY: {security['key_finding']}\n"
        f"MARKET: {market['key_finding']}\n"
        f"TECHNOLOGY: {tech['key_finding']}",
        max_tokens=400,
    )
    ctx.increment_llm_calls()

    total_tokens = (
        security["tokens_used"]
        + market["tokens_used"]
        + tech["tokens_used"]
        + response.usage.total_tokens
    )

    return {"summary": response.text, "total_tokens": total_tokens}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_key_finding(text: str) -> str:
    """Pull out the KEY FINDING line, or use the last sentence as fallback."""
    for line in text.splitlines():
        if "KEY FINDING:" in line.upper():
            return line.split(":", 1)[1].strip() if ":" in line else line.strip()
    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    return (sentences[-1] + ".") if sentences else text[:200]


# ---------------------------------------------------------------------------
# Workflow with RunLogger
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log_dir = Path(tempfile.mkdtemp(prefix="kairos_logs_"))

    # Create a RunLogger with console + file sinks
    logger = RunLogger(
        sinks=[
            ConsoleSink(stream=sys.stdout, verbosity=LogVerbosity.NORMAL),
            JSONLinesSink(str(log_dir)),
        ],
        verbosity=LogVerbosity.NORMAL,
    )

    workflow = Workflow(
        name="concurrent-research-logged",
        steps=[
            Step(
                name="plan_research",
                action=plan_research,
                failure_policy=RETRY_POLICY,
            ),
            Step(
                name="analyze_security",
                action=analyze_security,
                depends_on=["plan_research"],
                output_contract=analysis_schema,
                failure_policy=RETRY_POLICY,
                parallel=True,
            ),
            Step(
                name="analyze_market",
                action=analyze_market,
                depends_on=["plan_research"],
                output_contract=analysis_schema,
                failure_policy=RETRY_POLICY,
                parallel=True,
            ),
            Step(
                name="analyze_technology",
                action=analyze_technology,
                depends_on=["plan_research"],
                output_contract=analysis_schema,
                failure_policy=RETRY_POLICY,
                parallel=True,
            ),
            Step(
                name="synthesize",
                action=synthesize,
                depends_on=["analyze_security", "analyze_market", "analyze_technology"],
                output_contract=synthesis_schema,
                failure_policy=RETRY_POLICY,
            ),
        ],
        hooks=[logger],
        max_concurrency=3,
        sensitive_keys=["*api_key*"],
    )

    print("=" * 70)
    print("  CONCURRENT CLAUDE + RUN LOGGER (v0.4.0)")
    print("  3 parallel API calls with structured lifecycle logging")
    print("=" * 70)
    print()

    query = "AI agent orchestration frameworks"
    print(f"Query: {query}")
    print(f"Log directory: {log_dir}")
    print()
    print("-" * 70)
    print("LIVE LOG OUTPUT (ConsoleSink at NORMAL verbosity)")
    print("-" * 70)
    print()

    wall_start = time.perf_counter()
    result = workflow.run({"query": query})
    wall_end = time.perf_counter()

    print()
    print("-" * 70)
    print("WORKFLOW RESULTS")
    print("-" * 70)
    print()
    print(f"Status:         {result.status.value}")
    print(f"Wall-clock:     {wall_end - wall_start:.1f}s")
    print(f"Kairos duration:{result.duration_ms:.0f}ms")
    print(f"LLM calls:      {result.llm_calls}")
    print()

    # --- RunLog inspection ---
    print("-" * 70)
    print("RUNLOG INSPECTION (structured data from the logger)")
    print("-" * 70)
    print()

    run_log = logger.get_run_log()
    assert run_log is not None

    print(f"Run ID:        {run_log.run_id}")
    print(f"Total events:  {len(run_log.events)}")
    print()

    # Summary
    s = run_log.summary
    print("Run Summary:")
    print(f"  Completed steps: {s.completed_steps}/{s.total_steps}")
    print(f"  Failed steps:    {s.failed_steps}")
    print(f"  Total retries:   {s.total_retries}")
    print(f"  Duration:        {s.total_duration_ms:.0f}ms")
    print()

    # Event timeline
    print("Event Timeline:")
    for i, event in enumerate(run_log.events, 1):
        step = event.step_id or "(workflow)"
        ts = event.timestamp.strftime("%H:%M:%S.%f")[:-3]
        print(f"  {i:2d}. {ts} [{event.level}] {event.event_type:25s} {step}")
    print()

    # --- Key findings ---
    print("=" * 70)
    print("KEY FINDINGS")
    print("=" * 70)
    for name in ["analyze_security", "analyze_market", "analyze_technology"]:
        sr = result.step_results[name]
        output = cast(dict[str, Any], sr.output)
        print(f"\n  [{name.replace('analyze_', '').upper()}]")
        print(f"  {output['key_finding']}")

    synth = cast(dict[str, Any], result.step_results["synthesize"].output)
    print()
    print("=" * 70)
    print("EXECUTIVE SUMMARY")
    print("=" * 70)
    print(f"\n{synth['summary']}")
    print(f"\nTotal tokens: {synth['total_tokens']}")

    # --- JSON Lines file ---
    jsonl_files = list(log_dir.glob("*.jsonl"))
    if jsonl_files:
        print(f"\nJSON Lines log: {jsonl_files[0]}")
        line_count = len(jsonl_files[0].read_text(encoding="utf-8").strip().split("\n"))
        print(f"  {line_count} events written to disk")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
