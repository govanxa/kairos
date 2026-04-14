# Changelog

All notable changes to the Kairos SDK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `allow_localhost` parameter on both adapters (`ClaudeAdapter`, `OpenAIAdapter`) and factory functions (`claude()`, `openai_adapter()`) — enables HTTP on localhost for local models (Ollama, LM Studio). Defaults to `False` (secure by default). 10 new tests.
- Smart retry context in adapter factory closures — factories read `ctx.retry_context` and append a `[RETRY CONTEXT]` block to the prompt on retries. The LLM receives sanitized feedback about what went wrong so it can self-correct. 8 new tests. 4-layer safety chain verified (sanitize_retry_context -> FailureRouter -> StepExecutor -> adapter factory).
- Provider compatibility table in GETTING_STARTED.md — Section 9 (Using LLM Adapters) with supported providers, install commands, and usage patterns
- Real LLM example scripts: `examples/real_claude.py`, `examples/real_openai.py` — working examples that call actual LLM APIs
- Concurrent execution example scripts: `examples/real_claude_concurrent.py`, `examples/real_openai_concurrent.py`

## [0.3.1] - 2026-04-14 — StepContext LLM Call Tracking

### Added

- **`StepContext.increment_llm_calls(count=1)`** — step actions can now participate in the LLM circuit breaker. The executor injects a lambda callback into StepContext at context construction time. Step actions call `ctx.increment_llm_calls()` to count LLM invocations against the workflow's `max_llm_calls` limit.
- **Adapter factories auto-increment** — all three adapter factories (`claude()`, `openai_adapter()`, `gemini()`) call `ctx.increment_llm_calls()` automatically after each successful API call. Existing user-written step actions can also call it manually.
- Concurrent example scripts updated with manual `ctx.increment_llm_calls()` and restored LLM calls output.
- 26 new tests (1147 total), 98% coverage.

### Security

- Lambda callback injection in `_build_context()` prevents executor exposure via `__self__` — bound methods would leak the `StepExecutor` instance to step actions. Lambda closures have no `__self__` attribute.
- Input validation: `count < 1` raises `ConfigError` at both `StepContext` and executor levels.

### Pipeline

- Code Review: PASS WITH NOTES — 1 HIGH fixed (bare `ValueError` changed to `ConfigError`)
- Security Audit: BLOCKED then CLEARED — bound method changed to lambda, count validation added
- QA: READY TO MERGE

## [0.3.0] - 2026-04-13 — Concurrent Step Execution

### Added

- **Concurrent sibling step execution** via `parallel=True` on individual steps. Steps with all dependencies satisfied run concurrently in a `ThreadPoolExecutor` using a ready-set scheduler. Default `parallel=False` behavior is unchanged — zero behavior change for existing workflows.
- **Thread-safe `StateStore`** — `threading.Lock` on all public methods (`get`, `set`, `delete`, `snapshot`, size tracking) to prevent race conditions during concurrent step execution.
- **Thread-safe LLM circuit breaker** — counter and hook emission protected by locks to prevent races that could exceed the call limit.
- **`max_concurrency` parameter** on `Workflow` and `StepExecutor` — caps the `ThreadPoolExecutor` worker count for concurrent steps.
- **`TaskGraph.get_ready_steps(completed, failed, skipped)`** — returns step IDs whose dependencies are all satisfied and not already processed.
- **`TaskGraph.get_cascade_skip_steps(failed_ids)`** — returns step IDs that transitively depend on failed steps for cascade-skipping.
- **ADR-018**: Concurrent Step Execution (`docs/architecture/adr-018-concurrent-step-execution.md`)
- 54 new tests (1121 total, 98% coverage): 15 in test_plan, 5 in test_state, 30 in test_executor, 5 in test_workflow (includes 1 updated)

### Fixed

- Pre-existing unused `type: ignore` comment in `kairos/schema.py` (mypy clean)

## [0.2.2] - 2026-04-13 — Gemini Adapter

### Added

- **Gemini Adapter** (`kairos/adapters/gemini.py`) — `GeminiAdapter` class wrapping the Google GenAI SDK (`google-genai>=1.0`), `gemini()` factory function. Reads `GOOGLE_API_KEY` from environment with `GEMINI_API_KEY` fallback. Same security pattern as Claude/OpenAI adapters (env-only credentials, HTTPS enforcement, exception sanitization, safe `__repr__`). 37 new tests.
- Code review: PASS
- Security audit: SECURE

### Fixed

- Stale `kairos-sdk` pip install hints in Claude and OpenAI adapter error messages — updated to `kairos-ai`
- Dynamic PyPI badges on README.md (version, downloads, Python versions pulled live from PyPI)

### Test Summary (cumulative — v0.2.2)
- ~1,062 total tests passing
- 37 new Gemini adapter tests
- 99% adapter coverage

## [0.2.0] - 2026-04-13 — Model Adapters

### Added

#### Ecosystem Phase — Model Adapters (COMPLETE)

Published to PyPI as `kairos-ai` v0.2.0 via automated trusted publishing (OIDC, no token needed). GitHub Release v0.2.0 created.

Full agent pipeline passed: architect -> developer -> code review -> security audit -> QA -> merge.

- ADR-012: Thin Model Adapters (`docs/architecture/adr-012-thin-model-adapters.md`) — adapters normalize response shape and enforce credential security, pass through provider kwargs without abstraction
- ADR-017: Adapter Optional Dependencies (`docs/architecture/adr-017-adapter-optional-dependencies.md`) — each adapter is an optional pip extra, core SDK stays zero-dependency, missing SDKs detected at construction time with `ConfigError`
- **Base Adapter** (`kairos/adapters/base.py`) — 47 tests. `ModelAdapter` Protocol (call, model_name, provider properties), `ModelResponse` dataclass (content, model, provider, token_usage, raw_response, metadata), `TokenUsage` dataclass, `validate_no_inline_api_key()` (rejects api_key in kwargs with SecurityError), `enforce_https()` (urlparse-based URL validation), `wrap_provider_exception()` (sanitizes provider exceptions into ExecutionError)
- **Claude Adapter** (`kairos/adapters/claude.py`) — 37 tests. `ClaudeAdapter` class wrapping the Anthropic SDK, `claude()` factory function. Reads `ANTHROPIC_API_KEY` from environment only. HTTPS enforcement on base_url. Exception chain suppression. Safe `__repr__` (no credentials).
- **OpenAI Adapter** (`kairos/adapters/openai_adapter.py`) — 36 tests. `OpenAIAdapter` class wrapping the OpenAI SDK, `openai_adapter()` factory function. File named `openai_adapter.py` to avoid shadowing the `openai` package. Reads `OPENAI_API_KEY` from environment only. Same security pattern as Claude adapter.
- **Ollama Adapter** — NOT BUILT. Deferred. `OpenAIAdapter` with `allow_localhost=True` enables Ollama/LM Studio via the OpenAI-compatible API.
- Security requirements implemented: #14 (API keys from environment only), #15 (credential leak prevention)
- Code review: 3 HIGH findings resolved (missing __init__.py exports, timeout not forwarded in Claude, substring localhost bypass)
- Security audit: 4 findings resolved (1 MEDIUM: missing safe __repr__, 3 LOW: credential kwarg names, raw field warning, localhost bypass)
- QA: READY TO MERGE — 99% adapter coverage

