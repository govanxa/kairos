"""Simple chain example — the most basic Kairos workflow.

Demonstrates:
- Defining steps with dependencies
- Passing data between steps via state
- Running a workflow and inspecting the result

This is the "hello world" of Kairos. Three steps in a linear chain:
  prepare → process → summarize
"""

from typing import Any, cast

from kairos import Step, StepContext, Workflow, WorkflowStatus

# ---------------------------------------------------------------------------
# Step actions — plain functions that receive StepContext
# ---------------------------------------------------------------------------


def prepare(ctx: StepContext) -> dict[str, Any]:
    """Read initial input and prepare data for processing."""
    name = ctx.state.get("user_name", "World")
    items = cast(list[int], ctx.state.get("items", [1, 2, 3, 4, 5]))
    return {"name": name, "items": items, "count": len(items)}


def process(ctx: StepContext) -> dict[str, Any]:
    """Process the prepared data — double each item."""
    prepared = cast(dict[str, Any], ctx.inputs["prepare"])
    items = cast(list[int], prepared["items"])
    doubled = [x * 2 for x in items]
    return {"doubled": doubled, "total": sum(doubled)}


def summarize(ctx: StepContext) -> dict[str, Any]:
    """Produce a final summary from processed data."""
    prepared = cast(dict[str, Any], ctx.inputs["prepare"])
    processed = cast(dict[str, Any], ctx.inputs["process"])
    return {
        "greeting": f"Hello, {prepared['name']}!",
        "original_count": prepared["count"],
        "processed_total": processed["total"],
        "summary": (
            f"Processed {prepared['count']} items. Sum of doubled values: {processed['total']}."
        ),
    }


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="simple-chain",
    steps=[
        Step(name="prepare", action=prepare),
        Step(name="process", action=process, depends_on=["prepare"]),
        Step(name="summarize", action=summarize, depends_on=["prepare", "process"]),
    ],
    metadata={"description": "A simple three-step chain demonstrating basic Kairos usage"},
)


# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running simple-chain workflow...\n")

    result = workflow.run({"user_name": "Kairos", "items": [10, 20, 30, 40, 50]})

    print(f"Status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.1f}ms")
    print(f"Steps executed: {len(result.step_results)}")
    print()

    # Show each step's result
    for step_name, step_result in result.step_results.items():
        print(f"  [{step_result.status.value}] {step_name}: {step_result.output}")

    print()

    # The final summary
    summary_output = cast(dict[str, Any], result.step_results["summarize"].output)
    print(f"Final summary: {summary_output['summary']}")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
