# Getting Started with Kairos

A step-by-step guide to building your first contract-enforced AI workflow. By the end, you'll have a working pipeline with validation, failure recovery, and security boundaries.

**Time:** ~10 minutes
**Prerequisites:** Python 3.11+

---

## 1. Install Kairos

```bash
pip install kairos-ai
```

Verify it works:

```bash
python -c "from kairos import Workflow; print('Ready!')"
```

---

## 2. Your First Workflow

A workflow is a series of steps that run in order. Each step is a plain Python function that receives a `StepContext` and returns data.

Create a file called `my_workflow.py`:

```python
from kairos import Workflow, Step, StepContext

def fetch_data(ctx: StepContext) -> dict:
    """Step 1: Get the input data."""
    name = ctx.state.get("name", "World")
    return {"greeting": f"Hello, {name}!", "length": len(name)}

def process(ctx: StepContext) -> dict:
    """Step 2: Process the data from step 1."""
    fetched = ctx.inputs["fetch_data"]  # output from the previous step
    return {
        "message": fetched["greeting"].upper(),
        "original_length": fetched["length"],
    }

workflow = Workflow(
    name="my-first-workflow",
    steps=[
        Step(name="fetch_data", action=fetch_data),
        Step(name="process", action=process, depends_on=["fetch_data"]),
    ],
)

result = workflow.run({"name": "Kairos"})
print(f"Status: {result.status.value}")
print(f"Output: {result.step_results['process'].output}")
```

Run it:

```bash
python my_workflow.py
```

```
Status: complete
Output: {'message': 'HELLO, KAIROS!', 'original_length': 6}
```

**What just happened:**
- `fetch_data` ran first, read `"name"` from the initial state, and returned a dict.
- `process` ran second (because of `depends_on=["fetch_data"]`), received fetch_data's output via `ctx.inputs["fetch_data"]`, and transformed it.
- Kairos managed the execution order, state passing, and result collection.

---

## 3. Add Validation Contracts

Right now, if `fetch_data` returns garbage, `process` will crash with a confusing error. Let's add a contract that catches bad data at the boundary.

```python
from kairos import Workflow, Step, StepContext, Schema
from kairos import validators as v

# Define what fetch_data MUST return
fetch_schema = Schema(
    {"greeting": str, "length": int},
    validators={
        "greeting": [v.not_empty()],
        "length": [v.range(min=1, max=1000)],
    },
)

def fetch_data(ctx: StepContext) -> dict:
    name = ctx.state.get("name", "World")
    return {"greeting": f"Hello, {name}!", "length": len(name)}

def process(ctx: StepContext) -> dict:
    fetched = ctx.inputs["fetch_data"]
    return {
        "message": fetched["greeting"].upper(),
        "original_length": fetched["length"],
    }

workflow = Workflow(
    name="validated-workflow",
    steps=[
        Step(
            name="fetch_data",
            action=fetch_data,
            output_contract=fetch_schema,  # must match this shape
        ),
        Step(name="process", action=process, depends_on=["fetch_data"]),
    ],
)

result = workflow.run({"name": "Kairos"})
print(f"Status: {result.status.value}")
```

Now if `fetch_data` returns `{"greeting": "", "length": -1}`, Kairos will catch it immediately — the empty greeting fails `not_empty()`, the negative length fails `range(min=1)`. The `process` step never runs with bad data.

**Available validators:**
- `v.not_empty()` — rejects empty strings, lists, dicts
- `v.range(min=, max=)` — numeric range check
- `v.length(min=, max=)` — string or list length
- `v.pattern(regex)` — regex match (with ReDoS protection)
- `v.one_of(values)` — must be one of the allowed values
- `v.custom(fn)` — any function that returns `True`/`False`

---

## 4. Add Failure Policies

What should happen when a step fails? Kairos lets you decide per step.

```python
from kairos import FailurePolicy, FailureAction

workflow = Workflow(
    name="resilient-workflow",
    steps=[
        Step(
            name="fetch_data",
            action=fetch_data,
            output_contract=fetch_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,   # retry if the function crashes
                on_validation_fail=FailureAction.ABORT,   # stop if output is wrong shape
                max_retries=3,
            ),
        ),
        Step(name="process", action=process, depends_on=["fetch_data"]),
    ],
)
```

**Failure actions:**
- `RETRY` — try again (with exponential backoff + jitter)
- `ABORT` — stop the entire workflow
- `SKIP` — mark the step as skipped and continue

**Three-level hierarchy:** Step policy > Workflow policy > Kairos defaults. Most specific wins.

```python
# Set a default for the whole workflow, override on specific steps
workflow = Workflow(
    name="with-defaults",
    steps=[...],
    failure_policy=FailurePolicy(
        on_execution_fail=FailureAction.RETRY,
        max_retries=2,
    ),
)
```

---

## 5. Fan Out with foreach

Process a list of items individually — Kairos runs your step once per item:

```python
def analyze_competitor(ctx: StepContext) -> dict:
    """Runs once per competitor."""
    competitor = ctx.item  # the current item from the list
    return {"name": competitor, "score": len(str(competitor)) * 0.1}

workflow = Workflow(
    name="fan-out-example",
    steps=[
        Step(
            name="analyze",
            action=analyze_competitor,
            foreach="competitors",  # state key containing the list
        ),
    ],
)

result = workflow.run({"competitors": ["Globex", "Initech", "Umbrella"]})
# analyze runs 3 times, results collected into a list
print(result.step_results["analyze"].output)
# [{"name": "Globex", "score": 0.6}, {"name": "Initech", "score": 0.7}, ...]
```

