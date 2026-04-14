"""Real Claude API example — CONCURRENT STEP EXECUTION (v0.3.0).

This example exists specifically to test and demonstrate Kairos concurrent
step execution. It runs three independent Claude API calls IN PARALLEL,
then synthesizes the results.

Workflow graph:
    plan_research ──> analyze_security  (parallel=True) ──>
                 ──> analyze_market     (parallel=True) ──> synthesize
                 ──> analyze_technology (parallel=True) ──>

Without concurrency: ~12-20s (4 sequential API calls)
With concurrency:    ~6-10s  (1 + 1 parallel batch of 3 + 1 = 3 serial waits)

To compare: run this example, then comment out the three ``parallel=True``
lines in the workflow definition below (search for ``# <-- CONCURRENT``)
and run again to see the sequential baseline.

REQUIRES:
    1. pip install kairos-ai[anthropic]
    2. Set ANTHROPIC_API_KEY in your environment:
       Windows PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-your-key"
       Linux/Mac: export ANTHROPIC_API_KEY="sk-ant-your-key"

To run:
    py -3 examples/real_claude_concurrent.py
"""

from typing import Any, cast

from kairos import (
    FailureAction,
    FailurePolicy,
    Schema,
    Step,
    StepContext,
    Workflow,
    WorkflowStatus,
)
from kairos import validators as v
from kairos.adapters.claude import ClaudeAdapter

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
# Step 1: Plan — produces the research topic (sequential, runs first)
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

    lines = [line.strip() for line in response.text.strip().splitlines() if line.strip()]
    # Pad to 3 if the LLM returned fewer lines
    while len(lines) < 3:
        lines.append(f"Analyze {query} from a general perspective.")

    return {
        "security_focus": lines[0],
        "market_focus": lines[1],
        "tech_focus": lines[2],
        "tokens_used": response.usage.total_tokens,
    }


# ---------------------------------------------------------------------------
# Steps 2-4: Three PARALLEL analysis steps (this is what we're testing)
# ---------------------------------------------------------------------------


def analyze_security(ctx: StepContext) -> dict[str, Any]:
    """Analyze security implications — runs IN PARALLEL with market and tech."""
    adapter = ClaudeAdapter(model=MODEL)
    plan = cast(dict[str, Any], ctx.inputs["plan_research"])

    response = adapter.call(
        f"You are a cybersecurity analyst. In 3-4 sentences, analyze:\n"
        f"{plan['security_focus']}\n\n"
        f"End with one key finding sentence starting with 'KEY FINDING:'",
        max_tokens=300,
    )

    text = response.text
    key = _extract_key_finding(text)
    return {"analysis": text, "key_finding": key, "tokens_used": response.usage.total_tokens}


def analyze_market(ctx: StepContext) -> dict[str, Any]:
    """Analyze market opportunity — runs IN PARALLEL with security and tech."""
    adapter = ClaudeAdapter(model=MODEL)
    plan = cast(dict[str, Any], ctx.inputs["plan_research"])

    response = adapter.call(
        f"You are a market analyst. In 3-4 sentences, analyze:\n"
        f"{plan['market_focus']}\n\n"
        f"End with one key finding sentence starting with 'KEY FINDING:'",
        max_tokens=300,
    )

    text = response.text
    key = _extract_key_finding(text)
    return {"analysis": text, "key_finding": key, "tokens_used": response.usage.total_tokens}


def analyze_technology(ctx: StepContext) -> dict[str, Any]:
    """Analyze technology landscape — runs IN PARALLEL with security and market."""
    adapter = ClaudeAdapter(model=MODEL)
    plan = cast(dict[str, Any], ctx.inputs["plan_research"])

    response = adapter.call(
        f"You are a technology analyst. In 3-4 sentences, analyze:\n"
        f"{plan['tech_focus']}\n\n"
        f"End with one key finding sentence starting with 'KEY FINDING:'",
        max_tokens=300,
    )

    text = response.text
    key = _extract_key_finding(text)
    return {"analysis": text, "key_finding": key, "tokens_used": response.usage.total_tokens}