#### Test Summary (cumulative — v0.2.0)
- 1,025 total tests passing, 5 skipped (pydantic)
- 124 new adapter tests (47 base + 37 Claude + 36 OpenAI + 4 post-adapter enhancements)
- 99% adapter coverage

#### Documentation
- README.md updated with adapter examples and post-MVP status
- GETTING_STARTED.md updated with Section 9 (Using LLM Adapters)
- 8 example files in `examples/` directory

## [0.1.0] - 2026-04-13 — Published to PyPI

### Added

#### Published to PyPI as `kairos-ai`
- Package published to PyPI: `pip install kairos-ai`
- GitHub Actions CI workflow (`.github/workflows/ci.yml`): runs pytest + mypy + ruff on Python 3.11, 3.12, 3.13 for every push to `main`/`dev` and every pull request to `main`
- GitHub Actions publish workflow (`.github/workflows/publish.yml`): builds and publishes to PyPI on tag push (`v*`) using trusted publishing via `pypa/gh-action-pypi-publish` (no API token in GitHub Secrets)
- GitHub Release v0.1.0 created with release notes

#### MVP COMPLETE -- All 12 Modules Passing

The Kairos SDK MVP is complete. All 12 modules have passed the full agent pipeline (architect, developer, code review, security audit, QA). 898 tests, 97% coverage across 1761 statements in 12 source files. Zero security findings on the last 6 consecutive modules. The SDK is ready for release.

#### Module 12: Workflow (`kairos/workflow.py`) -- THE FINAL MVP MODULE
- 1 class (65 statements):
  - `Workflow` -- top-level orchestrator class. The single public-facing entry point for defining and running contract-enforced AI agent workflows. Constructor:
    - `name: str` -- non-empty, non-whitespace workflow identifier
    - `steps: list[Step]` -- non-empty list of Step definitions
    - `failure_policy: FailurePolicy | None` -- optional workflow-level policy (second level in three-level hierarchy)
    - `hooks: list[ExecutorHooks] | None` -- optional lifecycle hook subscribers
    - `max_llm_calls: int = 50` -- hard circuit-breaker limit
    - `sensitive_keys: list[str] | None` -- additional state key patterns to redact
    - `strict: bool = False` -- reserved for future use
    - `metadata: dict[str, object] | None` -- arbitrary JSON-serializable metadata
  - Construction validates eagerly: `ConfigError` for empty/whitespace name or empty steps list, `PlanError` for invalid graph structure (cycles, missing deps, duplicate names). Builds a `TaskGraph` and validates its structure. Populates a `SchemaRegistry` from any step input/output contracts.
  - `run(initial_inputs)` -- synchronous execution with full per-run isolation. Creates a fresh StateStore (with sensitive_keys), StructuralValidator, FailureRouter (with workflow-level policy), and StepExecutor (with hooks and circuit breaker). Merges initial_inputs into state (validates JSON serializability via StateStore.set). Returns `WorkflowResult` with final status, per-step results, duration, and redacted final state.
  - `run_async(initial_inputs)` -- stub that raises `NotImplementedError`. Async support planned for a future phase.
  - `to_dict()` -- JSON-safe serialization. Includes name, graph (via TaskGraph.to_dict()), metadata, max_llm_calls, strict, failure_policy. Excludes hooks, sensitive_keys, and all callables.
  - Properties: `name`, `steps` (shallow copy), `graph` (TaskGraph), `registry` (SchemaRegistry), `failure_policy`.
  - `__repr__` -- shows workflow name and step names.
- Security properties:
  - Per-run isolation: fresh StateStore per run() call -- no state leaks between runs
  - Sensitive key forwarding: sensitive_keys passed to StateStore, redacted in WorkflowResult.final_state
  - to_dict() security: never includes callables, hooks, or sensitive_keys (prevents leaking what patterns are considered sensitive)
  - Initial input validation: non-serializable values raise StateError before any step runs
  - No from_dict: Workflow cannot be reconstructed from serialized data (step actions are callables)
- `tests/test_workflow.py` -- 49 tests organized by TDD priority:
  - Group 1: Failure paths -- empty name, whitespace name, empty steps, cycle detection, missing deps, duplicate steps, non-serializable initial inputs
  - Group 2: Boundary conditions -- single step, no initial inputs, None initial inputs, metadata handling, strict flag
  - Group 3: Happy paths -- basic construction, multi-step with dependencies, run() execution, run() isolation (fresh state per call), failure_policy propagation, hooks propagation, max_llm_calls propagation
  - Group 4: Integration -- end-to-end validation wiring (step with output_contract triggers StructuralValidator), failure routing (validation failure routed through FailureRouter), multi-step dependency resolution, foreach fan-out, SKIP sentinel handling
  - Group 5: Security -- sensitive_keys redacted in final_state, to_dict omits hooks and sensitive_keys, run isolation (no state leakage)
  - Group 6: Serialization -- to_dict round-trip, graph serialization, failure_policy serialization, metadata serialization, SchemaRegistry population
  - Group 7: Async stub -- run_async raises NotImplementedError
- Code review: 2 rounds -- 4 HIGH (missing integration tests) + 4 MEDIUM findings, all resolved
- Security audit: zero findings -- sixth consecutive module with zero findings on first audit pass

#### Public API (`kairos/__init__.py`) -- Module 12 additions
- 1 new public export: `Workflow`
- The public API is now complete: `from kairos import Workflow, Step, Schema, StepContext, SKIP, FailurePolicy, FailureAction, WorkflowStatus, StepStatus, ForeachPolicy` (and 30+ additional symbols)

#### Test Summary (cumulative -- FINAL MVP)
- 890 total tests (15 enums + 19 exceptions + 87 security + 86 state + 92 step + 80 plan + 97 executor + 34 executor_validation + 137 schema + 110 validators + 83 failure + 49 workflow + 1 conftest), 5 skipped (pydantic)
- 97% coverage across 1761 statements in 12 source files
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 11: Executor+Validation Wiring (`kairos/executor.py` modification)
- StepExecutor wired with optional validation and failure routing (345 statements, up from 251):
  - `StepExecutor.__init__` gains two new optional parameters: `validator: Validator | None = None` and `failure_router: FailureRouter | None = None`. Both default to None for backward compatibility -- existing code that constructs StepExecutor without these parameters continues to work unchanged.
  - 3 new lifecycle hooks on `ExecutorHooks`: `on_validation_start(step, data)` fires before output contract validation, `on_validation_success(step, result)` fires when validation passes, `on_validation_failure(step, result)` fires when validation fails. All follow the existing hook safety pattern (exceptions caught, never crash executor).
  - `_validate_contract(step, output)` — internal helper that checks if the step has an `output_contract` and a validator is configured. If both exist, runs `validator.validate(output, step.output_contract)` and returns the `ValidationResult`. Returns None if no contract or no validator.
  - `_handle_failure(step, event)` — internal helper that delegates to `failure_router.handle(event, step.failure_policy)` when a failure router is configured. Returns the `RecoveryDecision` which determines whether to RETRY, SKIP, ABORT, or REPLAN. Falls back to default behavior when no router is configured.
  - `_dispatch_validation_failure(step, validation_result, attempt)` — internal helper that creates a `FailureEvent` from a validation failure, routes it through the failure router, and returns the `RecoveryDecision`. This is the bridge between the validation layer and the failure routing layer.
