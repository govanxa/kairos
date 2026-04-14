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
pip install kairos-ai[gemini]       # Gemini adapter
```

### Set your API key

Adapters read credentials from environment variables — never from code. This is a security requirement (ADR-016).

```bash
# Linux/Mac
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
export OPENAI_API_KEY="sk-your-key-here"
export GOOGLE_API_KEY="your-gemini-key-here"

# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
$env:OPENAI_API_KEY = "sk-your-key-here"
$env:GOOGLE_API_KEY = "your-gemini-key-here"
```

If you try to pass `api_key` as a parameter, Kairos raises `SecurityError` immediately. This prevents accidental credential exposure in code, logs, or version control.

### Use the factory functions

The `claude()`, `openai_adapter()`, and `gemini()` functions return step-compatible callables. Use `{placeholder}` syntax to reference data from upstream steps:

```python
from kairos import Workflow, Step, Schema, FailurePolicy, FailureAction
from kairos import validators as v
from kairos.adapters.claude import claude
from kairos.adapters.openai_adapter import openai_adapter
from kairos.adapters.gemini import gemini

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
Step(name="evaluate", action=gemini("Evaluate these ideas: {brainstorm}")),
Step(name="refine", action=openai_adapter("Refine the best idea: {evaluate}")),
```

### Supported providers

Kairos has dedicated adapters for Claude, OpenAI, and Gemini. Many other providers use the OpenAI-compatible API format, so the `OpenAIAdapter` works with them by changing the `base_url`:

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
| **Gemini** | `GeminiAdapter` | `pip install kairos-ai[gemini]` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` env var |

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

### Tracking LLM calls with `ctx.increment_llm_calls()`

Kairos has a built-in **LLM circuit breaker** that aborts a workflow when too many LLM calls are made. This protects against runaway loops and unexpected cost spikes. The limit is set via `max_llm_calls` on the `Workflow` (default: 50).

**When using adapter factories** (`claude()`, `openai_adapter()`, `gemini()`), the call is counted automatically — you don't need to do anything.

**When writing custom step actions** that call an LLM directly, you must count the call yourself by calling `ctx.increment_llm_calls()` after each LLM invocation:

```python
import anthropic
from kairos import StepContext

def my_custom_step(ctx: StepContext) -> dict:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        messages=[{"role": "user", "content": f"Analyze: {ctx.inputs['data']}"}],
    )
    ctx.increment_llm_calls()  # Track LLM call for circuit breaker
    return {"result": response.content[0].text}
```

If you make multiple LLM calls in one step (for example, a research step that calls the API once per sub-topic), call `ctx.increment_llm_calls()` once for each call, or pass a `count` argument:

```python
# Two LLM calls in one step
response1 = client.messages.create(...)
response2 = client.messages.create(...)
ctx.increment_llm_calls(2)  # Count both at once
```

If the circuit breaker limit is reached, Kairos raises `ExecutionError("LLM call limit reached")` and the workflow aborts immediately. This is intentional — it prevents a broken retry loop or runaway re-planning from making hundreds of API calls silently.

---

## 10. Concurrent Step Execution

When multiple steps have all their dependencies satisfied and don't depend on each other, you can run them in parallel. Add `parallel=True` to any step that should participate in concurrent execution:

```python
from kairos import Workflow, Step, StepContext

def fetch_data(ctx: StepContext) -> dict:
    return {"items": ["alpha", "beta", "gamma"]}

def analyze_a(ctx: StepContext) -> dict:
    data = ctx.inputs["fetch_data"]
    return {"result_a": f"Analyzed {len(data['items'])} items (path A)"}

def analyze_b(ctx: StepContext) -> dict:
    data = ctx.inputs["fetch_data"]
    return {"result_b": f"Analyzed {len(data['items'])} items (path B)"}

def combine(ctx: StepContext) -> dict:
    a = ctx.inputs["analyze_a"]
    b = ctx.inputs["analyze_b"]
    return {"combined": f"{a['result_a']} + {b['result_b']}"}

workflow = Workflow(
    name="concurrent-example",
    steps=[
        Step(name="fetch_data", action=fetch_data),
        Step(name="analyze_a", action=analyze_a,
             depends_on=["fetch_data"], parallel=True),
        Step(name="analyze_b", action=analyze_b,
             depends_on=["fetch_data"], parallel=True),
        Step(name="combine", action=combine,
             depends_on=["analyze_a", "analyze_b"]),
    ],
    max_concurrency=4,
)

result = workflow.run({})
print(f"Status: {result.status.value}")
print(f"Output: {result.step_results['combine'].output}")
```

**How it works:**
- `fetch_data` runs first (no dependencies).
- `analyze_a` and `analyze_b` both depend only on `fetch_data`. Since both have `parallel=True` and their dependencies are satisfied, they run concurrently in a `ThreadPoolExecutor`.
- `combine` waits for both analysis steps to finish before running.
- `max_concurrency=4` caps the thread pool to 4 workers.

