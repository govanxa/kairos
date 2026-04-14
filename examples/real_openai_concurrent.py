"""Real OpenAI API example — CONCURRENT STEP EXECUTION (v0.3.0).

This example exists specifically to test and demonstrate Kairos concurrent
step execution with the OpenAI adapter. It evaluates product launch
readiness by running four independent assessment tracks IN PARALLEL,
then produces a go/no-go recommendation.

Workflow graph:
    describe_product ──> eval_technical    (parallel=True) ──>
                    ──> eval_market        (parallel=True) ──> recommend
                    ──> eval_compliance    (parallel=True) ──>
                    ──> eval_operations    (parallel=True) ──>

Without concurrency: ~10-15s (6 sequential API calls)
With concurrency:    ~5-8s   (1 + 1 parallel batch of 4 + 1 = 3 serial waits)

To compare: run this example, then comment out the four ``parallel=True``
lines in the workflow definition below (search for ``# <-- CONCURRENT``)
and run again to see the sequential baseline.

REQUIRES:
    1. pip install kairos-ai[openai]
    2. Set OPENAI_API_KEY in your environment:
       Windows PowerShell: $env:OPENAI_API_KEY = "sk-your-key"
       Linux/Mac: export OPENAI_API_KEY="sk-your-key"

To run:
    py -3 examples/real_openai_concurrent.py
"""

from collections.abc import Callable
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
from kairos.adapters.openai_adapter import OpenAIAdapter

MODEL = "gpt-4o-mini"
RETRY_POLICY = FailurePolicy(on_execution_fail=FailureAction.RETRY, max_retries=1)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

eval_schema = Schema(
    {"verdict": str, "reasoning": str, "risk_score": int},
    validators={
        "verdict": [v.one_of(["go", "no-go", "conditional"])],
        "reasoning": [v.not_empty(), v.length(min=20)],
        "risk_score": [v.range(min=1, max=10)],
    },
)

recommendation_schema = Schema(
    {"decision": str, "summary": str, "total_risk": int},
    validators={
        "decision": [v.one_of(["LAUNCH", "DELAY", "CANCEL"])],
        "summary": [v.not_empty()],
        "total_risk": [v.range(min=4, max=40)],
    },
)

# ---------------------------------------------------------------------------
# Step 1: Describe the product (sequential, runs first)
# ---------------------------------------------------------------------------


def describe_product(ctx: StepContext) -> dict[str, Any]:
    """Generate a structured product description from the user's pitch."""
    adapter = OpenAIAdapter(model=MODEL)
    product = cast(str, ctx.state.get("product"))

    response = adapter.call(
        f"You are a product manager. Given this product idea:\n"
        f"'{product}'\n\n"
        f"Write a 3-sentence product description covering: what it does, "
        f"who it's for, and how it's different from existing solutions.",
        max_tokens=200,
    )

    return {
        "description": response.text,
        "tokens_used": response.usage.total_tokens,
    }


# ---------------------------------------------------------------------------
# Steps 2-5: Four PARALLEL evaluation tracks (this is what we're testing)
# ---------------------------------------------------------------------------


def _make_evaluator(role: str, focus: str) -> Callable[[StepContext], dict[str, Any]]:
    """Factory for parallel evaluation step actions.

    Each evaluator calls OpenAI with a role-specific prompt and returns
    a structured verdict. Using a factory avoids copy-pasting 4 identical
    functions that differ only in the role and focus area.
    """

    def action(ctx: StepContext) -> dict[str, Any]:
        adapter = OpenAIAdapter(model=MODEL)
        product_data = cast(dict[str, Any], ctx.inputs["describe_product"])
        description = product_data["description"]

        response = adapter.call(
            f"You are a {role}. Evaluate this product for launch readiness "
            f"from a {focus} perspective:\n\n"
            f"{description}\n\n"
            f"Respond with EXACTLY this JSON format (no markdown, no code fences):\n"
            f'{{"verdict": "go" or "no-go" or "conditional", '
            f'"reasoning": "2-3 sentences explaining your assessment", '
            f'"risk_score": 1-10 where 1=minimal risk and 10=critical risk}}',
            max_tokens=300,
        )

        # Parse the JSON response — fall back to structured extraction if needed
        import json

        try:
            result = json.loads(response.text)
        except json.JSONDecodeError:
            # LLM didn't return valid JSON — build a structured response
            result = {
                "verdict": "conditional",
                "reasoning": response.text[:200],
                "risk_score": 5,
            }

        # Ensure types are correct for schema validation
        return {
            "verdict": str(result.get("verdict", "conditional")).lower(),
            "reasoning": str(result.get("reasoning", response.text[:200])),
            "risk_score": int(result.get("risk_score", 5)),
            "tokens_used": response.usage.total_tokens,
        }

    return action