# ---------------------------------------------------------------------------
# Step 5: Synthesize — runs after all parallel steps complete
# ---------------------------------------------------------------------------


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
    # Fallback: last sentence
    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    return (sentences[-1] + ".") if sentences else text[:200]


# ---------------------------------------------------------------------------
# Workflow definition — the three analyze steps are parallel=True
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="concurrent-research",
    steps=[
        Step(
            name="plan_research",
            action=plan_research,
            failure_policy=RETRY_POLICY,
        ),
        # --- These three run CONCURRENTLY (parallel=True) ---
        Step(
            name="analyze_security",
            action=analyze_security,
            depends_on=["plan_research"],
            output_contract=analysis_schema,
            failure_policy=RETRY_POLICY,
            parallel=True,  # <-- CONCURRENT
        ),
        Step(
            name="analyze_market",
            action=analyze_market,
            depends_on=["plan_research"],
            output_contract=analysis_schema,
            failure_policy=RETRY_POLICY,
            parallel=True,  # <-- CONCURRENT
        ),
        Step(
            name="analyze_technology",
            action=analyze_technology,
            depends_on=["plan_research"],
            output_contract=analysis_schema,
            failure_policy=RETRY_POLICY,
            parallel=True,  # <-- CONCURRENT
        ),
        # --- Synthesis waits for all three to complete ---
        Step(
            name="synthesize",
            action=synthesize,
            depends_on=["analyze_security", "analyze_market", "analyze_technology"],
            output_contract=synthesis_schema,
            failure_policy=RETRY_POLICY,
        ),
    ],
    max_concurrency=3,
    sensitive_keys=["*api_key*"],
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    print("=" * 60)
    print("  CONCURRENT STEP EXECUTION TEST (v0.3.0)")
    print("  3 parallel Claude API calls + synthesis")
    print("=" * 60)
    print()
    print("Workflow graph:")
    print("  plan_research")
    print("       |")
    print("       +---> analyze_security  (parallel)")
    print("       +---> analyze_market    (parallel)")
    print("       +---> analyze_technology(parallel)")
    print("       |")
    print("  synthesize")
    print()

    query = "AI agent orchestration frameworks"

    print(f"Query: {query}")
    print()

    wall_start = time.perf_counter()

    result = workflow.run({"query": query})

    wall_end = time.perf_counter()
    wall_seconds = wall_end - wall_start

    print(f"Status: {result.status.value}")
    print(f"Kairos duration: {result.duration_ms:.0f}ms")
    print(f"Wall-clock time: {wall_seconds:.1f}s")
    print()

    # --- Per-step timing ---
    print("Step Timings:")
    print("-" * 50)
    for name in [
        "plan_research",
        "analyze_security",
        "analyze_market",
        "analyze_technology",
        "synthesize",
    ]:
        sr = result.step_results[name]
        print(f"  {name:25s}  {sr.duration_ms:7.0f}ms  {sr.status.value}")

    # --- Concurrency proof ---
    sec = result.step_results["analyze_security"].duration_ms
    mkt = result.step_results["analyze_market"].duration_ms
    tech = result.step_results["analyze_technology"].duration_ms
    sequential_estimate = sec + mkt + tech
    parallel_actual = max(sec, mkt, tech)  # wall-clock for the parallel batch

    print()
    print("Concurrency Analysis:")
    print("-" * 50)
    print(f"  Sum of 3 analysis steps (if sequential): {sequential_estimate:.0f}ms")
    print(f"  Longest analysis step (parallel bound):  {parallel_actual:.0f}ms")
    print(f"  Speedup:  {sequential_estimate / parallel_actual:.1f}x")
    print()

    # --- Results ---
    print("=" * 50)
    print("Key Findings:")
    print("=" * 50)
    for name in ["analyze_security", "analyze_market", "analyze_technology"]:
        sr = result.step_results[name]
        output = cast(dict[str, Any], sr.output)
        print(f"\n  [{name.replace('analyze_', '').upper()}]")
        print(f"  {output['key_finding']}")

    synth = cast(dict[str, Any], result.step_results["synthesize"].output)
    print()
    print("=" * 50)
    print("Executive Summary:")
    print("=" * 50)
    print(f"\n{synth['summary']}")
    print(f"\nTotal tokens: {synth['total_tokens']}")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
