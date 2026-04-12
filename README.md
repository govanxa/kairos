# Kairos

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://github.com/govanxa/kairos)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](https://github.com/govanxa/kairos/blob/main/LICENSE)

> *The right action, at the right time.*

**Security-hardened, model-agnostic Python SDK for contract-enforced AI workflows with automatic recovery.**

Kairos wraps around any LLM and enforces a disciplined execution loop:

```
Goal → Plan → Execute Step → Validate Output → Pass / Retry / Re-plan → Next Step → Done
```

Without Kairos, agents silently pass broken outputs between steps, lose context mid-task, retry with raw error messages (a prompt injection vector), and fail without recovery. With Kairos, every step is contracted, validated, and secured.

---

## Installation

```bash
pip install kairos-sdk
```

Optional extras:
```bash
pip install kairos-sdk[pydantic]    # Pydantic schema support
```

---

## Quick Start

```python
from kairos import Workflow, Step, StepContext

def greet(ctx: StepContext) -> str:
    name = ctx.inputs.get("name", "World")
    return f"Hello, {name}!"

def shout(ctx: StepContext) -> str:
    greeting = ctx.inputs["greet"]
    return greeting.upper()

workflow = Workflow(
    name="hello",
    steps=[
        Step(name="greet", action=greet),
        Step(name="shout", action=shout, depends_on=["greet"]),
    ],
)

result = workflow.run({"name": "Kairos"})
print(result.output)  # "HELLO, KAIROS!"
```

---

## Key Features

- **Contract Enforcement** — every step declares its input/output shape. Validation runs automatically between steps. Broken data never silently propagates.
- **Sanitized Retry Context** — when a step retries, only structured metadata (field names, types, attempt number) is injected. Raw LLM output and exception messages are never fed back into prompts, preventing prompt injection via error messages.
- **Scoped State Access** — steps only see the state keys they need. `read_keys` and `write_keys` enforce least-privilege per step.
- **Sensitive Key Redaction** — keys matching patterns like `password`, `token`, `api_key` are automatically redacted in logs, exports, and final state.
- **Configurable Failure Recovery** — three-level policy hierarchy (Step → Workflow → Kairos defaults) with retry, skip, abort, and re-plan actions.
- **Model-Agnostic** — any callable that accepts a `StepContext` works. Plain functions, API calls, local models, or no LLM at all.

---

## Why Kairos?

Orchestration tools exist (LangGraph, CrewAI). Validation tools exist (Guardrails AI, PydanticAI). None combine both with security as architecture:

| What you need | LangGraph | CrewAI | Guardrails AI | **Kairos** |
|---|:---:|:---:|:---:|:---:|
| Multi-step workflow orchestration | Yes | Yes | No | **Yes** |
| Inter-step contract validation | No | Partial | No (per-output only) | **Yes** |
| Sanitized retry context | No | No | N/A | **Yes** |
| Scoped state access per step | No | No | N/A | **Yes** |
| Sensitive key redaction | No | No | N/A | **Yes** |
| Configurable failure policies (retry/skip/abort/re-plan) | Partial | Partial | N/A | **Yes** |

**The gap Kairos fills:** Contract-enforced workflow orchestration where security is a first-class architectural concern — not a bolt-on.

---

## Architecture

Kairos is built as a single MVP phase combining the Core Engine and Validation Layer:

| Module | Purpose |
|---|---|
| **Plan Decomposer** | Structured task graph with dependency resolution |
| **Step Executor** | Step lifecycle with timeout, retry (with jitter), and foreach fan-out |
| **State Store** | Scoped key-value store with size limits and sensitive key redaction |
| **Schema Registry** | Input/output contracts per step (Kairos DSL, Pydantic, JSON Schema) |
| **Validation Engine** | Structural and semantic validation between steps |
| **Failure Router** | Policy-driven recovery: retry, re-plan, skip, abort |

---

## Status

**Pre-release.** Architecture is complete. Implementation is in progress following strict TDD (tests before code).

**Milestone 2: Core Engine** -- in progress (6 of 12 modules complete)

| Module | Status |
|---|---|
| `enums.py` | Done |
| `exceptions.py` | Done |
| `security.py` | Done |
| `state.py` | Done |
| `step.py` | Done |
| `plan.py` | Done |
| `executor.py` | Up next |
| `schema.py` | Planned |
| `validators.py` | Planned |
| `failure.py` | Planned |
| `workflow.py` (integration) | Planned |

373 tests passing, 100% coverage across implemented modules.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how you can help.

---

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

Built by [Vanxa](https://vanxa.com)
