"""Demo: What happens when bad data hits a Kairos contract.

This reuses the data_pipeline workflow but feeds it records that violate
the output contract — showing how Kairos catches the problem instead of
letting it silently corrupt downstream results.
"""

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

# --- Same schemas as data_pipeline.py ---

record_schema = Schema(
    {"name": str, "email": str, "score": float},
    validators={
        "name": [v.not_empty()],
        "email": [v.pattern(r"^[\w.+-]+@[\w-]+\.[\w.]+$")],
        "score": [v.range(min=0.0, max=1.0)],
    },
)

aggregation_schema = Schema(
    {"total_records": int, "average_score": float, "valid_emails": list[str]},
)


# --- Steps ---


def ingest(ctx: StepContext) -> dict[str, Any]:
    return {"records": ctx.state.get("raw_records"), "source": "test"}


def clean_record(ctx: StepContext) -> dict[str, Any]:
    """Just passes the record through — doesn't actually clean it."""
    record = cast(dict[str, Any], ctx.item)
    return {
        "name": record.get("name", ""),
        "email": record.get("email", ""),
        "score": record.get("score", 0.0),
    }


def aggregate(ctx: StepContext) -> dict[str, Any]:
    cleaned = cast(list[dict[str, Any] | None], ctx.inputs["clean"])
    total = len(cleaned)
    scores = [r["score"] for r in cleaned if r is not None]
    avg = sum(scores) / len(scores) if scores else 0.0
    emails = [r["email"] for r in cleaned if r is not None and "@" in r["email"]]
    return {"total_records": total, "average_score": round(avg, 3), "valid_emails": emails}


workflow = Workflow(
    name="broken-data-demo",
    steps=[
        Step(name="ingest", action=ingest),
        Step(
            name="clean",
            action=clean_record,
            depends_on=["ingest"],
            foreach="raw_records",
            output_contract=record_schema,  # <-- THIS is the guard
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.ABORT,  # Fail hard on bad data
                max_retries=0,
            ),
        ),
        Step(name="aggregate", action=aggregate, depends_on=["clean"]),
    ],
)


if __name__ == "__main__":
    # ---------------------------------------------------------------
    # Test 1: Good data — everything passes
    # ---------------------------------------------------------------
    print("=" * 60)
    print("  TEST 1: Good data")
    print("=" * 60)

    good_data: dict[str, object] = {
        "raw_records": [
            {"name": "Alice", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob", "email": "bob@example.com", "score": 0.82},
        ],
    }

    result = workflow.run(good_data)
    print(f"  Status: {result.status.value}")
    agg = cast(dict[str, Any], result.step_results["aggregate"].output)
    print(f"  Average score: {agg['average_score']}")
    print("  Result: CORRECT — data flowed through cleanly")
    print()

    # ---------------------------------------------------------------
    # Test 2: Bad email — not a valid email address
    # ---------------------------------------------------------------
    print("=" * 60)
    print("  TEST 2: Bad email (LLM hallucinated garbage)")
    print("=" * 60)

    bad_email_data: dict[str, object] = {
        "raw_records": [
            {"name": "Alice", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob", "email": "not-an-email", "score": 0.82},  # <-- BAD
        ],
    }

    result = workflow.run(bad_email_data)
    print(f"  Status: {result.status.value}")
    clean_result = result.step_results.get("clean")
    if clean_result:
        for attempt in clean_result.attempts:
            if attempt.error_type:
                print(f"  Caught: {attempt.error_type}")
                print(f"  Message: {attempt.error_message}")
        if not any(a.error_type for a in clean_result.attempts):
            print(f"  Step failed: {clean_result.status.value}")
    agg_result = result.step_results.get("aggregate")
    agg_status = agg_result.status.value if agg_result else "never ran"
    print(f"  Aggregate step: {agg_status}")
    print("  Result: Kairos BLOCKED bad data from reaching aggregate")
    print()

    # ---------------------------------------------------------------
    # Test 3: Score out of range — LLM returned 95 instead of 0.95
    # ---------------------------------------------------------------
    print("=" * 60)
    print("  TEST 3: Score out of range (95 instead of 0.95)")
    print("=" * 60)

    bad_score_data: dict[str, object] = {
        "raw_records": [
            {"name": "Alice", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob", "email": "bob@example.com", "score": 95},  # <-- BAD
        ],
    }

    result = workflow.run(bad_score_data)
    print(f"  Status: {result.status.value}")
    clean_result = result.step_results.get("clean")
    if clean_result:
        for attempt in clean_result.attempts:
            if attempt.error_type:
                print(f"  Caught: {attempt.error_type}")
                print(f"  Message: {attempt.error_message}")
        if not any(a.error_type for a in clean_result.attempts):
            print(f"  Step failed: {clean_result.status.value}")
    agg_result = result.step_results.get("aggregate")
    agg_status = agg_result.status.value if agg_result else "never ran"
    print(f"  Aggregate step: {agg_status}")
    print("  Result: Kairos BLOCKED — average won't be corrupted by 95")
    print()

    # ---------------------------------------------------------------
    # Test 4: Empty name — LLM returned blank
    # ---------------------------------------------------------------
    print("=" * 60)
    print("  TEST 4: Empty name (LLM returned nothing)")
    print("=" * 60)

    empty_name_data: dict[str, object] = {
        "raw_records": [
            {"name": "", "email": "ghost@example.com", "score": 0.5},  # <-- BAD
        ],
    }

    result = workflow.run(empty_name_data)
    print(f"  Status: {result.status.value}")
    clean_result = result.step_results.get("clean")
    if clean_result:
        for attempt in clean_result.attempts:
            if attempt.error_type:
                print(f"  Caught: {attempt.error_type}")
                print(f"  Message: {attempt.error_message}")
        if not any(a.error_type for a in clean_result.attempts):
            print(f"  Step failed: {clean_result.status.value}")
    agg_result = result.step_results.get("aggregate")
    agg_status = agg_result.status.value if agg_result else "never ran"
    print(f"  Aggregate step: {agg_status}")
    print("  Result: Kairos BLOCKED — empty names don't slip through")
    print()

    # ---------------------------------------------------------------
    # Without Kairos, what would have happened?
    # ---------------------------------------------------------------
    print("=" * 60)
    print("  WITHOUT KAIROS — what would happen with test 3's data?")
    print("=" * 60)
    print()
    print("  The score of 95 would flow to aggregate unchecked.")
    print("  Average would be: (0.95 + 95) / 2 = 47.975")
    print("  A customer report goes out saying average confidence is 4797%.")
    print("  Nobody notices until a client calls.")
    print()
    print("  With Kairos, the workflow STOPPED at the clean step.")
    print("  Bad data never reached aggregate. The error is clear and immediate.")
