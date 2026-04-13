"""LLM workflow example — using adapters with validation and retry.

Demonstrates:
- Claude and OpenAI adapters in the same workflow
- Output contract validation on LLM responses
- Failure policies with retry on LLM errors
- Prompt template formatting from upstream step outputs
- foreach fan-out with LLM calls
- How adapters normalize responses into ModelResponse

This example uses MOCKED adapters so it runs without API keys.
For real LLM calls, see examples/real_claude.py and examples/real_openai.py.

To run:
    python examples/llm_workflow.py
"""

from typing import Any, cast
from unittest.mock import MagicMock, patch

from kairos import (
    FailureAction,
    FailurePolicy,
    Schema,
    Step,
    Workflow,
    WorkflowStatus,
)
from kairos import validators as v

# ---------------------------------------------------------------------------
# Schemas — what each LLM step must produce
# ---------------------------------------------------------------------------

research_schema = Schema(
    {"text": str, "model": str, "latency_ms": float},
    validators={
        "text": [v.not_empty()],
        "model": [v.not_empty()],
    },
)

report_schema = Schema(
    {"text": str, "model": str, "latency_ms": float},
    validators={
        "text": [v.not_empty()],
    },
)


# ---------------------------------------------------------------------------
# Mock helpers — simulate LLM responses without real API calls
# ---------------------------------------------------------------------------


def _make_mock_anthropic(response_text: str = "Mock Claude response") -> MagicMock:
    """Create a mock anthropic module with realistic response structure."""
    mock = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=response_text)]
    response.model = "claude-sonnet-4-20250514"
    response.usage.input_tokens = 150
    response.usage.output_tokens = 200
    response.stop_reason = "end_turn"
    mock.Anthropic.return_value.messages.create.return_value = response
    return mock


def _make_mock_openai(response_text: str = "Mock OpenAI response") -> MagicMock:
    """Create a mock openai module with realistic response structure."""
    mock = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=response_text), finish_reason="stop")]
    response.model = "gpt-4o"
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = 300
    mock.OpenAI.return_value.chat.completions.create.return_value = response
    return mock


# ---------------------------------------------------------------------------
# The workflow — Claude researches, OpenAI writes the report
# ---------------------------------------------------------------------------


def build_workflow() -> Workflow:
    """Build the workflow with adapter-powered steps.

    We import the factory functions inside this function so we can
    patch the SDK modules before the adapters try to use them.
    """
    from kairos.adapters.claude import claude
    from kairos.adapters.openai_adapter import openai_adapter

    return Workflow(
        name="llm-research-pipeline",
        steps=[
            # Step 1: Claude researches each topic (foreach fan-out)
            Step(
                name="research",
                action=claude(
                    "Research the following topic thoroughly: {item}",
                    model="claude-sonnet-4-20250514",
                ),
                foreach="topics",
                output_contract=research_schema,
                failure_policy=FailurePolicy(
                    on_execution_fail=FailureAction.RETRY,
                    on_validation_fail=FailureAction.RETRY,
                    max_retries=2,
                ),
            ),
            # Step 2: OpenAI writes a report from the research
            Step(
                name="report",
                action=openai_adapter(
                    "Based on the following research results, write a concise report:\n\n"
                    "{research}",
                    model="gpt-4o",
                ),
                depends_on=["research"],
                output_contract=report_schema,
                failure_policy=FailurePolicy(
                    on_validation_fail=FailureAction.ABORT,
                ),
            ),
        ],
        sensitive_keys=["*api_key*", "*token*"],
        metadata={"description": "Multi-provider LLM workflow with validation"},
    )


# ---------------------------------------------------------------------------
# Run it with mocked LLM calls
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Kairos LLM Workflow — Adapter Demo (mocked)")
    print("=" * 60)
    print()
    print("This demo uses mocked LLM responses to show the adapter pattern.")
    print("For real LLM calls, set your API keys and use the real examples.")
    print()

    # Mock both SDKs so no real API calls are made
    mock_ant = _make_mock_anthropic(
        "The AI agent security landscape is evolving rapidly. Key concerns include "
        "prompt injection, credential exposure in retry context, and unvalidated "
        "data flowing between pipeline steps. Kairos addresses all three."
    )
    mock_oai = _make_mock_openai(
        "REPORT: AI Agent Security Analysis\n\n"
        "This report synthesizes research on three critical topics in AI agent "
        "security. The findings indicate that current orchestration frameworks "
        "lack adequate security controls at the step boundary level."
    )

    with (
        patch("kairos.adapters.claude.anthropic", mock_ant),
        patch("kairos.adapters.openai_adapter.openai_sdk", mock_oai),
        patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-ant-mock-key-for-demo",
                "OPENAI_API_KEY": "sk-mock-key-for-demo",
            },
        ),
    ):
        workflow = build_workflow()
        result = workflow.run(
            {
                "topics": [
                    "prompt injection in AI agents",
                    "retry context sanitization",
                    "scoped state access controls",
                ],
            }
        )

    # --- Show results ---

    print(f"Status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.1f}ms")
    print()

    print("Step Results:")
    print("-" * 40)
    for name, sr in result.step_results.items():
        status_icon = {"completed": "+", "skipped": "~", "failed_final": "X"}.get(
            sr.status.value, "?"
        )
        print(f"  [{status_icon}] {name} ({sr.status.value}, {len(sr.attempts)} attempt(s))")

        # Show the LLM response text for each step
        raw_output: object = sr.output
        if raw_output is not None:
            if isinstance(raw_output, list):
                # foreach step — show each item
                items = cast(list[dict[str, Any]], raw_output)
                for i, item in enumerate(items):
                    text = str(item.get("text", ""))[:80]
                    model = item.get("model", "unknown")
                    print(f"    [{i}] ({model}) {text}...")
            elif isinstance(raw_output, dict):
                out = cast(dict[str, Any], raw_output)
                text = str(out.get("text", ""))[:80]
                model = out.get("model", "unknown")
                print(f"    ({model}) {text}...")

    print()

    # Show token usage from the report step
    report_output = cast(dict[str, Any], result.step_results["report"].output)
    usage = report_output.get("usage", {})
    print("Report token usage:")
    print(f"  Input tokens:  {usage.get('input_tokens', 'N/A')}")
    print(f"  Output tokens: {usage.get('output_tokens', 'N/A')}")
    print(f"  Total tokens:  {usage.get('total_tokens', 'N/A')}")
    print()

    # Show that validation ran
    print("Validation:")
    print("  research output contract: passed (text is non-empty string)")
    print("  report output contract:   passed (text is non-empty string)")
    print()

    # Show what would happen with a real workflow
    print("=" * 60)
    print("  HOW THIS WORKS WITH REAL LLMs")
    print("=" * 60)
    print()
    print("  1. Set environment variables:")
    print("     export ANTHROPIC_API_KEY='sk-ant-your-real-key'")
    print("     export OPENAI_API_KEY='sk-your-real-key'")
    print()
    print("  2. Install the provider SDKs:")
    print("     pip install kairos-ai[anthropic] kairos-ai[openai]")
    print()
    print("  3. Remove the mock patches — the same workflow code")
    print("     calls real APIs with real responses.")
    print()
    print("  4. Kairos validates every LLM response against the")
    print("     output contract. Bad responses get caught and retried.")
    print()

    assert result.status == WorkflowStatus.COMPLETE
    print("Done.")
