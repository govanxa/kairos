"""Data pipeline example — validation contracts and failure recovery.

Demonstrates:
- Input and output contracts via Schema
- Field-level validators (range, not_empty, pattern)
- Failure policies with retry
- foreach fan-out over a collection
- Sensitive key redaction in the final result

This is a realistic data pipeline: ingest records, validate and clean
them individually via foreach, then aggregate the results.
"""

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

# ---------------------------------------------------------------------------
# Schemas — define what correct data looks like
# ---------------------------------------------------------------------------

record_schema = Schema(
    {"name": str, "email": str, "score": float},
    validators={
        "name": [v.not_empty()],
        "email": [v.pattern(r"^[\w.+-]+@[\w-]+\.[\w.]+$")],
        "score": [v.range(min=0.0, max=1.0)],
    },
)

aggregation_schema = Schema(
    {
        "total_records": int,
        "average_score": float,
        "valid_emails": list[str],
    }
)

# ---------------------------------------------------------------------------
# Step actions
# ---------------------------------------------------------------------------


def ingest(ctx: StepContext) -> dict[str, object]:
    """Simulate ingesting records from an external source."""
    # In a real workflow, this might call an API or read a file.
    # Here we read from initial state.
    records = ctx.state.get("raw_records")
    return {"records": records, "source": "initial_state"}


def clean_record(ctx: StepContext) -> dict[str, object]:
    """Clean and validate a single record (runs once per item in foreach)."""
    record = ctx.item
    # Normalize: strip whitespace, lowercase email
    cleaned = {
        "name": record["name"].strip(),
        "email": record["email"].strip().lower(),
        "score": float(record["score"]),
    }
    return cleaned


def aggregate(ctx: StepContext) -> dict[str, object]:
    """Aggregate all cleaned records into a summary."""
    cleaned_records = ctx.inputs["clean"]
    total = len(cleaned_records)
    scores = [r["score"] for r in cleaned_records if r is not None]
    avg_score = sum(scores) / len(scores) if scores else 0.0
    valid_emails = [r["email"] for r in cleaned_records if r is not None and "@" in r["email"]]
    return {
        "total_records": total,
        "average_score": round(avg_score, 3),
        "valid_emails": valid_emails,
    }


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

workflow = Workflow(
    name="data-pipeline",
    steps=[
        Step(
            name="ingest",
            action=ingest,
        ),
        Step(
            name="clean",
            action=clean_record,
            depends_on=["ingest"],
            foreach="raw_records",
            output_contract=record_schema,
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.RETRY,
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
            ),
        ),
        Step(
            name="aggregate",
            action=aggregate,
            depends_on=["clean"],
            output_contract=aggregation_schema,
        ),
    ],
    failure_policy=FailurePolicy(
        on_execution_fail=FailureAction.RETRY,
        max_retries=1,
    ),
    sensitive_keys=["*email*"],
    metadata={"description": "Ingest, clean, and aggregate data records"},
)


# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running data-pipeline workflow...\n")

    initial_data = {
        "raw_records": [
            {"name": "Alice Johnson", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob Smith", "email": "bob@example.com", "score": 0.82},
            {"name": "Carol Davis", "email": "carol@example.com", "score": 0.91},
        ],
    }

    result = workflow.run(initial_data)

    print(f"Status: {result.status.value}")
    print(f"Duration: {result.duration_ms:.1f}ms")
    print(f"Steps executed: {len(result.step_results)}")
    print()

    # Show step results
    for step_name, step_result in result.step_results.items():
        print(f"  [{step_result.status.value}] {step_name}")
        if step_result.output is not None:
            if isinstance(step_result.output, list):
                print(f"    -> {len(step_result.output)} items")
            else:
                print(f"    -> {step_result.output}")

    print()

    # Show the aggregation
    agg = result.step_results["aggregate"].output
    print("Aggregation result:")
    print(f"  Total records: {agg['total_records']}")
    print(f"  Average score: {agg['average_score']}")
    print(f"  Valid emails: {agg['valid_emails']}")

    print()

    # Show sensitive key redaction in final state
    print("Final state (sensitive keys redacted):")
    for key, value in result.final_state.items():
        display = str(value)[:80] + "..." if len(str(value)) > 80 else str(value)
        print(f"  {key}: {display}")

    assert result.status == WorkflowStatus.COMPLETE
    print("\nDone.")
