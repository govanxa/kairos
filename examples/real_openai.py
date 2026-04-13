"""Real OpenAI API example — success AND retry demonstration.

Demonstrates:
- Real OpenAI API calls via the adapter
- A successful workflow
- A step that FAILS validation, retries, and succeeds on the 2nd attempt
- How Kairos validates LLM output and recovers automatically
- Token usage tracking across steps

The retry demo works by having a strict custom validator that rejects
the first response (simulating an LLM returning slightly wrong data),
then accepts the retry (simulating the LLM self-correcting with fresh context).

REQUIRES:
    1. pip install kairos-ai[openai]
    2. Set OPENAI_API_KEY in your environment:
       Windows PowerShell: $env:OPENAI_API_KEY = "sk-your-key"
       Linux/Mac: export OPENAI_API_KEY="sk-your-key"

To run:
    py -3 examples/real_openai.py
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
from kairos.adapters.openai_adapter import OpenAIAdapter

# ---------------------------------------------------------------------------
# Attempt tracker — makes the retry demo deterministic
# ---------------------------------------------------------------------------


class AttemptTracker:
    """Tracks how many times a step has been called.

    Used to make the first attempt fail validation (simulating an LLM
    returning imperfect data) and the retry succeed (simulating the
    LLM self-correcting). This makes the retry demo reliable — real
    LLM output is unpredictable, but the pattern is realistic.
    """

    def __init__(self) -> None:
        self.attempts: int = 0

    def increment(self) -> int:
        self.attempts += 1
        return self.attempts


# One tracker per step that needs retry behavior
analyze_tracker = AttemptTracker()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

analysis_schema = Schema(
    {
        "summary": str,
        "risk_level": str,
        "recommendation": str,
    },
    validators={
        "summary": [v.not_empty(), v.length(min=20)],
        "risk_level": [v.one_of(["low", "medium", "high", "critical"])],
        "recommendation": [v.not_empty()],
    },
)


# ---------------------------------------------------------------------------
# Step actions
# ---------------------------------------------------------------------------


def gather_context(ctx: StepContext) -> dict[str, Any]:
    """Step 1: Call OpenAI to gather context about the topic."""
    adapter = OpenAIAdapter(model="gpt-4o-mini")

    topic = cast(str, ctx.state.get("topic"))
    response = adapter.call(
        f"In 2-3 sentences, describe the current state of: {topic}",
        max_tokens=200,
    )

    return {
        "context": response.text,
        "model": response.model,
        "tokens": response.usage.total_tokens,
    }


def analyze_risk(ctx: StepContext) -> dict[str, Any]:
    """Step 2: Analyze risk — DELIBERATELY FAILS on first attempt.

    On the first attempt, returns a risk_level of "MEDIUM" (uppercase).
    The schema validator requires lowercase ("medium"), so validation
    FAILS. Kairos retries automatically per the failure policy.

    On the second attempt, returns the correct lowercase format.
    This demonstrates Kairos catching bad LLM output and recovering.

    In real life, this happens when an LLM returns slightly malformed
    data (wrong case, extra whitespace, unexpected format). The retry
    gives it another chance with fresh context.
    """
    adapter = OpenAIAdapter(model="gpt-4o-mini")

    context_data = cast(dict[str, Any], ctx.inputs["gather_context"])
    context = context_data["context"]

    attempt = analyze_tracker.increment()

    response = adapter.call(
        f"Based on this context:\n{context}\n\nProvide a security risk analysis. Be concise.",
        max_tokens=300,
    )

    if attempt == 1:
        # First attempt: return UPPERCASE risk_level — validation will reject it
        print("    [attempt 1] Returning 'MEDIUM' (uppercase) — validation will fail...")
        return {
            "summary": response.text,
            "risk_level": "MEDIUM",  # WRONG — validator expects lowercase
            "recommendation": "Review security controls",
        }
    else:
        # Second attempt: return correct lowercase — validation will pass
        print("    [attempt 2] Returning 'medium' (lowercase) — validation will pass!")
        return {
            "summary": response.text,
            "risk_level": "medium",  # CORRECT
            "recommendation": "Implement input validation and output contracts on all LLM calls",
        }


def write_report(ctx: StepContext) -> dict[str, Any]:
    """Step 3: Write the final report from the analysis."""
    adapter = OpenAIAdapter(model="gpt-4o-mini")

    analysis = cast(dict[str, Any], ctx.inputs["analyze_risk"])
    context_data = cast(dict[str, Any], ctx.inputs["gather_context"])

    response = adapter.call(
        f"Write a brief security report (3-4 sentences) based on:\n"
        f"Context: {context_data['context']}\n"
        f"Risk level: {analysis['risk_level']}\n"
        f"Analysis: {analysis['summary']}\n"
        f"Recommendation: {analysis['recommendation']}",
        max_tokens=400,
    )

    return {
        "report": response.text,
        "risk_level": analysis["risk_level"],
        "model": response.model,
        "tokens": response.usage.total_tokens,
    }


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="openai-security-analysis",
    steps=[
        # Step 1: Succeeds normally
        Step(
            name="gather_context",
            action=gather_context,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
            ),
        ),
        # Step 2: FAILS on first attempt (bad risk_level), SUCCEEDS on retry
        Step(
            name="analyze_risk",
            action=analyze_risk,
            depends_on=["gather_context"],
            output_contract=analysis_schema,  # <-- THIS catches the bad data
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.RETRY,  # <-- retry on validation failure
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
            ),
        ),
        # Step 3: Runs after retry succeeds
        Step(
            name="write_report",
            action=write_report,
            depends_on=["gather_context", "analyze_risk"],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=1,
            ),
        ),
    ],
    sensitive_keys=["*api_key*", "*token*"],
)


# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ----- Part 1: The full workflow with retry -----
    print("=" * 60)
    print("  Real OpenAI Workflow — With Retry Demo")
    print("=" * 60)
    print()
    print("Watch the analyze_risk step: it will FAIL on attempt 1")
    print("(returns 'MEDIUM' instead of 'medium'), then SUCCEED on retry.")
    print()

    result = workflow.run({"topic": "AI agent security in production systems"})

    print()
    print(f"Status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.0f}ms")
    print()

    # Show each step
    print("Step Results:")
    print("-" * 40)
    for name, sr in result.step_results.items():
        status_icon = "+" if sr.status.value == "completed" else "X"
        attempts = len(sr.attempts)
        print(f"  [{status_icon}] {name} — {sr.status.value}, {attempts} attempt(s)")

        # Highlight the retry
        if attempts > 1:
            print("      ^ RETRIED! First attempt failed validation, second succeeded.")

    print()

    # Show the retry details for analyze_risk
    analyze_result = result.step_results["analyze_risk"]
    print("Retry Details (analyze_risk):")
    print("-" * 40)
    for i, attempt in enumerate(analyze_result.attempts):
        print(f"  Attempt {i + 1}: {attempt.status.value}")
        if attempt.error_type:
            print(f"    Error: {attempt.error_type}")
            msg = attempt.error_message or ""
            print(f"    Message: {msg[:100]}...")
    print()

    # Show the final report
    report = cast(dict[str, Any], result.step_results["write_report"].output)
    print("=" * 40)
    print("Final Report:")
    print("=" * 40)
    print(f"\nRisk Level: {report['risk_level']}")
    print(f"Model: {report['model']}")
    print(f"Tokens: {report['tokens']}")
    print(f"\n{report['report']}")
    print()

    # Show total token usage
    gather_output = cast(dict[str, Any], result.step_results["gather_context"].output)
    total_tokens = gather_output.get("tokens", 0) + report.get("tokens", 0)
    print(f"Total tokens across all steps: ~{total_tokens}")
    print()

    # ----- The point -----
    print("=" * 60)
    print("  WHAT JUST HAPPENED")
    print("=" * 60)
    print()
    print("  1. gather_context called OpenAI — succeeded on first try")
    print("  2. analyze_risk called OpenAI — returned 'MEDIUM' (uppercase)")
    print("     Kairos validated the output against analysis_schema:")
    print("     -> risk_level must be one_of(['low','medium','high','critical'])")
    print("     -> 'MEDIUM' is NOT in that list (case-sensitive)")
    print("     -> Validation FAILED -> FailureAction.RETRY triggered")
    print("  3. analyze_risk retried — returned 'medium' (lowercase)")
    print("     -> Validation PASSED -> step completed")
    print("  4. write_report ran with the validated data — succeeded")
    print()
    print("  Without Kairos: 'MEDIUM' flows to the report unchecked.")
    print("  With Kairos: bad data caught, step retried, correct data flows.")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
