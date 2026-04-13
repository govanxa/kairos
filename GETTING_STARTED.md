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

## 9. Using LLM Adapters

So far, every step has been a plain Python function. In real AI workflows, you call an LLM. Kairos provides **adapters** — thin wrappers that handle SDK setup, credential management, response parsing, and error wrapping for you.

### Install a provider

Adapters are optional dependencies. Install only what you need:

```bash
pip install kairos-ai[anthropic]    # Claude adapter
pip install kairos-ai[openai]       # OpenAI adapter
```

### Set your API key

Adapters read credentials from environment variables — never from code. This is a security requirement (ADR-016).

```bash
# Linux/Mac
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
export OPENAI_API_KEY="sk-your-key-here"

# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
$env:OPENAI_API_KEY = "sk-your-key-here"
```

If you try to pass `api_key` as a parameter, Kairos raises `SecurityError` immediately. This prevents accidental credential exposure in code, logs, or version control.

### Use the factory functions

The `claude()` and `openai_adapter()` functions return step-compatible callables. Use `{placeholder}` syntax to reference data from upstream steps:

```python
from kairos import Workflow, Step, Schema, FailurePolicy, FailureAction
from kairos import validators as v
from kairos.adapters.claude import claude
from kairos.adapters.openai_adapter import openai_adapter

# Define what the analysis must look like
analysis_schema = Schema(
    {"summary": str, "key_points": list},
    validators={"summary": [v.not_empty()]},
)

workflow = Workflow(
    name="llm-pipeline",
    steps=[
        # Claude researches the topic
        Step(
            name="research",
            action=claude("Provide a detailed analysis of: {topic}"),
            output_contract=analysis_schema,
            failure_policy=FailurePolicy(
                on_execution_fail=FailureAction.RETRY,
                max_retries=2,
            ),
        ),
        # OpenAI writes the final report
        Step(
            name="report",
            action=openai_adapter(
                "Based on this research: {research}\n\nWrite a concise report."
            ),
            depends_on=["research"],
        ),
    ],
)

result = workflow.run({"topic": "AI agent security"})
```

**What happens under the hood:**
1. `claude("...")` creates a `ClaudeAdapter`, validates credentials, returns a closure
2. The executor calls that closure with a `StepContext` containing `{topic}` from state
3. The adapter calls the Anthropic API, normalizes the response into a `ModelResponse`
4. The closure returns `response.to_dict()` — a JSON-safe dict stored in state
5. The output contract validates the dict (is `summary` a non-empty string? is `key_points` a list?)
6. If validation fails, the failure policy retries up to 2 times
7. The `report` step receives `{research}` (the dict from step 1) in its prompt template

### What the adapter returns

Each adapter call produces a `ModelResponse` with:

```python
from kairos import ModelResponse, TokenUsage

# response.text       → "The analysis shows..."
# response.model      → "claude-sonnet-4-20250514"
# response.usage      → TokenUsage(input_tokens=150, output_tokens=500, ...)
# response.latency_ms → 1234.5
# response.metadata   → {"stop_reason": "end_turn"}
```

The factory functions return `response.to_dict()` automatically, so the step output is always a JSON-safe dict that flows cleanly through state and validation.

### Mix and match providers

Different steps can use different providers in the same workflow:

```python
Step(name="brainstorm", action=claude("Brainstorm ideas for: {topic}")),
Step(name="evaluate", action=openai_adapter("Evaluate these ideas: {brainstorm}")),
Step(name="refine", action=claude("Refine the best idea: {evaluate}")),
```

### Supported providers

Kairos has dedicated adapters for Claude and OpenAI. Many other providers use the OpenAI-compatible API format, so the `OpenAIAdapter` works with them by changing the `base_url`:

| Provider | Adapter | Install | Setup |
|---|---|---|---|
| **Claude** | `ClaudeAdapter` | `pip install kairos-ai[anthropic]` | `ANTHROPIC_API_KEY` env var |
| **OpenAI** | `OpenAIAdapter` | `pip install kairos-ai[openai]` | `OPENAI_API_KEY` env var |
| **DeepSeek** | `OpenAIAdapter` | `pip install kairos-ai[openai]` | `OPENAI_API_KEY` = your DeepSeek key |
| **Mistral** | `OpenAIAdapter` | `pip install kairos-ai[openai]` | `OPENAI_API_KEY` = your Mistral key |
| **Groq** | `OpenAIAdapter` | `pip install kairos-ai[openai]` | `OPENAI_API_KEY` = your Groq key |
| **Together AI** | `OpenAIAdapter` | `pip install kairos-ai[openai]` | `OPENAI_API_KEY` = your Together key |
| **Ollama** (local) | `OpenAIAdapter` | `pip install kairos-ai[openai]` | No API key needed |
| **LM Studio** (local) | `OpenAIAdapter` | `pip install kairos-ai[openai]` | No API key needed |
| **Gemini** | *Planned* | — | — |

**Using OpenAI-compatible providers:**

```python
from kairos.adapters.openai_adapter import OpenAIAdapter

# DeepSeek
adapter = OpenAIAdapter(
    model="deepseek-chat",
    base_url="https://api.deepseek.com",
)

# Mistral
adapter = OpenAIAdapter(
    model="mistral-large-latest",
    base_url="https://api.mistral.ai/v1",
)

# Groq (fast inference)
adapter = OpenAIAdapter(
    model="llama-3.3-70b-versatile",
    base_url="https://api.groq.com/openai/v1",
)
```

**Using local models (Ollama, LM Studio):**

```python
from kairos.adapters.openai_adapter import OpenAIAdapter

# Ollama running locally (e.g., gemma3, llama3, mistral)
adapter = OpenAIAdapter(
    model="gemma3",
    base_url="http://localhost:11434/v1",
    allow_localhost=True,  # required for HTTP on localhost
)

# LM Studio running locally
adapter = OpenAIAdapter(
    model="gemma-3-4b",
    base_url="http://localhost:1234/v1",
    allow_localhost=True,
)
```

Local models don't need API keys. Set `OPENAI_API_KEY` to any non-empty value (e.g., `"not-needed"`) since the adapter validates the env var exists.

### Without adapters

You can always write your own step functions that call any LLM, API, or service. Adapters are convenience, not a requirement:

```python
import anthropic

def my_custom_step(ctx: StepContext) -> dict:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        messages=[{"role": "user", "content": f"Analyze: {ctx.inputs['data']}"}],
    )
    return {"result": response.content[0].text}

Step(name="analyze", action=my_custom_step)
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
| `claude` | `from kairos.adapters.claude import claude` | Claude adapter factory |
| `openai_adapter` | `from kairos.adapters.openai_adapter import openai_adapter` | OpenAI adapter factory |
| `ModelResponse` | `from kairos import ModelResponse` | Normalized LLM response |
| `TokenUsage` | `from kairos import TokenUsage` | Token count from LLM call |