Each item gets its own execution with its own retry policy. If one fails, the others still run (configurable via `ForeachPolicy`).

---

## 6. Secure State Access

By default, every step can read and write any state key. For security-sensitive workflows, lock it down:

```python
workflow = Workflow(
    name="secure-workflow",
    steps=[
        Step(
            name="call_api",
            action=call_external_api,
            read_keys=["api_key", "query"],    # can ONLY read these keys
            write_keys=["call_api"],            # can ONLY write its own output
        ),
        Step(
            name="process_results",
            action=process_fn,
            depends_on=["call_api"],
            read_keys=["call_api"],             # can read the API results
            # CANNOT read api_key — if this step is compromised,
            # the key stays safe
        ),
    ],
    sensitive_keys=["*api_key*", "*password*"],  # redacted in logs and final state
)
```

If `process_results` tries `ctx.state.get("api_key")`, Kairos raises `StateError`. The step literally cannot see keys outside its declared scope.

---

## 7. Inspect Results

Every workflow run returns a `WorkflowResult` with full details:

```python
result = workflow.run({"name": "Kairos"})

# Overall status
print(result.status)        # WorkflowStatus.COMPLETE or FAILED
print(result.duration_ms)   # total time in milliseconds
print(result.llm_calls)     # number of LLM invocations (for budgeting)

# Per-step results
for name, step_result in result.step_results.items():
    print(f"{name}: {step_result.status.value}")
    print(f"  Output: {step_result.output}")
    print(f"  Attempts: {len(step_result.attempts)}")

    # Each attempt records what happened
    for attempt in step_result.attempts:
        print(f"    #{attempt.attempt_number}: {attempt.status.value}")
        if attempt.error_type:
            print(f"    Error: {attempt.error_type}: {attempt.error_message}")

# Final state (sensitive keys are redacted)
print(result.final_state)
```

---

## 8. Putting It All Together

Here's a complete workflow that uses everything above:

```python
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


# --- Schemas ---

research_schema = Schema(
    {"name": str, "products": list, "strength": str},
    validators={
        "name": [v.not_empty()],
        "strength": [v.not_empty()],
    },
)

report_schema = Schema(
    {"title": str, "findings": list, "recommendation": str},
    validators={
        "title": [v.not_empty()],
        "recommendation": [v.length(min=10)],
    },
)


# --- Steps ---

def fetch(ctx: StepContext) -> dict:
    return {"companies": ctx.state.get("targets")}

def research(ctx: StepContext) -> dict:
    company = ctx.item
    return {
        "name": company,
        "products": [f"{company} Pro", f"{company} Lite"],
        "strength": f"{company} has strong market presence",
    }

def report(ctx: StepContext) -> dict:
    research_results = ctx.inputs["research"]
    names = [r["name"] for r in research_results]
    return {
        "title": "Competitive Analysis",
        "findings": [f"Analyzed {len(names)} companies: {', '.join(names)}"],
        "recommendation": "Focus on differentiation through security-first design.",
    }


# --- Workflow ---

workflow = Workflow(
    name="full-example",
    steps=[
        Step(name="fetch", action=fetch, read_keys=["targets"]),
        Step(
            name="research",
            action=research,
            depends_on=["fetch"],
            foreach="targets",
            output_contract=research_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=3,
            ),
        ),
        Step(
            name="report",
            action=report,
            depends_on=["research"],
            output_contract=report_schema,
            failure_policy=FailurePolicy(
                on_validation_fail=FailureAction.ABORT,
            ),
        ),
    ],
    sensitive_keys=["*api_key*"],
)

result = workflow.run({"targets": ["Acme", "Globex", "Initech"]})

print(f"Status: {result.status.value}")
for name, sr in result.step_results.items():
    print(f"  {name}: {sr.status.value} ({len(sr.attempts)} attempts)")

assert result.status == WorkflowStatus.COMPLETE
print("\nDone!")
```

---

## Next Steps

- **Run the examples** in the `examples/` directory to see more patterns
- **Read the [README](README.md)** for the full feature overview and comparison table
- **Check the source** — Kairos has zero external dependencies; the entire SDK is readable Python

---

## Quick Reference

| Concept | Import | Purpose |
|---|---|---|
| `Workflow` | `from kairos import Workflow` | Top-level orchestrator |
| `Step` | `from kairos import Step` | A single unit of work |
| `StepContext` | `from kairos import StepContext` | Passed to your step function |
| `Schema` | `from kairos import Schema` | Define data contracts |
| `validators` | `from kairos import validators as v` | Field-level validation rules |
| `FailurePolicy` | `from kairos import FailurePolicy` | Configure retry/abort/skip |
| `FailureAction` | `from kairos import FailureAction` | RETRY, ABORT, SKIP |
| `SKIP` | `from kairos import SKIP` | Return from a step to skip it |
| `WorkflowStatus` | `from kairos import WorkflowStatus` | COMPLETE, FAILED, RUNNING |