# Create the four evaluator actions
eval_technical = _make_evaluator(
    "senior software architect",
    "technical feasibility, scalability, and architecture",
)
eval_market = _make_evaluator(
    "market analyst",
    "market size, competition, and product-market fit",
)
eval_compliance = _make_evaluator(
    "compliance officer",
    "regulatory requirements, data privacy, and legal risk",
)
eval_operations = _make_evaluator(
    "VP of operations",
    "operational readiness, support capacity, and infrastructure",
)


# ---------------------------------------------------------------------------
# Step 6: Recommend — runs after all parallel evaluations complete
# ---------------------------------------------------------------------------


def recommend(ctx: StepContext) -> dict[str, Any]:
    """Synthesize the four parallel evaluations into a launch decision."""
    adapter = OpenAIAdapter(model=MODEL)

    tracks = ["eval_technical", "eval_market", "eval_compliance", "eval_operations"]
    evals = {name: cast(dict[str, Any], ctx.inputs[name]) for name in tracks}

    eval_summary = "\n".join(
        f"- {name.replace('eval_', '').upper()}: "
        f"verdict={e['verdict']}, risk={e['risk_score']}/10, "
        f"reasoning={e['reasoning'][:100]}"
        for name, e in evals.items()
    )

    total_risk = sum(e["risk_score"] for e in evals.values())

    response = adapter.call(
        f"You are a CEO making a launch decision. Based on these evaluations:\n\n"
        f"{eval_summary}\n\n"
        f"Total risk score: {total_risk}/40\n\n"
        f"Respond with EXACTLY this JSON format (no markdown, no code fences):\n"
        f'{{"decision": "LAUNCH" or "DELAY" or "CANCEL", '
        f'"summary": "3-4 sentence executive summary of the decision"}}',
        max_tokens=300,
    )

    import json

    try:
        result = json.loads(response.text)
    except json.JSONDecodeError:
        result = {"decision": "DELAY", "summary": response.text[:300]}

    total_tokens = sum(e["tokens_used"] for e in evals.values())
    total_tokens += response.usage.total_tokens

    return {
        "decision": str(result.get("decision", "DELAY")).upper(),
        "summary": str(result.get("summary", response.text[:300])),
        "total_risk": total_risk,
        "total_tokens": total_tokens,
    }