- `tests/test_executor_validation.py` — 34 new tests covering:
  - Validation integration: output contract validation runs after step execution, validation passes through, validation failure triggers failure routing, retry on validation failure, skip on validation failure, abort on validation failure
  - Hook lifecycle: validation hooks fire in correct order, validation failure hooks include result details
  - Edge cases: no validator configured (skip validation), no output contract (skip validation), no failure router (default behavior), validator exceptions handled gracefully
  - Backward compatibility: StepExecutor works identically when validator/failure_router are None
- `tests/test_executor.py` — minor fix: replaced bare `ValueError` with `ConfigError` for consistency with CLAUDE.md error handling standards

#### Test Summary (cumulative)
- 838 total tests (15 enums + 19 exceptions + 87 security + 86 state + 92 step + 80 plan + 97 executor + 34 executor_validation + 137 schema + 110 validators + 83 failure), 5 skipped (pydantic)
- 96% coverage across 1695 statements in 11 source files
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 10: Failure (`kairos/failure.py`)
- 4 public classes + 1 module-level constant (138 statements):
  - `FailurePolicy` — `@dataclass` configuring how a step or workflow handles failures. Fields: `on_validation_fail` (FailureAction, default RETRY), `on_execution_fail` (FailureAction, default RETRY), `max_retries` (int, default 2), `max_replans` (int, default 2), `retry_with_feedback` (bool, default True), `retry_delay` (float, default 0.0), `retry_backoff` (float, default 1.0), `fallback_action` (FailureAction, default ABORT), `custom_handler` (Callable | None, default None, excluded from repr). `__post_init__` validates: all numeric fields >= 0, fallback_action != RETRY (infinite loop prevention). `to_dict()` excludes `custom_handler` (callables are not serializable). `from_dict()` never reconstructs `custom_handler` — always None on deserialization.
  - `FailureEvent` — `@dataclass` representing a failure occurrence from the executor or validator. Fields: `step_id`, `failure_type` (FailureType.EXECUTION or VALIDATION), `error` (Exception | ValidationResult | dict), `attempt_number` (1-based), `timestamp`. `to_dict()` sanitizes error content: execution errors via `sanitize_exception()` produce `{error_class, sanitized_message}`, validation errors produce `{valid, error_count, failed_fields}` with field names sanitized via `_sanitize_validation_token()` to prevent injection payloads. `from_dict()` restores error as a plain dict (original exception not recoverable — intentional).
  - `RecoveryDecision` — `@dataclass` holding the router's output. Fields: `action` (FailureAction), `reason` (sanitized — never raw exception messages), `retry_context` (sanitized metadata dict or None), `rollback_to` (always None in MVP). `to_dict()`/`from_dict()` with security: `rollback_to` always restored as None, action validated against FailureAction enum.
  - `FailureRouter` — stateless decision engine implementing 3-level policy resolution (step → workflow → defaults). Constructor: `FailureRouter(workflow_policy=None, defaults=None)`. Core method: `handle(event, step_policy=None, replan_count=0) -> RecoveryDecision`. Decision flow: resolve effective policy → select initial action from failure_type → enforce retry limit (attempt >= max_retries → fallback) → enforce replan limit (count >= max_replans → fallback) → build sanitized reason (exception class name only) → build retry context if RETRY + feedback → return RecoveryDecision. Helper methods: `resolve_policy(step_policy)` returns the most specific non-None policy, `_build_reason()` uses only exception class names via `sanitize_exception()`, `_build_retry_context()` delegates entirely to `sanitize_retry_context()` from security.py.
  - `KAIROS_DEFAULTS` — module-level `FailurePolicy()` constant (all defaults) serving as the base level of the three-level policy hierarchy. Step policy → workflow policy → KAIROS_DEFAULTS.
