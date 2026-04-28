"""Demo: What happens when bad data hits a Kairos contract.

Paced and color-styled version of broken_data.py for GIF recording.

Functionally identical to broken_data.py — same workflow, same schemas,
same test cases — with brief time.sleep() pauses between sections and
ANSI color codes for visual clarity in recorded GIFs.

For the original (instant, plain) version, see examples/broken_data.py.
"""

import time
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

# --- Pacing helper for GIF recording ---


def pause(seconds: float = 0.6) -> None:
    """Brief pause between output sections so the GIF shows progression."""
    time.sleep(seconds)


# --- ANSI color helpers ---
# Retro green terminal aesthetic with red/yellow accents for failures and warnings.


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    # Retro green palette
    GREEN = "\033[32m"
    BRIGHT_GREEN = "\033[92m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_BRIGHT_GREEN = "\033[1;92m"
    # Accents
    RED = "\033[31m"
    BOLD_RED = "\033[1;31m"
    YELLOW = "\033[33m"
    BOLD_YELLOW = "\033[1;33m"
    DIM_GREEN = "\033[2;32m"


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


def header(title: str, color: str = C.BOLD_BRIGHT_GREEN) -> None:
    """Print a styled section header."""
    bar = "=" * 60
    print(f"{color}{bar}{C.RESET}")
    print(f"  {color}{title}{C.RESET}")
    print(f"{color}{bar}{C.RESET}")


if __name__ == "__main__":
    # ---------------------------------------------------------------
    # Test 1: Good data — everything passes
    # ---------------------------------------------------------------
    header("TEST 1: Good data")
    pause(0.4)

    good_data: dict[str, object] = {
        "raw_records": [
            {"name": "Alice", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob", "email": "bob@example.com", "score": 0.82},
        ],
    }

    result = workflow.run(good_data)
    print(f"  Status: {C.BOLD_GREEN}{result.status.value}{C.RESET}")
    agg = cast(dict[str, Any], result.step_results["aggregate"].output)
    print(f"  Average score: {C.BRIGHT_GREEN}{agg['average_score']}{C.RESET}")
    print(
        f"  Result: {C.BOLD_GREEN}CORRECT{C.RESET} {C.GREEN}— data flowed through cleanly{C.RESET}"
    )
    print()
    pause(1.2)

    # ---------------------------------------------------------------
    # Test 2: Bad email — not a valid email address
    # ---------------------------------------------------------------
    header("TEST 2: Bad email (LLM hallucinated garbage)")
    pause(0.4)

    bad_email_data: dict[str, object] = {
        "raw_records": [
            {"name": "Alice", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob", "email": "not-an-email", "score": 0.82},  # <-- BAD
        ],
    }

    result = workflow.run(bad_email_data)
    print(f"  Status: {C.BOLD_RED}{result.status.value}{C.RESET}")
    clean_result = result.step_results.get("clean")
    if clean_result:
        for attempt in clean_result.attempts:
            if attempt.error_type:
                print(f"  Caught: {C.YELLOW}{attempt.error_type}{C.RESET}")
                print(f"  Message: {C.DIM_GREEN}{attempt.error_message}{C.RESET}")
        if not any(a.error_type for a in clean_result.attempts):
            print(f"  Step failed: {C.RED}{clean_result.status.value}{C.RESET}")
    agg_result = result.step_results.get("aggregate")
    agg_status = agg_result.status.value if agg_result else "never ran"
    print(f"  Aggregate step: {C.YELLOW}{agg_status}{C.RESET}")
    print(
        f"  Result: {C.BOLD_GREEN}Kairos BLOCKED{C.RESET} "
        f"{C.GREEN}bad data from reaching aggregate{C.RESET}"
    )
    print()
    pause(1.2)

    # ---------------------------------------------------------------
    # Test 3: Score out of range — LLM returned 95 instead of 0.95
    # ---------------------------------------------------------------
    header("TEST 3: Score out of range (95 instead of 0.95)")
    pause(0.4)

    bad_score_data: dict[str, object] = {
        "raw_records": [
            {"name": "Alice", "email": "alice@example.com", "score": 0.95},
            {"name": "Bob", "email": "bob@example.com", "score": 95},  # <-- BAD
        ],
    }

    result = workflow.run(bad_score_data)
    print(f"  Status: {C.BOLD_RED}{result.status.value}{C.RESET}")
    clean_result = result.step_results.get("clean")
    if clean_result:
        for attempt in clean_result.attempts:
            if attempt.error_type:
                print(f"  Caught: {C.YELLOW}{attempt.error_type}{C.RESET}")
                print(f"  Message: {C.DIM_GREEN}{attempt.error_message}{C.RESET}")
        if not any(a.error_type for a in clean_result.attempts):
            print(f"  Step failed: {C.RED}{clean_result.status.value}{C.RESET}")
    agg_result = result.step_results.get("aggregate")
    agg_status = agg_result.status.value if agg_result else "never ran"
    print(f"  Aggregate step: {C.YELLOW}{agg_status}{C.RESET}")
    print(
        f"  Result: {C.BOLD_GREEN}Kairos BLOCKED{C.RESET} "
        f"{C.GREEN}— average won't be corrupted by{C.RESET} "
        f"{C.BOLD_RED}95{C.RESET}"
    )
    print()
    pause(1.2)

    # ---------------------------------------------------------------
    # Test 4: Empty name — LLM returned blank
    # ---------------------------------------------------------------
    header("TEST 4: Empty name (LLM returned nothing)")
    pause(0.4)

    empty_name_data: dict[str, object] = {
        "raw_records": [
            {"name": "", "email": "ghost@example.com", "score": 0.5},  # <-- BAD
        ],
    }

    result = workflow.run(empty_name_data)
    print(f"  Status: {C.BOLD_RED}{result.status.value}{C.RESET}")
    clean_result = result.step_results.get("clean")
    if clean_result:
        for attempt in clean_result.attempts:
            if attempt.error_type:
                print(f"  Caught: {C.YELLOW}{attempt.error_type}{C.RESET}")
                print(f"  Message: {C.DIM_GREEN}{attempt.error_message}{C.RESET}")
        if not any(a.error_type for a in clean_result.attempts):
            print(f"  Step failed: {C.RED}{clean_result.status.value}{C.RESET}")
    agg_result = result.step_results.get("aggregate")
    agg_status = agg_result.status.value if agg_result else "never ran"
    print(f"  Aggregate step: {C.YELLOW}{agg_status}{C.RESET}")
    print(
        f"  Result: {C.BOLD_GREEN}Kairos BLOCKED{C.RESET} "
        f"{C.GREEN}— empty names don't slip through{C.RESET}"
    )
    print()
    pause(1.5)

    # ---------------------------------------------------------------
    # Without Kairos, what would have happened?
    # ---------------------------------------------------------------
    header("WITHOUT KAIROS — what would happen with test 3's data?", color=C.BOLD_YELLOW)
    print()
    pause(0.6)
    print(
        f"  {C.GREEN}The score of {C.BOLD_RED}95{C.RESET}"
        f"{C.GREEN} would flow to aggregate unchecked.{C.RESET}"
    )
    pause(0.5)
    print(f"  {C.GREEN}Average would be: (0.95 + 95) / 2 = {C.BOLD_RED}47.975{C.RESET}")
    pause(0.5)
    print(
        f"  {C.GREEN}A customer report goes out saying average confidence is "
        f"{C.BOLD_RED}4797%{C.GREEN}.{C.RESET}"
    )
    pause(0.5)
    print(f"  {C.DIM_GREEN}Nobody notices until a client calls.{C.RESET}")
    print()
    pause(1.0)
    print(
        f"  {C.GREEN}With Kairos, the workflow {C.BOLD_BRIGHT_GREEN}STOPPED{C.RESET}"
        f"{C.GREEN} at the clean step.{C.RESET}"
    )
    pause(0.4)
    print(
        f"  {C.GREEN}Bad data never reached aggregate. The error is clear and immediate.{C.RESET}"
    )