**Key points:**
- `parallel=False` (the default) is unchanged -- existing workflows work identically.
- `StateStore` is thread-safe under the hood (`threading.Lock` on all public methods).
- The LLM circuit breaker and hook emission are also thread-safe.
- Steps without `parallel=True` always run sequentially, even if siblings are ready.

### When to use `parallel=True`

Use `parallel=True` for steps that are **I/O-bound** and **independent of each other**:

- Calling multiple external APIs in the same workflow (e.g., research step A and research step B both call an LLM with different prompts)
- Fetching data from multiple sources simultaneously (databases, web APIs, file reads)
- Running independent analysis branches before a final synthesis step

Do not use `parallel=True` for CPU-bound computation — Python's GIL means threads don't speed up CPU work. And do not use it for steps that depend on each other's output — use `depends_on` to express that relationship.

### Controlling concurrency with `max_concurrency`

`max_concurrency` controls how many parallel steps can run simultaneously. Set it on the `Workflow`:

```python
workflow = Workflow(
    name="my-workflow",
    steps=[...],
    max_concurrency=4,  # At most 4 parallel steps run at the same time
)
```

If `max_concurrency` is not set, Kairos uses `min(parallel_step_count, 32)` automatically. This is sensible for most workflows — you rarely need to tune it unless you're hitting rate limits on an external API and want to throttle concurrency down.

### Hook ordering in concurrent workflows

When parallel steps run simultaneously, lifecycle hooks (`on_step_start`, `on_step_complete`, etc.) fire **in non-deterministic order** — whichever thread finishes first emits its hook first. Each hook call is thread-safe (protected by a lock), but you cannot rely on a specific ordering between concurrent steps.

Sequential steps (those without `parallel=True`) always fire hooks in topological order, as before.

### foreach and concurrency

`foreach` fan-out (where a step runs once per item in a collection) always runs **sequentially** within the step, even when `parallel=True` is set. The `parallel=True` flag controls whether the step itself participates in the concurrent scheduler — it does not parallelize the foreach items within the step. Each foreach item processes in order, one at a time.

---

## 11. Structured Run Logging

Kairos has a built-in **Run Logger** that captures every workflow event as structured, machine-readable data. It subscribes to the executor's lifecycle hooks and dispatches events to pluggable sinks.

> **Try it now:** Run `py examples/run_logger.py` for a full interactive demo showing all 4 sinks, verbosity levels, sensitive key redaction, and RunLog inspection.

### Basic setup

Create a `RunLogger` with one or more sinks and pass it as a hook to your workflow:

```python
from kairos import Workflow, Step, StepContext, RunLogger, ConsoleSink, LogLevel

def fetch(ctx: StepContext) -> dict:
    return {"items": ["alpha", "beta", "gamma"]}

def process(ctx: StepContext) -> dict:
    data = ctx.inputs["fetch"]
    return {"count": len(data["items"])}

# Create a logger with console output
logger = RunLogger(
    verbosity=LogLevel.NORMAL,
    sinks=[ConsoleSink()],
)

workflow = Workflow(
    name="logged-workflow",
    steps=[
        Step(name="fetch", action=fetch),
        Step(name="process", action=process, depends_on=["fetch"]),
    ],
    hooks=[logger],  # attach the logger as a lifecycle hook subscriber
)

result = workflow.run({"query": "test"})
```

The `ConsoleSink` prints formatted events to stderr as they happen. You will see step start, step complete, and workflow complete events in the console.

### Sink types

Kairos provides four built-in sinks. Use one or combine several:

```python
from kairos import (
    RunLogger, ConsoleSink, JSONLinesSink, FileSink, CallbackSink, LogLevel,
)

# Console — pretty-printed events to stderr (or any stream)
console = ConsoleSink()

# JSON Lines — one JSON object per line, appended to a .jsonl file
# Great for log aggregation tools (jq, Datadog, Splunk)
jsonl = JSONLinesSink("runs/my-workflow.jsonl")

# File — writes the complete RunLog as a single JSON file on close()
# Use this when you want one file per run
file_sink = FileSink("runs/my-workflow-run.json")

# Callback — forwards every event to your own function
def my_handler(event):
    print(f"[{event.event_type}] step={event.step_id}")

callback = CallbackSink(my_handler)

# Combine multiple sinks — each receives every event independently
logger = RunLogger(
    verbosity=LogLevel.NORMAL,
    sinks=[console, jsonl, file_sink, callback],
)
```

**Sink isolation:** If one sink fails (e.g., a file write error), the other sinks still receive the event. One broken sink never blocks the rest.

### Verbosity levels

The `verbosity` parameter controls how much detail the logger captures:

```python
from kairos import RunLogger, ConsoleSink, LogLevel

# MINIMAL — only workflow start/complete, step failures, validation failures
logger = RunLogger(verbosity=LogLevel.MINIMAL, sinks=[ConsoleSink()])

# NORMAL (default) — adds step start/complete/retry/skip, validation complete
logger = RunLogger(verbosity=LogLevel.NORMAL, sinks=[ConsoleSink()])

# VERBOSE — adds validation_start, full redacted step output
logger = RunLogger(verbosity=LogLevel.VERBOSE, sinks=[ConsoleSink()])
```

| Level | What you see |
|---|---|
| MINIMAL | Workflow start/complete, step failures, validation failures |
| NORMAL | Everything in MINIMAL + step start/complete/retry/skip, validation complete |
| VERBOSE | Everything in NORMAL + validation_start, full redacted step output |

Use MINIMAL in production for low-noise monitoring. Use VERBOSE during development to see exactly what data flows between steps.

### Retrieving the RunLog

After a workflow completes, call `logger.get_log()` to get the complete structured record:

```python
logger = RunLogger(verbosity=LogLevel.NORMAL, sinks=[ConsoleSink()])
workflow = Workflow(name="example", steps=[...], hooks=[logger])

result = workflow.run({"input": "data"})

# Get the complete run record
run_log = logger.get_log()

print(f"Run ID: {run_log.run_id}")
print(f"Workflow: {run_log.workflow_name}")
print(f"Status: {run_log.status}")
print(f"Events: {len(run_log.events)}")

# Access the summary metrics
summary = run_log.summary
print(f"Steps completed: {summary.steps_completed}")
print(f"Steps failed: {summary.steps_failed}")
print(f"Steps skipped: {summary.steps_skipped}")
print(f"Total retries: {summary.total_retries}")
print(f"Duration: {summary.duration_ms}ms")

# Iterate individual events
for event in run_log.events:
    print(f"  [{event.level.value}] {event.event_type} step={event.step_id}")

# Serialize to JSON for storage or analysis
import json
run_json = json.dumps(run_log.to_dict(), indent=2)
```

### Sensitive key redaction

The Run Logger integrates with Kairos's sensitive key redaction system. Any state key matching a sensitive pattern is automatically redacted to `"[REDACTED]"` in all log events **before** they reach any sink:

```python
logger = RunLogger(
    verbosity=LogLevel.VERBOSE,
    sinks=[ConsoleSink(), JSONLinesSink("runs/audit.jsonl")],
    sensitive_keys=["*api_key*", "*password*", "*token*"],
)

workflow = Workflow(
    name="secure-logged",
    steps=[...],
    hooks=[logger],
    sensitive_keys=["*api_key*", "*password*", "*token*"],
)

# When a step outputs {"result": "ok", "api_key": "sk-secret-123"}
# The log event will contain {"result": "ok", "api_key": "[REDACTED]"}
# The raw value is NEVER written to any sink
```

This means you can safely pipe logs to external systems without worrying about credential leakage. The redaction happens inside the `RunLogger` before dispatch, so even a custom `CallbackSink` receives only redacted data.

### Writing to files

The `JSONLinesSink` and `FileSink` both sanitize file paths via `sanitize_path()` to prevent path traversal attacks. Only characters matching `[a-zA-Z0-9_-]` are allowed in filenames:

```python
# Safe — characters sanitized automatically
jsonl = JSONLinesSink("runs/my-workflow.jsonl")
file_sink = FileSink("runs/output.json")

# Path traversal attempts raise SecurityError
# JSONLinesSink("../../etc/passwd")  # SecurityError
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
| `gemini` | `from kairos.adapters.gemini import gemini` | Gemini adapter factory |
| `ModelResponse` | `from kairos import ModelResponse` | Normalized LLM response |
| `TokenUsage` | `from kairos import TokenUsage` | Token count from LLM call |
| `RunLogger` | `from kairos import RunLogger` | Structured event logger (ExecutorHooks) |
| `RunLog` | `from kairos import RunLog` | Complete run record |
| `LogEvent` | `from kairos import LogEvent` | Single structured event |
| `RunSummary` | `from kairos import RunSummary` | Aggregated run metrics |
| `LogLevel` | `from kairos import LogLevel` | MINIMAL, NORMAL, VERBOSE |
| `ConsoleSink` | `from kairos import ConsoleSink` | Pretty-print events to stderr |
| `JSONLinesSink` | `from kairos import JSONLinesSink` | One JSON per line to .jsonl file |
| `FileSink` | `from kairos import FileSink` | Complete RunLog as single JSON file |
| `CallbackSink` | `from kairos import CallbackSink` | Forward events to your callback |
| `LogSink` | `from kairos import LogSink` | Protocol for custom sink implementations |
