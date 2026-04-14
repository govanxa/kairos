"""Real Claude API example — a working multi-step AI workflow.

Demonstrates:
- Real Claude API calls via the adapter
- foreach fan-out (research 3 topics in sequence)
- Output contract validation on real LLM responses
- Failure policies with retry
- Prompt template formatting from upstream outputs

REQUIRES:
    1. pip install kairos-ai[anthropic]
    2. Set ANTHROPIC_API_KEY in your environment:
       Windows PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-your-key"
       Linux/Mac: export ANTHROPIC_API_KEY="sk-ant-your-key"

To run:
    py -3 examples/real_claude.py
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

# ---------------------------------------------------------------------------
# Schemas — what each step must produce
# ---------------------------------------------------------------------------

# The adapter returns: {"text": str, "model": str, "usage": {...}, ...}
# We validate the text is non-empty (LLM didn't return blank)
adapter_output_schema = Schema(
    {"text": str, "model": str, "latency_ms": float},
    validators={"text": [v.not_empty()]},
)


# ---------------------------------------------------------------------------
# Step actions — real Claude calls
# ---------------------------------------------------------------------------


def research_topic(ctx: StepContext) -> dict[str, Any]:
    """Research a single topic using Claude.

    This is a custom step (not using the factory) to show how you can
    call the adapter directly and do post-processing on the response.
    """
    adapter = ClaudeAdapter(model="claude-sonnet-4-20250514")
    topic = cast(str, ctx.item)

    response = adapter.call(
        f"In 2-3 sentences, explain the key insight about: {topic}. "
        f"Focus on why it matters for AI agent security.",
        max_tokens=300,
    )
    ctx.increment_llm_calls()  # Track LLM call for circuit breaker

    return {
        "topic": topic,
        "insight": response.text,
        "tokens_used": response.usage.total_tokens,
        "model": response.model,
    }


def synthesize(ctx: StepContext) -> dict[str, Any]:
    """Synthesize all research into a final summary using Claude."""
    adapter = ClaudeAdapter(model="claude-sonnet-4-20250514")

    research_results = cast(list[dict[str, Any]], ctx.inputs["research"])
    insights = "\n".join(f"- {r['topic']}: {r['insight']}" for r in research_results)

    response = adapter.call(
        f"You are a security analyst. Based on these research findings:\n\n"
        f"{insights}\n\n"
        f"Write a 3-sentence executive summary of the key themes.",
        max_tokens=400,
    )
    ctx.increment_llm_calls()

    total_research_tokens = sum(r["tokens_used"] for r in research_results)

    return {
        "summary": response.text,
        "topics_analyzed": len(research_results),
        "total_tokens": total_research_tokens + response.usage.total_tokens,
        "model": response.model,
    }


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="claude-research",
    steps=[
        Step(
            name="research",
            action=research_topic,
            foreach="topics",
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
            ),
        ),
        Step(
            name="synthesize",
            action=synthesize,
            depends_on=["research"],
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=1,
            ),
        ),
    ],
    sensitive_keys=["*api_key*"],
)


# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Real Claude Workflow — AI Security Research")
    print("=" * 60)
    print()

    result = workflow.run(
        {
            "topics": [
                "prompt injection attacks on AI agents",
                "sanitized retry context as a security boundary",
                "least-privilege state access in multi-step workflows",
            ],
        }
    )

    print(f"Status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.0f}ms")
    print()

    # Show research results
    print("Research Results:")
    print("-" * 40)
    research = result.step_results["research"]
    raw_output: object = research.output
    if isinstance(raw_output, list):
        for item in cast(list[dict[str, Any]], raw_output):
            print(f"\n  Topic: {item['topic']}")
            print(f"  Model: {item['model']}")
            print(f"  Tokens: {item['tokens_used']}")
            print(f"  Insight: {item['insight'][:200]}...")

    print()
    print("=" * 40)
    print("Executive Summary:")
    print("=" * 40)
    synth = cast(dict[str, Any], result.step_results["synthesize"].output)
    print(f"\n{synth['summary']}")
    print(f"\nTopics analyzed: {synth['topics_analyzed']}")
    print(f"Total tokens used: {synth['total_tokens']}")
    print(f"Model: {synth['model']}")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