- Security properties:
  - Retry context sanitization: `_build_retry_context()` delegates to `sanitize_retry_context()` — raw step output and exception messages never in retry context (security requirement #1)
  - Exception sanitization: `_build_reason()` uses only the exception class name from `sanitize_exception()` — raw messages never in reason strings (security requirement #2)
  - No callable deserialization: `FailurePolicy.from_dict()` never reconstructs `custom_handler`. `FailurePolicy.to_dict()` excludes it entirely (security requirement #8)
  - Validation field sanitization: `FailureEvent.to_dict()` sanitizes field names via `_sanitize_validation_token()` to prevent injection payloads (e.g., "IGNORE ALL INSTRUCTIONS") from reaching log output
  - State rollback safety: `RecoveryDecision.rollback_to` always restored as None — state rollback is not supported in MVP and must not be reconstructed from external data
- `tests/test_failure.py` — 80 tests organized by TDD priority:
  - Group 1: Failure paths — invalid max_retries/max_replans/retry_delay/retry_backoff, fallback_action=RETRY rejection, from_dict with missing keys, invalid FailureAction values, RecoveryDecision.from_dict with invalid action
  - Group 2: Boundary conditions — max_retries=0, max_replans=0, retry_delay=0, retry_backoff=0, attempt_number at exact boundary, single-attempt exhaustion
  - Group 3: Happy paths — FailurePolicy defaults, FailureEvent construction, RecoveryDecision fields, FailureRouter 3-level resolution, handle() for all FailureAction types, retry context generation, REPLAN routing
  - Group 4: Security — custom_handler excluded from to_dict, custom_handler never reconstructed in from_dict, retry context uses sanitize_retry_context, reason uses sanitize_exception class name only, rollback_to always None, field name sanitization in FailureEvent.to_dict
  - Group 5: Serialization — FailurePolicy JSON round-trip, FailureEvent to_dict/from_dict, RecoveryDecision to_dict/from_dict
- Code review: 2 rounds — 5 MEDIUM findings, all resolved
- Security audit: 2 rounds — 1 LOW (unsanitized field names in FailureEvent.to_dict), resolved by adding `_sanitize_validation_token`

#### Security modification (`kairos/security.py`)
- Added `_sanitize_validation_token(value)` internal function — sanitizes individual validation error tokens (field names, expected/actual strings) by truncating to 200 chars and stripping control characters. Prevents injection payloads in field names from reaching log output or downstream consumers.

#### Public API (`kairos/__init__.py`) — Module 10 additions
- 5 new public exports: `FailurePolicy`, `FailureEvent`, `RecoveryDecision`, `FailureRouter`, `KAIROS_DEFAULTS`
- `FailureType` enum was already exported from Module 1 (enums)

#### Test Summary (cumulative)
- 799 total tests (15 enums + 19 exceptions + 87 security + 86 state + 92 step + 80 plan + 97 executor + 137 schema + 110 validators + 83 failure), 5 skipped (pydantic)
- 97% coverage across 1603 statements in 11 source files
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 9: Validators (`kairos/validators.py`)
- 4 public classes + 6 built-in validator factories (203 statements):
  - `Validator` — `@runtime_checkable Protocol` defining the `.validate(data, schema) -> ValidationResult` interface. This is the duck-typing contract: any object with a conforming `validate` method can be used wherever a Validator is expected (StructuralValidator, LLMValidator, CompositeValidator, or custom implementations).
  - `StructuralValidator` — two-phase structural validation. Phase 1 delegates to `Schema.validate()` for type/required-field checking. Phase 2 runs field-level validator functions (from FieldDefinition.validators) only on fields that passed type checking in Phase 1, are present in the data dict, and are not None (for optional fields). Internal exceptions are caught and wrapped in `ValidationResult(valid=False)` — this class never raises.
  - `LLMValidator` — semantic validation using any callable `llm_fn(prompt) -> str`. Constructor validates criteria (non-empty), llm_fn (callable), and threshold (0.0-1.0). The `validate()` method serializes data via `json.dumps(data, default=str)` (falls back to `"<non-serializable data>"` on failure — never `str(data)`), builds a prompt from criteria + serialized data, runs the LLM call in a per-call ThreadPoolExecutor with timeout enforcement, and parses the response for `RESULT: PASS|FAIL` and `CONFIDENCE: <float>`. Returns `ValidationResult` with `.metadata` dict containing `"confidence"` (float) and `"raw_response"` (str). Exception messages from `llm_fn` are never exposed — only the exception class name via `sanitize_exception()`.
  - `CompositeValidator` — chains multiple validators in sequence with short-circuit on first failure. Constructor requires a non-empty list. Validators run in order; the first `ValidationResult(valid=False)` stops the chain and its errors become the final result. If all pass, returns `ValidationResult(valid=True)`.
  - 6 built-in validator factory functions:
    - `range_(min=, max=)` / `range(min=, max=)` — inclusive numeric range validator. Rejects booleans explicitly (bool subclasses int in Python, but boolean fields should use dedicated validators). Returns error string for non-numeric types.
    - `length(min=, max=)` — string/list length range validator. Reports "string" or "list" in error messages.
    - `pattern(regex, timeout=5.0)` — regex matching with ReDoS protection. Regex is pre-compiled at definition time — `ConfigError` raised immediately for invalid patterns. Matching runs in a per-call ThreadPoolExecutor; timeout triggers a validation failure (not a hang) with a message mentioning "ReDoS".
    - `one_of(values)` — allowlist membership validator. Empty allowlist means nothing passes.
    - `not_empty()` — non-empty validator for strings and lists. Strings are stripped before checking (whitespace-only = empty). None always fails. Other types pass unconditionally.
    - `custom(fn)` — wraps an arbitrary callable as a FieldValidator. Exceptions from `fn` are caught and sanitized via `sanitize_exception()` — only the exception class name appears in the error message, never the raw exception content.
  - `FieldValidator` type alias — `Callable[[Any], bool | str]` (True = pass, error string = fail)
- Security properties:
  - Exception sanitization: `custom()` and `LLMValidator` both use `sanitize_exception()` to prevent raw exception messages (which may contain API keys, credentials, or prompt injection payloads) from reaching consumers
  - ReDoS protection: `pattern()` pre-compiles regex at definition time and runs matching in a per-call thread pool with configurable timeout. Catastrophic backtracking triggers a validation failure, not a hang.
  - Safe serialization: `LLMValidator` falls back to `"<non-serializable data>"` (never `str(data)`) when `json.dumps()` fails on circular references
  - LLM exception leakage prevention: `LLMValidator` catches all exceptions from `llm_fn` and uses only the sanitized class name — the raw message is discarded
- `tests/test_validators.py` — 103 tests organized by TDD priority:
  - Group 1: Failure paths — invalid range/length args, non-numeric values, non-string pattern input, empty one_of, empty/whitespace not_empty, custom validator exceptions, StructuralValidator with missing/wrong-type fields, LLMValidator construction errors
  - Group 2: Boundary conditions — None values, booleans as numeric input, empty lists, zero-length strings, threshold edge cases (0.0, 1.0), pattern timeout
  - Group 3: Happy paths — all 6 validators with valid input, StructuralValidator two-phase flow, LLMValidator PASS/FAIL parsing, CompositeValidator chaining
  - Group 4: Security — ReDoS timeout protection, custom validator exception sanitization, LLM exception class-name-only leakage, non-serializable data fallback, regex compilation error at definition time
  - Group 5: LLM-specific — confidence parsing, threshold comparison, unparseable response handling, metadata attachment, timeout handling
  - Group 6: Coverage fallbacks — edge cases for not_empty non-string/non-list, custom fn returning error strings, StructuralValidator internal exceptions
- Code review: 2 rounds — 3 HIGH + 5 MEDIUM findings, all resolved
- Security audit: 2 rounds — 1 HIGH (LLM exception leakage in error message) + 2 LOW (str fallback in json.dumps, missing TestRegexSecurity class). All resolved.

#### Schema modification (`kairos/schema.py`)
- Added `metadata: dict[str, object]` field to `ValidationResult` dataclass (default: empty dict). Used by `LLMValidator` to attach `confidence` and `raw_response` to validation results.

#### Public API (`kairos/__init__.py`) — Module 9 additions
- 4 new public exports: `StructuralValidator`, `LLMValidator`, `CompositeValidator`, `Validator`
- Built-in validator factories are accessed via `from kairos.validators import range_, length, pattern, one_of, not_empty, custom` or `from kairos import validators as v` then `v.range(min=0, max=10)`

#### Test Summary (cumulative)
- 721 total tests (15 enums + 19 exceptions + 85 security + 86 state + 92 step + 80 plan + 97 executor + 137 schema + 110 validators), 5 skipped (pydantic)
- 97% coverage across 1462 statements in 10 source files
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 8: Schema (`kairos/schema.py`)
- 6 items (4 public, 2 internal):
  - `FieldDefinition` (internal) — canonical representation of one schema field. A `@dataclass` holding the normalized form of a DSL annotation: `name`, `field_type` (primitive type or string tag like `'list'`, `'nested'`, `'optional_<X>'`), `required`, `validators` (stored for later use by the validators module), `item_type` (for `list[T]`), `nested_schema` (for nested `Schema` fields). Custom `__eq__` excludes validators from comparison — two FieldDefinitions are equal if they have the same structural shape regardless of attached validators.
  - `FieldValidationError` — a single field-level validation error. Fields: `field` (dot-separated path like `"address.zip"` or `"items[0]"`), `expected` (human-readable expected type), `actual` (what was found), `message` (full error message), `severity` (`Severity.ERROR` default). Used as items in `ValidationResult.errors`.
  - `ValidationResult` — outcome of a schema validation. Fields: `valid` (bool), `errors` (list of `FieldValidationError`, empty when valid). Schema.validate() always returns this — it never raises exceptions.
  - `Schema` — the main schema class implementing the Kairos DSL. Constructor accepts a dict mapping field names to Python type annotations and normalizes them into `FieldDefinition` objects. Supported annotations: `str`, `int`, `float`, `bool`, `list`, `list[str]`, `list[int]`, `list[float]`, `list[bool]`, `list[Schema]`, nested `Schema`, and optional variants via `T | None`. Circular references are detected at construction time. Max nesting depth: 32 levels.
    - `validate(data)` — structural validation: type checking, required-field enforcement, NaN/Inf rejection for floats, recursive validation for nested schemas. Never raises — returns `ValidationResult`.
    - `to_json_schema()` — exports the schema as a JSON Schema object with `type`, `properties`, and `required`.
    - `from_json_schema(spec)` — creates a Schema from a JSON Schema dict. Only processes safe keywords (`type`, `properties`, `required`, `items`). Enforces recursion depth limit.
    - `from_pydantic(model)` — creates a Schema from a Pydantic v2 BaseModel subclass. Optional dependency — raises `ConfigError` if pydantic not installed.
    - `extend(fields, validators=None)` — returns a new Schema with this schema's fields merged with additional or overriding fields. Extension fields take precedence.
    - Properties: `field_definitions`, `field_names`, `required_fields`.
  - `ContractPair` — `@dataclass` holding an input/output schema pair for a workflow step. Fields: `input_schema` (Schema | None), `output_schema` (Schema | None).
  - `SchemaRegistry` (internal) — lookup table mapping step IDs to `ContractPair` objects. Populated automatically by Workflow. Methods: `register(step_id, input_schema, output_schema)`, `get_input_contract(step_id)`, `get_output_contract(step_id)`, `has_contract(step_id)`, `all_contracts()`, `export_json_schema()`.
- Security properties:
  - Recursion depth limit (_MAX_SCHEMA_DEPTH = 32) prevents stack overflow from deeply nested or crafted schemas
  - Circular reference detection at construction time prevents infinite loops
  - `from_json_schema` processes only safe keywords — unknown keywords silently ignored
  - No pickle, eval, exec, or importlib anywhere in the module
  - Validation never crashes — always returns ValidationResult with errors (catches internal exceptions)
  - NaN/Inf rejection for float fields prevents non-finite values from propagating
- `tests/test_schema.py` — 104 tests organized by TDD priority:
  - Group 1: Failure paths — unsupported types, invalid annotations, non-dict validation, missing required fields, type mismatches, NaN/Inf rejection, invalid from_json_schema input
  - Group 2: Boundary conditions — empty schema, single field, None for optional fields, empty lists, nested empty dicts
  - Group 3: Happy paths — DSL construction, all primitive types, optional types, list types, nested schemas, validate(), field_definitions/field_names/required_fields properties, extend(), repr/eq
  - Group 4: JSON Schema — to_json_schema export, from_json_schema import, round-trip, nested schemas, array types
  - Group 5: Pydantic integration — from_pydantic with various field types, optional fields, nested models (5 tests skipped when pydantic not installed)
  - Group 6: Security — depth limits, circular reference detection, safe keyword filtering
  - Group 7: Serialization — ContractPair construction, SchemaRegistry CRUD operations, export_json_schema
  - Group 8: Depth limits and circular references — explicit depth overflow, mutual circular references
  - Group 9: Additional type coverage — edge cases for type normalization
- Code review: 2 rounds — 5 HIGH test gaps + 7 MEDIUM findings, all resolved
- Security audit: 2 rounds — 2 MEDIUM (recursion depth limits) + 2 LOW (circular ref test gap, optional nested object), all resolved

#### Public API (`kairos/__init__.py`) — Module 8 additions
- 4 new public exports: `Schema`, `ValidationResult`, `FieldValidationError`, `ContractPair`
- 2 internal items (not exported): `FieldDefinition`, `SchemaRegistry`

#### Test Summary (cumulative)
- 611 total tests (15 enums + 19 exceptions + 85 security + 86 state + 92 step + 80 plan + 97 executor + 137 schema), 5 skipped (pydantic)
- 96% coverage across 1257 statements in 9 source files
- mypy strict mode: no errors
- ruff check + format: no violations

### Fixed

#### Module 6: Plan (`kairos/plan.py`) — Type Annotation Cleanup
- Fixed ~15 Pylance type-checking warnings in `kairos/plan.py` for better VS Code IDE experience
- Added `cast` import from `typing`; used `cast()` after `isinstance` checks in `from_dict()` so Pylance recognizes narrowed types
- Changed `metadata` field default from `field(default_factory=dict)` to `field(default_factory=lambda: {})` for generic type inference
- Changed `from_dict()` parameter type from `dict[str, object]` to `dict[str, Any]` (standard deserialization pattern)
- Removed redundant `isinstance(self.name, str)` check (already enforced by dataclass type annotation)
- Zero behavior changes — all 80 plan tests pass, mypy clean, ruff clean

### Added

#### Module 7: Executor (`kairos/executor.py`)
- 3 classes (251 statements):
  - `ExecutorHooks` — base class providing no-op implementations of all 7 lifecycle hooks. Consumers (RunLogger, ValidationEngine, FailureRouter) subclass this and override only the events they care about. Methods: `on_step_start(step, attempt)`, `on_step_complete(step, result)`, `on_step_fail(step, error, attempt)`, `on_step_retry(step, attempt)`, `on_step_skip(step, reason)`, `on_workflow_start(graph)`, `on_workflow_complete(result)`. Hook exceptions are caught and logged — they never crash the executor.
  - `WorkflowResult` — terminal `@dataclass` for a complete workflow run. Fields: `status` (WorkflowStatus.COMPLETE or FAILED), `step_results` (dict[str, StepResult]), `final_state` (safe snapshot with sensitive keys redacted via `to_safe_dict()`), `duration_ms`, `timestamp` (UTC), `llm_calls`. Has `to_dict()`/`from_dict()` for JSON serialization — `from_dict` validates required fields and types.
  - `StepExecutor` — the runtime engine that drives workflow step execution. Constructor: `StepExecutor(state, hooks=None, max_llm_calls=50)`. Core method: `run(graph) -> WorkflowResult`. Capabilities:
    - Executes a TaskGraph in topologically sorted order
    - Retry loops with configurable delay, backoff, and jitter (`actual = base * backoff^attempt * uniform(0.5, 1.5)`)
    - Timeout enforcement via `ThreadPoolExecutor` — step action runs in a thread with the configured timeout
    - `foreach` fan-out: reads a state key containing a list, creates virtual sub-steps (one per item), respects `ForeachPolicy.REQUIRE_ALL` (default, aborts on any failure) and `ForeachPolicy.ALLOW_PARTIAL` (continues on individual failures)
    - Scoped state proxy: when a step declares `read_keys` or `write_keys`, the executor provides a `ScopedStateProxy` in `StepContext.state` instead of the raw `StateStore`
    - Input resolution: resolves dependency outputs via `json.loads(json.dumps())` for deep copy isolation
    - LLM call counting: `increment_llm_calls(count=1)` method for step actions and validators to call. Circuit breaker raises `ExecutionError("LLM call limit reached")` at the configured maximum.
    - Sanitized retry context: on retry, `sanitize_retry_context()` produces structured metadata only — raw outputs and exceptions are never included
    - Exception sanitization: `sanitize_exception()` is used for all AttemptRecord error fields
    - Dependency failure handling: when a step fails, all dependents are skipped with reason
    - SKIP sentinel detection: when a step action returns `SKIP`, the step is marked as SKIPPED
    - Hook emission at every lifecycle transition, with safe invocation (exceptions caught and logged)
  - `_LLMCircuitBreakerError` — internal sentinel subclass of `ExecutionError` used by the circuit breaker. Users see `ExecutionError` in their except clauses.
- Security properties:
  - Retry context sanitization — raw step output and exception messages never in retry context (security requirement #1)
  - Exception sanitization — credentials and file paths redacted in all AttemptRecord error fields (security requirement #2)
  - Scoped state proxy enforcement — ScopedStateProxy provided for steps with read_keys/write_keys (security requirement #5)
  - JSON round-trip deep copy — input resolution uses `json.loads(json.dumps())`, not `copy.deepcopy()` (security requirement #7)
  - Retry jitter — all retry delays include random jitter by default: `base * backoff^attempt * uniform(0.5, 1.5)` (security requirement #10)
  - LLM call circuit breaker — workflow aborts at max_llm_calls (default 50) with `ExecutionError` (security requirement #12)
  - Hook safety — exceptions in hook methods are caught and logged, never propagate to the executor
- Design note — concurrent execution:
  - The current executor runs all steps **sequentially** in topological order. Sibling steps that are both "ready" (all dependencies satisfied) still execute one after the other. Foreach items also execute sequentially.
  - `StepConfig.parallel` and `StepConfig.max_concurrency` fields exist in the data model as **intentional groundwork** for the concurrent sibling execution enhancement, planned as the **first post-MVP update**. They are validated, serialized, and documented, but not read by the current `StepExecutor.run()` loop.
  - The `ThreadPoolExecutor` in `_invoke_action()` is used **only for per-step timeout enforcement** (max_workers=1), not for parallel step scheduling.
  - See `docs/project-management/BUILD_PROGRESS.md` § Post-MVP Roadmap for the full concurrency implementation plan.
  - Final state redaction — `WorkflowResult.final_state` uses `to_safe_dict()` for sensitive key redaction
- `tests/test_executor.py` — 90 tests organized by TDD priority:
  - Group 1: Failure paths — step exceptions, retry exhaustion, timeout enforcement, missing foreach key, dependency failure cascading, circuit breaker abort
  - Group 2: Boundary conditions — single step, no retries, zero timeout, empty foreach, max_llm_calls=1
  - Group 3: Happy paths — basic execution, multi-step with dependencies, retry success, foreach fan-out, SKIP sentinel, hook invocation order
  - Group 4: Security — sanitized retry context, exception sanitization, scoped state proxy, JSON deep copy, jitter range, circuit breaker, hook exception safety
  - Group 5: Serialization — WorkflowResult to_dict/from_dict JSON round-trip, from_dict validation
- 95% code coverage on executor module (251 statements, 13 uncovered — defensive branches for edge cases in async/timeout cleanup)
- Code review: 5 MEDIUM findings (ThreadPoolExecutor blocking on timeout, assert in production code, slow timeout tests, missing WorkflowResult.from_dict, fixtures not in conftest) — all resolved
- Security audit: zero findings — fourth consecutive module with zero findings on first audit pass

#### Public API (`kairos/__init__.py`) — Module 7 additions
- 3 new public exports: `ExecutorHooks`, `StepExecutor`, `WorkflowResult`

#### Test Summary (cumulative)
- 474 total tests (15 enums + 19 exceptions + 81 security + 86 state + 92 step + 80 plan + 101 executor)
- 99% code coverage across all 8 source files (860 statements, 1 uncovered)
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 6: Plan (`kairos/plan.py`)
- 1 class (171 statements):
  - `TaskGraph` — directed acyclic graph (DAG) of Steps with dependency resolution. Constructor accepts a list of `Step` objects. Core responsibilities:
    - `validate()` — validates the entire graph in a single pass, accumulating ALL errors before raising `PlanError`. Checks for: duplicate step names, missing dependencies, self-dependencies, and cycles (via DFS three-color algorithm with cycle path reconstruction).
    - `topological_sort()` — returns steps in dependency-respecting execution order using Kahn's algorithm. Uses insertion-order tiebreaking for deterministic output across runs.
    - `execution_order()` — alias for `topological_sort()`, provided for readability.
    - `get_step(name)` — returns a Step by name, raises `PlanError` if not found.
    - `get_dependencies(name)` — returns the list of Step objects that a given step depends on.
    - `get_dependents(name)` — returns the list of Step objects that depend on a given step.
    - `to_dict()` — serializes the graph to a JSON-safe dict (step names, dependencies, config). Callables are never serialized.
    - `from_dict(data)` — (classmethod) reconstructs a TaskGraph from a serialized dict. Assigns `_noop_action` (a fail-loud placeholder) as the action for all deserialized steps. Config is filtered to known keys only. Never uses pickle/eval/exec/importlib.
  - `_noop_action` — placeholder callable assigned to deserialized steps. Raises `PlanError` if invoked if invoked. Prevents silent failures from accidentally running a deserialized step.
- Security properties:
  - `from_dict` never reconstructs callables — deserialized steps get `_noop_action` (security requirement #8)
  - Config filtering: only known `StepConfig` fields are passed through from serialized data — unknown keys are silently dropped
  - No pickle, eval, exec, or importlib anywhere in the module
  - Cycle detection prevents infinite loops in dependency resolution
- `tests/test_plan.py` — 80 tests organized by TDD priority:
  - Group 1: Failure paths — empty graph, duplicate steps, missing deps, self-deps, cycles (simple + complex), get_step on nonexistent, invalid from_dict data
  - Group 2: Boundary conditions — single step, no deps, diamond deps, long chains
  - Group 3: Happy paths — TaskGraph construction, validate, topological_sort determinism, get_dependencies/get_dependents, execution_order alias
  - Group 4: Security — from_dict produces _noop_action, no callables in to_dict, config filtered to known keys, from_dict guard tests
  - Group 5: Serialization — JSON round-trip via to_dict/from_dict, structural preservation, _noop_action behavior
- 100% code coverage on plan module
- Code review: 3 MEDIUM findings (line length, missing importlib test, undocumented precondition) — all resolved
- Security audit: zero findings — third consecutive module with zero findings on first audit pass

#### Public API (`kairos/__init__.py`) — Module 6 additions
- 1 new public export: `TaskGraph`

#### Test Summary (cumulative)
- 373 total tests (15 enums + 19 exceptions + 81 security + 86 state + 92 step + 80 plan)
- 100% code coverage on all 7 source files (608 statements)
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 5: Step (`kairos/step.py`)
- 6 items (154 statements):
  - `Step` — workflow step definition. Holds name (validated against `[a-zA-Z0-9_-]+`), action callable, `depends_on` list, `StepConfig`, `read_keys`/`write_keys` for scoped state access, `input_contract`/`output_contract` for schema validation, and `failure_policy`. Supports convenience kwargs (e.g., `retries=3`) that auto-build a `StepConfig`. Intentionally has NO `from_dict` — step actions are callables and can never be deserialized from untrusted data (security requirement #8).
  - `StepConfig` — `@dataclass` with `__post_init__` validation. Fields: `retries` (>= 0), `timeout` (> 0 or None), `foreach` (state key for fan-out), `foreach_policy` (`ForeachPolicy.REQUIRE_ALL` default), `parallel`, `max_concurrency` (>= 1 or None), `retry_delay` (>= 0), `retry_backoff` (>= 0), `retry_jitter` (True default, security requirement #10), `validation_timeout` (> 0, 30s default). Raises `ConfigError` on invalid values.
  - `StepContext` — runtime context passed to every step action as its single argument. Fields: `state` (StateStore or ScopedStateProxy), `inputs` (resolved outputs from dependency steps), `item` (current foreach item or None), `retry_context` (sanitized metadata dict or None), `step_id`, `attempt` (1-based).
  - `StepResult` — aggregated outcome of a step execution. Fields: `step_id`, `status` (StepStatus), `output`, `attempts` (list of AttemptRecord), `duration_ms`, `timestamp`. Has `to_dict()`/`from_dict()` for JSON serialization — reconstructs structural data only, never callables.
  - `AttemptRecord` — immutable log of a single execution attempt. Fields: `attempt_number` (1-based), `status` (AttemptStatus), `output`, `error_type` (sanitized class name), `error_message` (sanitized, redacted), `duration_ms`, `timestamp`. Has `to_dict()`/`from_dict()` — `from_dict` validates required keys and raises `ConfigError` on missing/invalid data.
  - `SKIP` (`_SkipSentinel`) — singleton sentinel for voluntary step skipping. The executor detects `result is SKIP` and transitions the step to `StepStatus.SKIPPED`. Validation is not run on skipped steps. Falsy (`__bool__` returns False). `repr()` returns `"SKIP"`.
- Security properties:
  - Step has NO `from_dict` — actions can never be deserialized (security requirement #8)
  - Step names restricted to `[a-zA-Z0-9_-]+` — prevents path traversal and injection via workflow identifiers (security requirement #9)
  - AttemptRecord stores only pre-sanitized strings for error_type and error_message — never raw Exception objects (security requirement #2)
  - `from_dict` on AttemptRecord and StepResult reconstructs structural data only; callables are never reconstructed
- `tests/test_step.py` — 92 tests organized by TDD priority:
  - Group 1: Failure paths — invalid names, non-callable actions, config validation errors, from_dict with missing/invalid keys
  - Group 2: Boundary conditions — empty depends_on, None defaults, zero retries, min timeout
  - Group 3: Happy paths — Step construction, StepConfig defaults, StepContext fields, StepResult/AttemptRecord lifecycle
  - Group 4: Security — name character restriction, no from_dict on Step, sanitized-only strings in AttemptRecord, from_dict guard tests
  - Group 5: Serialization — JSON round-trip for AttemptRecord and StepResult via to_dict/from_dict
- 100% code coverage on step module
- Code review: 3 MEDIUM findings (missing from_dict guard tests) — all resolved
- Security audit: zero findings — second module in a row with zero findings on first audit pass

#### Public API (`kairos/__init__.py`) — Module 5 additions
- 6 new public exports: `Step`, `StepConfig`, `StepContext`, `StepResult`, `AttemptRecord`, `SKIP`

#### Test Summary (cumulative)
- 293 total tests (15 enums + 19 exceptions + 81 security + 86 state + 92 step)
- 100% code coverage on all 6 source files (436 statements)
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 4: State (`kairos/state.py`)
- 3 classes (109 statements):
  - `StateStore` — key-value store for workflow state. JSON-serializable value enforcement by default (raises `StateError` on non-serializable values; `allow_non_serializable=True` overrides). Hard size limit of 100MB total (`max_total_size`), soft per-key limit of 10MB (`max_value_size`) with warning. Sensitive key redaction via `to_safe_dict()` using configurable patterns + `DEFAULT_SENSITIVE_PATTERNS`. Snapshot/restore via JSON round-trip (`json.loads(json.dumps(data))`) — never `copy.deepcopy()`. Creates `ScopedStateProxy` instances for per-step access control.
  - `StateSnapshot` — frozen `@dataclass` checkpoint containing data dict, step_id, and timestamp. Used by `StateStore.snapshot()` for state rollback.
  - `ScopedStateProxy` — restricted state view enforcing `read_keys` and `write_keys` boundaries per step. Raises `StateError` on unauthorized read or write access. Supports `get()`, `set()`, `keys()`, `contains()`.
- Security requirements implemented:
  - #3 — JSON-serializable enforcement: `StateStore.set()` rejects values that fail `json.dumps()` by default
  - #4 — Sensitive key redaction: `to_safe_dict()` redacts values for keys matching sensitive patterns to `"[REDACTED]"`
  - #5 — Scoped state proxy: `ScopedStateProxy` enforces least-privilege read/write access per step
  - #6 — Size limits: 100MB total hard limit, 10MB per-key soft limit with warning
  - #7 — JSON round-trip for deep copy: `snapshot()` and data copies use `json.loads(json.dumps())`, not `copy.deepcopy()`
- `tests/test_state.py` — 86 tests organized by TDD priority:
  - Group 1: Failure paths (10 tests) — non-serializable values, size limit enforcement, missing keys, scoped proxy unauthorized access
  - Group 2: Boundary conditions (17 tests) — empty store, None values, nested dicts, zero-size limits, empty key lists
  - Group 3: Happy paths (32 tests) — get/set, snapshot/restore, scoped proxy operations, to_safe_dict, sensitive patterns
  - Group 4: Security (12 tests) — JSON round-trip (no deepcopy), scoped proxy enforcement, sensitive key redaction, size limit enforcement
  - Group 5: Serialization (8 tests) — JSON round-trip for StateStore and StateSnapshot
  - Group 6: Regression fixes (7 tests) — fixes for 2 HIGH + 4 MEDIUM code review findings
- 100% code coverage on state module
- Code review: 2 HIGH findings (snapshot/to_dict crash with non-serializable data) + 4 MEDIUM findings — all resolved
- Security audit: zero findings — first module to achieve this on first audit pass

#### Public API (`kairos/__init__.py`) — Module 4 additions
- 3 new public exports: `StateStore`, `StateSnapshot`, `ScopedStateProxy`

#### Test Summary (cumulative)
- 201 total tests (15 enums + 19 exceptions + 81 security + 86 state)
- 100% code coverage on all 5 source files (281 statements)
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 3: Security (`kairos/security.py`)
- 4 public security functions:
  - `sanitize_exception(exc) -> (str, str)` — extracts exception class name and sanitized message. Redacts credentials matching 10 compiled regex patterns (`sk-*`, `key-*`, `Bearer *`, `token=*`, `password=*`, etc.). Strips file paths to filenames only. Truncates messages to 500 characters.
  - `sanitize_retry_context(step_output, exception, attempt, failure_type, validation_errors) -> dict` — CRITICAL security function. Produces structured retry context containing only metadata (field names, expected/actual types, attempt number, generic guidance). Raw step output, raw LLM responses, and raw exception messages are NEVER included. Prevents prompt injection through retry context.
  - `redact_sensitive(data, sensitive_patterns) -> dict` — recursively redacts state dictionary values whose keys match any sensitive pattern. Uses `fnmatch` for pattern matching. Handles nested dicts, lists, and mixed structures with recursion depth protection.
  - `sanitize_path(name, base_dir) -> str` — sanitizes names for safe filesystem use. Restricts to `[a-zA-Z0-9_-]`, replaces other characters with `_`. Canonicalizes via `os.path.realpath()` and verifies the result is within `base_dir`. Rejects path traversal attempts (`..`, absolute paths) with `SecurityError`.
- `DEFAULT_SENSITIVE_PATTERNS` — 11 fnmatch patterns for automatic sensitive key detection: `*password*`, `*secret*`, `*token*`, `*api_key*`, `*api-key*`, `*apikey*`, `*credential*`, `*private_key*`, `*private-key*`, `*auth*`, `*ssn*`
- `_CREDENTIAL_PATTERNS` — 10 compiled regex patterns for credential detection in exception messages
- `tests/test_security.py` — 81 tests organized by TDD priority:
  - Group 1: Failure paths (8 tests) — invalid inputs, non-exception types, empty data
  - Group 2: Boundary conditions (12 tests) — empty strings, None values, edge cases
  - Group 3: Happy paths (12 tests) — basic functionality for all 4 functions
  - Group 4: Security constraints (25 tests across 4 classes) — credential redaction, prompt injection prevention, path traversal, sensitive key detection
  - Group 5: Serialization (5 tests) — JSON round-trip for all return types
  - Groups 6-9: Regression tests (19 tests) — fixes for 8 findings from code review and security audit
- 100% code coverage on security module
- 8 security findings discovered and resolved across 2 audit rounds:
  - 2 HIGH: prompt injection via validation_errors field in retry context
  - 4 MEDIUM: credential pattern gaps, list recursion depth
  - 1 LOW: relative path handling
  - 1 additional: list-of-lists recursion in redact_sensitive

#### Public API (`kairos/__init__.py`) — Module 3 additions
- 5 new public exports: `sanitize_exception`, `sanitize_retry_context`, `redact_sensitive`, `sanitize_path`, `DEFAULT_SENSITIVE_PATTERNS`

#### Test Summary (cumulative)
- 115 total tests (15 enums + 19 exceptions + 81 security)
- 100% code coverage on all 4 source files (171 statements)
- mypy strict mode: no errors
- ruff check + format: no violations

#### Module 1: Enums (`kairos/enums.py`)
- 11 enums using `StrEnum` for native JSON serialization:
  - `WorkflowStatus` — terminal workflow states: COMPLETE, FAILED, PARTIAL
  - `StepStatus` — step lifecycle: PENDING, RUNNING, VALIDATING, COMPLETED, FAILED, RETRYING, FAILED_FINAL, ROUTING, SKIPPED
  - `FailureAction` — failure responses: RETRY, REPLAN, SKIP, ABORT, CUSTOM
  - `FailureType` — failure categories: EXECUTION, VALIDATION
  - `ForeachPolicy` — fan-out behavior: REQUIRE_ALL, ALLOW_PARTIAL
  - `AttemptStatus` — attempt outcomes: SUCCESS, FAILURE
  - `ValidationLayer` — validation scope: STRUCTURAL, SEMANTIC, BOTH
  - `Severity` — issue severity: ERROR, WARNING
  - `LogLevel` — log event level: INFO, WARN, ERROR
  - `LogVerbosity` — logger verbosity: MINIMAL, NORMAL, VERBOSE
  - `PlanStrategy` — graph construction: MANUAL, LLM_GENERATED, HYBRID
- `tests/test_enums.py` — 15 tests covering all enum values, `str` mixin behavior, JSON serialization round-trip, and enum count verification
- All tests pass, 100% coverage, mypy clean, ruff clean

#### Module 2: Exceptions (`kairos/exceptions.py`)
- 8 exception classes in a single-inheritance hierarchy:
  - `KairosError` — base exception with `.message` attribute
  - `PlanError` — invalid plan structure (keyword-only: `step_id`)
  - `ExecutionError` — step runtime failure (keyword-only: `step_id`, `attempt`)
  - `ValidationError` — contract violation (keyword-only: `step_id`, `field`)
  - `StateError` — state access issue (keyword-only: `key`)
  - `PolicyError` — invalid failure policy configuration
  - `SecurityError` — security violation (credential leak, path traversal, unauthorized access)
  - `ConfigError` — configuration error (missing API key, invalid adapter config)
- `tests/test_exceptions.py` — 19 tests covering hierarchy verification, `isinstance` catching, default/custom attributes, keyword-only argument enforcement, exception chaining (`raise ... from`), and class count verification
- All tests pass, 100% coverage, mypy clean, ruff clean

#### Public API (`kairos/__init__.py`)
- Updated public API exports for modules 1 and 2:
  - Enums (public): `FailureAction`, `ForeachPolicy`, `LogVerbosity`, `Severity`, `StepStatus`, `WorkflowStatus`
  - Exceptions (all): `KairosError`, `PlanError`, `ExecutionError`, `ValidationError`, `StateError`, `PolicyError`, `SecurityError`, `ConfigError`
- 5 enums kept internal (not in `__all__`): `FailureType`, `AttemptStatus`, `ValidationLayer`, `LogLevel`, `PlanStrategy`

#### Test Summary
- 34 total tests (15 enums + 19 exceptions)
- 100% code coverage on both modules
- mypy strict mode: no errors
- ruff check + format: no violations

## [0.0.0] - 2026-04-11 — Project Setup Complete

### Added

#### Repository and Version Control
- GitHub repository created at https://github.com/govanxa/kairos (public, Apache 2.0, Vanxa org)
- `main` branch established — receives merges only after milestones
- `dev` branch created for all development work
- `.gitignore` configured: `.claude/`, `CLAUDE.md`, `docs/`, Python artifacts, virtual environments, IDE files, `.env` files
- LICENSE file (Apache 2.0, Vanxa copyright)

#### Project Configuration
- `pyproject.toml` fully configured:
  - Build system: hatchling
  - Package name: `kairos-ai` v0.1.0
  - Python >=3.11 required
  - Zero core dependencies (stdlib only)
  - Optional dependency groups: `pydantic`, `cli`, `anthropic`, `openai`, `dev`, `all`
  - Tool configs: ruff (lint + format), mypy (strict), pytest, pyright (strict), coverage (90% threshold)
  - Coverage omits future-phase modules (cli, adapters, plugins, interop)

#### Python Environment
- Python 3.13.13 installed
- Virtual environment at `.venv/`
- Dev dependencies installed: pytest 9.0.3, pytest-cov, pytest-asyncio, mypy 1.20.0, ruff 0.15.10, pyright 1.1.408

#### Package Skeleton
- `kairos/__init__.py` — module root with `__version__ = "0.1.0"`
- `tests/__init__.py` — test package marker
- `tests/conftest.py` — shared fixtures placeholder

#### Architecture Documentation (internal, gitignored)
- `docs/architecture/architecture.md` — canonical architecture reference
- `docs/architecture/adr-006-retry-context-injection.md`
- `docs/architecture/adr-016-security-first.md`
- Module specs for all phases:
  - Phase 1: plan-decomposer, step-executor, state-store
  - Phase 2: schema-registry, validation-engine, failure-router
  - Phase 3: run-logger, cli-runner, dashboard
  - Phase 4: model-adapters, plugin-system
- `CLAUDE.md` — canonical project instructions with full build order, coding standards, security requirements, TDD methodology

#### Claude Code Agent Team (in `.claude/agents/`)
- `kairos-architect` (opus) — designs module architecture, produces blueprints
- `kairos-developer` (sonnet) — writes tests and implementation code following TDD
- `kairos-code-reviewer` (opus) — reviews code against CLAUDE.md, ADRs, security requirements
- `kairos-security-analyst` (opus) — audits all 17 security requirements, absolute veto power
- `kairos-qa-analyst` (opus) — final quality gate, 90%+ coverage, TDD compliance
- `kairos-project-manager` (opus) — coordinates pipeline, tracks build order

#### Mandatory Agent Pipeline
- Enforced pipeline: architect -> developer -> code reviewer -> security analyst -> QA analyst -> merge
- Each agent has specific responsibilities and gate criteria documented in its agent file

#### Git Hooks (in `.git/hooks/`)
- `pre-commit` — runs `ruff check` + `ruff format --check` on staged `.py` files; blocks commit on failure
- `pre-push` — runs `pytest` + `mypy kairos/` before push; blocks push on failure; gracefully skips if no source files exist yet

#### Claude Code Hooks (in `.claude/settings.json`)
- `PostToolUse` on `Edit|Write` — auto-formats Python files via `.claude/hooks/format-python.sh`
- `PreToolUse` on `Bash` — blocks dangerous commands (rm -rf, git push --force, git reset --hard, etc.) via `.claude/hooks/block-dangerous.sh`
- `PreCompact` — agent documents session changes in CHANGELOG.md and CLAUDE.md before context compaction

#### Claude Code Plugins
- pyright-lsp — Python type checking and code intelligence
- commit-commands — /commit, /commit-push-pr, /clean_gone
- claude-md-management — CLAUDE.md audit and session learnings

#### Skills
- TDD skill at `.claude/skills/tdd/SKILL.md` — strict Red-Green-Refactor workflow with module-specific test guidance, security test checklist, mocking patterns, and commit conventions

#### Project Management
- `docs/project-management/CHANGELOG.md` — this file