# ---------------------------------------------------------------------------
# Workflow — the four eval steps are parallel=True
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="concurrent-launch-review",
    steps=[
        Step(
            name="describe_product",
            action=describe_product,
            failure_policy=RETRY_POLICY,
        ),
        # --- These four run CONCURRENTLY (parallel=True) ---
        Step(
            name="eval_technical",
            action=eval_technical,
            depends_on=["describe_product"],
            output_contract=eval_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                on_validation_fail=FailureAction.RETRY,
                max_retries=2,
            ),
            parallel=True,  # <-- CONCURRENT
        ),
        Step(
            name="eval_market",
            action=eval_market,
            depends_on=["describe_product"],
            output_contract=eval_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                on_validation_fail=FailureAction.RETRY,
                max_retries=2,
            ),
            parallel=True,  # <-- CONCURRENT
        ),
        Step(
            name="eval_compliance",
            action=eval_compliance,
            depends_on=["describe_product"],
            output_contract=eval_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                on_validation_fail=FailureAction.RETRY,
                max_retries=2,
            ),
            parallel=True,  # <-- CONCURRENT
        ),
        Step(
            name="eval_operations",
            action=eval_operations,
            depends_on=["describe_product"],
            output_contract=eval_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                on_validation_fail=FailureAction.RETRY,
                max_retries=2,
            ),
            parallel=True,  # <-- CONCURRENT
        ),
        # --- Recommendation waits for all four to complete ---
        Step(
            name="recommend",
            action=recommend,
            depends_on=[
                "eval_technical",
                "eval_market",
                "eval_compliance",
                "eval_operations",
            ],
            output_contract=recommendation_schema,
            failure_policy=RETRY_POLICY,
        ),
    ],
    max_concurrency=4,
    sensitive_keys=["*api_key*"],
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    print("=" * 60)
    print("  CONCURRENT STEP EXECUTION TEST (v0.3.0)")
    print("  4 parallel OpenAI evaluation tracks + recommendation")
    print("=" * 60)
    print()
    print("Workflow graph:")
    print("  describe_product")
    print("       |")
    print("       +---> eval_technical   (parallel)")
    print("       +---> eval_market      (parallel)")
    print("       +---> eval_compliance  (parallel)")
    print("       +---> eval_operations  (parallel)")
    print("       |")
    print("  recommend")
    print()

    wall_start = time.perf_counter()

    result = workflow.run(
        {
            "product": "An AI-powered code review tool that detects security "
            "vulnerabilities in pull requests using static analysis and LLM reasoning"
        }
    )

    wall_end = time.perf_counter()
    wall_seconds = wall_end - wall_start

    print(f"Status: {result.status.value}")
    print(f"Kairos duration: {result.duration_ms:.0f}ms")
    print(f"Wall-clock time: {wall_seconds:.1f}s")
    print()

    # --- Per-step timing ---
    print("Step Timings:")
    print("-" * 55)
    step_order = [
        "describe_product",
        "eval_technical",
        "eval_market",
        "eval_compliance",
        "eval_operations",
        "recommend",
    ]
    for name in step_order:
        sr = result.step_results[name]
        attempts = len(sr.attempts)
        retry_note = f"  ({attempts} attempts)" if attempts > 1 else ""
        print(f"  {name:22s}  {sr.duration_ms:7.0f}ms  {sr.status.value}{retry_note}")

    # --- Concurrency proof ---
    eval_names = [
        "eval_technical",
        "eval_market",
        "eval_compliance",
        "eval_operations",
    ]
    eval_durations = [result.step_results[n].duration_ms for n in eval_names]
    sequential_estimate = sum(eval_durations)
    parallel_actual = max(eval_durations)

    print()
    print("Concurrency Analysis:")
    print("-" * 55)
    print(f"  Sum of 4 eval steps (if sequential): {sequential_estimate:,.0f}ms")
    print(f"  Longest eval step (parallel bound):  {parallel_actual:,.0f}ms")
    if parallel_actual > 0:
        print(f"  Speedup:  {sequential_estimate / parallel_actual:.1f}x")
    print()

    # --- Evaluation results ---
    print("=" * 55)
    print("Evaluation Results:")
    print("=" * 55)
    for name in eval_names:
        output = cast(dict[str, Any], result.step_results[name].output)
        label = name.replace("eval_", "").upper()
        print(f"\n  [{label}]")
        print(f"  Verdict: {output['verdict']}  |  Risk: {output['risk_score']}/10")
        print(f"  {output['reasoning'][:120]}")

    # --- Final recommendation ---
    rec = cast(dict[str, Any], result.step_results["recommend"].output)
    print()
    print("=" * 55)
    print(f"  DECISION: {rec['decision']}")
    print(f"  Total Risk Score: {rec['total_risk']}/40")
    print("=" * 55)
    print(f"\n{rec['summary']}")
    print(f"\nTotal tokens: {rec.get('total_tokens', 'N/A')}")
    print(f"LLM calls tracked: {result.llm_calls}")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
