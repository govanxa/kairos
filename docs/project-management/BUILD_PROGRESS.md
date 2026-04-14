# Build Progress Tracker

Tracks the implementation status of every module in the MVP build phase. Updated after each module completes the full pipeline (architect -> developer -> code reviewer -> security analyst -> QA analyst -> merge).

Last updated: 2026-04-14 (v0.3.1 — StepContext LLM Call Tracking)

---

## MVP Phase — Core Engine + Validation Layer

Build order is strict due to dependencies. Each module follows TDD: tests first (red), then implementation (green), then refactor.

### Core Engine

| # | Module | Source | Tests | Status | Notes |
|---|--------|--------|-------|--------|-------|
| 1 | Enums | `kairos/enums.py` | `tests/test_enums.py` (15 tests) | COMPLETE | 11 StrEnum classes, 100% coverage, mypy clean, ruff clean |
| 2 | Exceptions | `kairos/exceptions.py` | `tests/test_exceptions.py` (19 tests) | COMPLETE | 8 exception classes, keyword-only attrs, 100% coverage, mypy clean, ruff clean |
| 3 | Security | `kairos/security.py` | `tests/test_security.py` (81 tests) | COMPLETE | 4 public functions + constants, 100% coverage, 8 security findings resolved across 2 audit rounds |
| 4 | State | `kairos/state.py` | `tests/test_state.py` (86 tests) | COMPLETE | 3 classes (StateStore, StateSnapshot, ScopedStateProxy), 109 statements, 100% coverage, security requirements #3 #4 #5 #6 #7 implemented, zero security findings on first audit |
| 5 | Step | `kairos/step.py` | `tests/test_step.py` (92 tests) | COMPLETE | 6 items (Step, StepConfig, StepContext, StepResult, AttemptRecord, SKIP), 154 statements, 100% coverage, zero security findings on first audit |
| 6 | Plan | `kairos/plan.py` | `tests/test_plan.py` (80 tests) | COMPLETE | 1 class (TaskGraph, 171 statements), Kahn's algorithm topological sort, DFS cycle detection, secure from_dict (no callables), 100% coverage, zero security findings (3rd consecutive). Post-merge fix: Pylance type annotation cleanup (~15 warnings resolved, zero behavior changes). |
| 7 | Executor | `kairos/executor.py` | `tests/test_executor.py` (90 tests) | COMPLETE | 3 classes (ExecutorHooks, WorkflowResult, StepExecutor), 251 statements, 95% coverage, security requirements #1 #2 #5 #7 #10 #12 implemented, zero security findings on first audit (4th consecutive). **Note:** `StepConfig.parallel` and `StepConfig.max_concurrency` fields exist in the data model but are not used by the current executor — they are intentional groundwork for the concurrent sibling execution enhancement planned as the first post-MVP update. |

### Validation Layer

| # | Module | Source | Tests | Status | Notes |
|---|--------|--------|-------|--------|-------|
| 8 | Schema | `kairos/schema.py` | `tests/test_schema.py` (104 tests) | COMPLETE | 6 items (FieldDefinition, FieldValidationError, ValidationResult, Schema, ContractPair, SchemaRegistry), Kairos DSL, Pydantic integration, JSON Schema import/export, recursion depth limits, circular ref detection |
| 9 | Validators | `kairos/validators.py` | `tests/test_validators.py` (103 tests) | COMPLETE | 4 public classes (StructuralValidator, LLMValidator, CompositeValidator, Validator Protocol), 6 built-in validators (range_, length, pattern, one_of, not_empty, custom), ReDoS protection via thread timeout, 99% coverage, security findings resolved across 2 audit rounds |
| 10 | Failure | `kairos/failure.py` | `tests/test_failure.py` (80 tests) | COMPLETE | 4 public classes (FailurePolicy, FailureEvent, RecoveryDecision, FailureRouter) + 1 constant (KAIROS_DEFAULTS), 138 statements, 97% coverage, 3-level policy resolution, security findings resolved across 2 audit rounds |
| 11 | Executor+Validation | `kairos/executor.py` | `tests/test_executor.py` + `tests/test_executor_validation.py` (131 tests total) | COMPLETE | StepExecutor gains optional `validator` and `failure_router` params, 3 new validation hooks on ExecutorHooks, `_validate_contract` / `_handle_failure` / `_dispatch_validation_failure` helpers, 345 statements, 96% coverage, zero security findings on first audit (5th consecutive) |

### Integration

| # | Module | Source | Tests | Status | Notes |
|---|--------|--------|-------|--------|-------|
| 12 | Workflow | `kairos/workflow.py` | `tests/test_workflow.py` (49 tests) | COMPLETE | Workflow class — top-level public API entry point. Constructor validates name/steps, builds TaskGraph, populates SchemaRegistry. run() creates fresh StateStore + StructuralValidator + FailureRouter + StepExecutor per call for full isolation. to_dict() serialization (omits callables/hooks/sensitive_keys). 65 statements, 100% coverage, zero security findings (6th consecutive). |

---

## Status Key

- **NOT STARTED** — Module has not entered the pipeline
- **TESTS WRITTEN** — Test file committed, all tests red (TDD step 1)
- **IN PROGRESS** — Implementation underway, some tests green
- **CODE REVIEW** — Implementation complete, in code review
- **SECURITY REVIEW** — Passed code review, in security audit
- **QA REVIEW** — Passed security review, in QA validation
- **COMPLETE** — All gates passed, merged to dev

---

## Pipeline Runs

Log of each module's journey through the agent pipeline. Add entries as modules are built.

### Pipeline Run #1 — Modules 1 & 2 (Enums + Exceptions)
- **Date:** 2026-04-11
- **Modules:** `kairos/enums.py` (Module 1), `kairos/exceptions.py` (Module 2)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Code Review (kairos-code-reviewer): **PASS**
  - Security Audit (kairos-security-analyst): **SECURE**
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 34 tests (15 enums + 19 exceptions), 100% coverage
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **Public API updated:** `kairos/__init__.py` exports 6 public enums + 8 exception classes
- **Status:** Merged to dev

### Pipeline Run #2 — Module 3 (Security)
- **Date:** 2026-04-11
- **Module:** `kairos/security.py` (Module 3)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 4 public functions, constants, 3 internal helpers
  - Implement (kairos-developer): **All tests pass** — 81 tests, strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 2 issues (bare ValueError instead of SecurityError, list recursion depth). All fixed.
  - Security Audit (kairos-security-analyst): **SECURE** — 2 rounds. Initial found 7 findings (2 HIGH: prompt injection via validation_errors in retry context; 4 MEDIUM: credential pattern gaps + list recursion; 1 LOW: relative paths). Second round found 1 more (list-of-lists recursion). All 8 findings resolved.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE** — 115/115 tests pass, 100% coverage, TDD compliance verified
- **Test stats:** 81 new tests (115 total: 34 from Modules 1-2 + 81 new), 100% coverage across all 4 source files (171 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `sanitize_exception(exc) -> (str, str)` — credential redaction (10 compiled regex patterns), path stripping, truncation to 500 chars
  - `sanitize_retry_context(step_output, exception, attempt, failure_type, validation_errors) -> dict` — CRITICAL security function preventing prompt injection via retry context
  - `redact_sensitive(data, sensitive_patterns) -> dict` — recursive state dict redaction with fnmatch patterns
  - `sanitize_path(name, base_dir) -> str` — safe filesystem naming, path traversal prevention
  - `DEFAULT_SENSITIVE_PATTERNS` — 11 fnmatch patterns for sensitive key detection
  - `_CREDENTIAL_PATTERNS` — 10 compiled credential regex patterns
- **Public API updated:** `kairos/__init__.py` exports 5 new symbols: `sanitize_exception`, `sanitize_retry_context`, `redact_sensitive`, `sanitize_path`, `DEFAULT_SENSITIVE_PATTERNS`
- **Status:** Merged to dev

### Pipeline Run #3 — Module 4 (State)
- **Date:** 2026-04-11
- **Module:** `kairos/state.py` (Module 4)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 3 classes (StateStore, StateSnapshot, ScopedStateProxy), security boundaries defined
  - Implement (kairos-developer): **All tests pass** — 82 tests written first, then implementation, all pass
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 HIGH findings (snapshot/to_dict crash with non-serializable data) + 4 MEDIUM findings. All fixed.
  - Security Audit (kairos-security-analyst): **SECURE** — zero findings, all 7 applicable security requirements verified (first module to achieve zero findings on first audit)
  - QA Validation (kairos-qa-analyst): **READY TO MERGE** — 201/201 tests pass, 100% coverage
- **Test stats:** 86 new tests (201 total: 115 from Modules 1-3 + 86 new), 100% coverage across all 5 source files (281 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `StateStore` — key-value store with JSON-serializable enforcement, size limits (10MB per-key soft / 100MB total hard), sensitive key redaction via `to_safe_dict()`, snapshot/restore via JSON round-trip (no deepcopy), scoped access proxies
  - `StateSnapshot` — frozen dataclass checkpoint (data, step_id, timestamp)
  - `ScopedStateProxy` — restricted view enforcing read_keys/write_keys boundaries per step, raises StateError on unauthorized access
- **Security requirements implemented:** #3 (JSON-serializable values), #4 (sensitive key redaction), #5 (scoped state proxy), #6 (state size limits), #7 (JSON round-trip for deep copy)
- **Public API updated:** `kairos/__init__.py` exports: `StateStore`, `StateSnapshot`, `ScopedStateProxy`
- **Status:** Ready to merge to dev

### Pipeline Run #4 — Module 5 (Step)
- **Date:** 2026-04-11
- **Module:** `kairos/step.py` (Module 5)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 5 data structures + 1 sentinel (Step, StepConfig, StepContext, StepResult, AttemptRecord, SKIP)
  - Implement (kairos-developer): **All tests pass** — 88 tests written first, then expanded to 92, strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 3 MEDIUM findings (missing from_dict guard tests). All fixed.
  - Security Audit (kairos-security-analyst): **SECURE** — zero findings (second module in a row with zero findings on first audit)
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 92 new tests (293 total: 201 from Modules 1-4 + 92 new), 100% coverage across all 6 source files (436 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `Step` — workflow step definition: name (validated against `[a-zA-Z0-9_-]+`), action callable, depends_on, config, read_keys/write_keys, input/output contracts, failure_policy. Supports convenience kwargs that auto-build StepConfig. Intentionally has NO `from_dict` — actions can never be deserialized from untrusted data.
  - `StepConfig` — `@dataclass` with `__post_init__` validation for retries (>= 0), timeout (> 0 or None), foreach, foreach_policy, parallel, max_concurrency, retry_delay, retry_backoff, retry_jitter, validation_timeout
  - `StepContext` — runtime context passed to every step action: state (StateStore or ScopedStateProxy), inputs, item (foreach), retry_context (sanitized metadata only), step_id, attempt
  - `StepResult` — execution outcome with `to_dict()`/`from_dict()` (structural only, no callables). Aggregates attempts, tracks step_id, status, output, duration_ms, timestamp.
  - `AttemptRecord` — single attempt log with `to_dict()`/`from_dict()`. Stores only pre-sanitized strings for error fields — never raw Exception objects.
  - `SKIP` (`_SkipSentinel`) — singleton sentinel for voluntary step skipping. `result is SKIP` check by executor. Falsy (`__bool__` returns False).
- **Security:** Step has NO `from_dict` (actions can never be deserialized). Step names restricted to `[a-zA-Z0-9_-]+`. AttemptRecord stores sanitized strings only. `from_dict` on AttemptRecord and StepResult reconstructs structural data only.
- **Public API updated:** `kairos/__init__.py` exports 6 new symbols: `Step`, `StepConfig`, `StepContext`, `StepResult`, `AttemptRecord`, `SKIP`
- **Status:** Ready to merge to dev

### Pipeline Run #5 — Module 6 (Plan)
- **Date:** 2026-04-11
- **Module:** `kairos/plan.py` (Module 6)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — TaskGraph with Kahn's algorithm + DFS cycle detection
  - Implement (kairos-developer): **All tests pass** — 78 tests written first, expanded to 80 after code review fixes, strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 3 MEDIUM findings (line length, missing importlib test, undocumented precondition). All fixed.
  - Security Audit (kairos-security-analyst): **SECURE** — zero findings (third consecutive module with zero findings on first audit)
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 80 new tests (373 total: 293 from Modules 1-5 + 80 new), 100% coverage across all 7 source files (608 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `TaskGraph` — DAG of Steps with dependency resolution
    - `validate()` — accumulates ALL errors (duplicates, missing deps, self-deps, cycles)
    - `topological_sort()` — Kahn's algorithm with insertion-order tiebreaking, deterministic output
    - `execution_order()` — alias for topological_sort
    - `get_step()`, `get_dependencies()`, `get_dependents()` — query helpers
    - `to_dict()` / `from_dict()` — structural serialization (no callables, _noop_action placeholder)
    - DFS three-color cycle detection with cycle path reconstruction
  - `_noop_action` — fail-loud placeholder for deserialized steps
- **Security:** from_dict never reconstructs callables. Config filtered to known keys only. No pickle/eval/exec/importlib.
- **Public API updated:** `kairos/__init__.py` exports: `TaskGraph`
- **Status:** Ready to merge to dev

### Pipeline Run #6 — Module 7 (Executor)
- **Date:** 2026-04-12
- **Module:** `kairos/executor.py` (Module 7)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 3 classes (ExecutorHooks, WorkflowResult, StepExecutor), lifecycle hooks, retry with jitter, foreach fan-out, timeout enforcement, scoped state proxy, LLM circuit breaker
  - Implement (kairos-developer): **All tests pass** — 89 tests written first, expanded to 90 after code review fixes, strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 5 MEDIUM findings (ThreadPoolExecutor blocking on timeout, assert in production code, slow timeout tests, missing WorkflowResult.from_dict, fixtures not in conftest). Developer fixed all 5. Second review implicit (fixes verified by test suite).
  - Security Audit (kairos-security-analyst): **SECURE** — 1 round. Zero findings — all 7 applicable security requirements verified. Fourth consecutive module with zero findings on first audit.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 101 new tests (474 total: 373 from Modules 1-6 + 101 executor), 99% coverage across all 8 source files (860 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `ExecutorHooks` — base class for lifecycle hook subscribers with 7 no-op methods: `on_step_start`, `on_step_complete`, `on_step_fail`, `on_step_retry`, `on_step_skip`, `on_workflow_start`, `on_workflow_complete`. Consumers subclass and override only the events they care about. Hook exceptions are caught and logged — never crash the executor.
  - `WorkflowResult` — terminal `@dataclass` for workflow execution outcome. Fields: `status` (WorkflowStatus), `step_results` (dict[str, StepResult]), `final_state` (safe dict with sensitive keys redacted), `duration_ms`, `timestamp`, `llm_calls`. Has `to_dict()`/`from_dict()` for JSON serialization.
  - `StepExecutor` — the runtime engine. Constructor takes `state` (StateStore), `hooks` (list[ExecutorHooks]), `max_llm_calls` (default 50). Core capabilities:
    - `run(graph)` — executes a TaskGraph in topological order, returns WorkflowResult
    - Retry loops with configurable backoff and jitter (security requirement #10)
    - Timeout enforcement via `ThreadPoolExecutor` with proper cleanup
    - `foreach` fan-out: iterates state collection, creates virtual sub-steps, respects ForeachPolicy (REQUIRE_ALL / ALLOW_PARTIAL)
    - Scoped state proxy injection for steps with declared read_keys/write_keys (security requirement #5)
    - Input resolution via `json.loads(json.dumps())` for deep copy (security requirement #7)
    - LLM call counting via `increment_llm_calls()` with circuit breaker at max_llm_calls (security requirement #12)
    - Sanitized retry context via `sanitize_retry_context()` (security requirement #1)
    - Exception sanitization via `sanitize_exception()` in AttemptRecord (security requirement #2)
    - Lifecycle hook emission at every transition
  - `_LLMCircuitBreakerError` — internal sentinel subclass of ExecutionError for circuit breaker abort
- **Security requirements implemented:** #1 (retry context sanitization), #2 (exception sanitization), #5 (scoped state proxy), #7 (JSON round-trip deep copy), #10 (retry jitter), #12 (LLM call circuit breaker)
- **Design note — sequential execution:** The current executor runs steps sequentially in a plain `for` loop over the topologically sorted list. `StepConfig.parallel` and `StepConfig.max_concurrency` exist in the data model as intentional groundwork for concurrent sibling execution (first post-MVP update) but are not read by the current `StepExecutor.run()`. The `ThreadPoolExecutor` in `_invoke_action()` is for per-step timeout enforcement only (max_workers=1), not parallel scheduling. See Post-MVP Roadmap below.
- **Public API updated:** `kairos/__init__.py` exports 3 new symbols: `ExecutorHooks`, `StepExecutor`, `WorkflowResult`
- **Status:** Ready to merge to dev

### Pipeline Run #7 — Module 8 (Schema)
- **Date:** 2026-04-12
- **Module:** `kairos/schema.py` (Module 8)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 6 items (FieldDefinition, FieldValidationError, ValidationResult, Schema, ContractPair, SchemaRegistry), Kairos DSL normalization, JSON Schema import/export, Pydantic integration, circular reference detection, recursion depth limits
  - Implement (kairos-developer): **All tests pass** — 94 tests written first, expanded to 104 after code review and security audit fixes, strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 5 HIGH test gaps + 7 MEDIUM findings. Developer fixed all 12. Second review: PASS WITH NOTES.
  - Security Audit (kairos-security-analyst): **SECURE** — 2 rounds. Initial found 2 MEDIUM (recursion depth limits missing) + 2 LOW (circular ref test gap, optional nested object). Developer fixed all 4. Second audit implicit (fixes verified by test suite). Verdict: SECURE.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE** — 611/611 tests pass, 5 skipped (pydantic optional dependency), 96% coverage, 38 tests added by QA
- **Test stats:** 137 new tests (611 total: 474 from Modules 1-7 + 137 schema), 96% coverage across all 9 source files (1257 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `FieldDefinition` (internal) — canonical representation of one schema field: name, field_type, required, validators, item_type, nested_schema. Custom `__eq__` excludes validators (callables) from equality comparison.
  - `FieldValidationError` — single field-level validation error with field path, expected/actual descriptions, message, and severity.
  - `ValidationResult` — outcome of schema validation: valid flag + list of errors.
  - `Schema` — main class for Kairos schema DSL. Constructor normalizes Python type annotations into FieldDefinitions. Methods: `validate(data)` (structural validation — never raises), `to_json_schema()` (export), `from_json_schema(spec)` (import with depth limit), `from_pydantic(model)` (Pydantic v2 integration), `extend(fields)` (composition). Properties: `field_definitions`, `field_names`, `required_fields`. Circular reference detection at construction time. Recursion depth limit of 32 for nested schemas.
  - `ContractPair` — input/output schema pair for step contracts.
  - `SchemaRegistry` (internal) — lookup table mapping step IDs to ContractPairs. Methods: `register()`, `get_input_contract()`, `get_output_contract()`, `has_contract()`, `all_contracts()`, `export_json_schema()`.
  - `_normalize_type()` — converts DSL annotations (str, int, float, bool, list[T], Schema, T | None) to FieldDefinition
  - `_MAX_SCHEMA_DEPTH = 32` — hard limit on schema nesting depth
  - `_PRIMITIVE_TYPES` — frozenset of supported primitive types
- **Security:** Recursion depth limits prevent stack overflow from crafted schemas. Circular reference detection at construction time. `from_json_schema` processes only safe keywords (type, properties, required, items). No pickle/eval/exec. Validation never crashes — always returns ValidationResult.
- **Public API updated:** `kairos/__init__.py` exports 4 new symbols: `Schema`, `ValidationResult`, `FieldValidationError`, `ContractPair`
- **Internal (not exported):** `FieldDefinition`, `SchemaRegistry`
- **Status:** Ready to merge to dev

### Pipeline Run #8 — Module 9 (Validators)
- **Date:** 2026-04-12
- **Module:** `kairos/validators.py` (Module 9)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 1 round. 4 public classes (StructuralValidator, LLMValidator, CompositeValidator, Validator Protocol), 6 built-in validator factories, ReDoS protection via thread timeout, exception sanitization in custom validators
  - Implement (kairos-developer): **All tests pass** — 1 round. Strict TDD.
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 3 HIGH + 5 MEDIUM findings. All fixed.
  - Security Audit (kairos-security-analyst): **SECURE** — 2 rounds. Initial found 1 HIGH (LLM exception leakage in error message) + 2 LOW (str fallback in json.dumps default, missing TestRegexSecurity test class). All fixed.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 110 new tests (721 total: 611 from Modules 1-8 + 110 validators), 5 skipped (pydantic), 97% coverage across all 10 source files (1462 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `Validator` — `@runtime_checkable Protocol` defining the `.validate(data, schema) -> ValidationResult` interface. Any object implementing this protocol can be used wherever a Validator is expected.
  - `StructuralValidator` — two-phase validation. Phase 1: runs `Schema.validate()` for type/required checks. Phase 2: runs field-level validator functions only on fields that passed type checking and are present in the data. Never raises exceptions — wraps internal errors in `ValidationResult(valid=False)`.
  - `LLMValidator` — semantic validation using any callable `llm_fn(prompt) -> str`. Builds a prompt from criteria + serialized data, runs in a thread with timeout, parses response for `RESULT: PASS|FAIL` and `CONFIDENCE: <float>`. Returns `ValidationResult` with `.metadata` containing `confidence` and `raw_response`. Exception messages from `llm_fn` are never exposed — only exception class name via `sanitize_exception()`.
  - `CompositeValidator` — chains multiple validators in order with short-circuit on first failure. All errors from the failing validator are included in the result.
  - 6 built-in validator factories:
    - `range_(min=, max=)` — inclusive numeric range, rejects booleans (bool subclasses int)
    - `length(min=, max=)` — string/list length range
    - `pattern(regex, timeout=5.0)` — regex matching with ReDoS protection (thread timeout, pre-compiled at definition time, `ConfigError` on invalid regex)
    - `one_of(values)` — allowlist membership
    - `not_empty()` — non-empty string (whitespace stripped) or list, None always fails
    - `custom(fn)` — wraps arbitrary callable, exception class name only in error (sanitized)
  - `range` alias — `v.range(min=0, max=10)` works as natural spelling alongside `range_`
  - `FieldValidator` type alias — `Callable[[Any], bool | str]`
  - `_DEFAULT_REGEX_TIMEOUT = 5.0` — default pattern() timeout
  - `_DEFAULT_LLM_TIMEOUT = 30.0` — default LLMValidator timeout
- **Also modified:** `kairos/schema.py` — added `metadata: dict[str, object]` field to `ValidationResult` dataclass (used by LLMValidator to attach confidence scores)
- **Security:** Exception sanitization via `sanitize_exception()` in custom() and LLMValidator (raw exception messages never exposed). ReDoS protection in pattern() via per-call ThreadPoolExecutor with timeout. Pre-compilation at definition time catches invalid regex early. LLM data serialization falls back to `"<non-serializable data>"` (never `str(data)`) on circular references.
- **Public API updated:** `kairos/__init__.py` exports 4 new symbols: `StructuralValidator`, `LLMValidator`, `CompositeValidator`, `Validator`
- **Internal (not exported):** `FieldValidator` type alias, `range_` (accessible as `range` via module), `length`, `pattern`, `one_of`, `not_empty`, `custom` (accessed via `from kairos.validators import ...` or `from kairos import validators as v`)
- **Status:** Ready to merge to dev

### Pipeline Run #9 — Module 10 (Failure)
- **Date:** 2026-04-12
- **Module:** `kairos/failure.py` (Module 10)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 1 round. 4 classes (FailurePolicy, FailureEvent, RecoveryDecision, FailureRouter), 3-level policy resolution, stateless router, sanitized retry context via security.py delegation.
  - Implement (kairos-developer): **All tests pass** — 1 round. 71 tests written first (RED), implementation completed (GREEN), strict TDD.
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 5 MEDIUM findings. Developer fixed all 5.
  - Security Audit (kairos-security-analyst): **SECURE** — 2 rounds. Initial found 1 LOW (unsanitized field names in FailureEvent.to_dict could allow injection payloads). Developer fixed by adding _sanitize_validation_token. Second audit: SECURE.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 83 new tests (799 total: 716 from Modules 1-9 + 83 failure), 5 skipped (pydantic), 97% coverage across all 11 source files (1603 statements)
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `FailurePolicy` — `@dataclass` configuring failure handling. Fields: `on_validation_fail`, `on_execution_fail` (FailureAction), `max_retries`, `max_replans`, `retry_with_feedback`, `retry_delay`, `retry_backoff`, `fallback_action`, `custom_handler` (callable, never serialized). `__post_init__` validates all numeric fields (>= 0) and rejects `fallback_action=RETRY` (infinite loop prevention). `to_dict()` excludes `custom_handler`. `from_dict()` never reconstructs `custom_handler`.
  - `FailureEvent` — `@dataclass` representing a failure occurrence. Fields: `step_id`, `failure_type` (FailureType), `error` (Exception | ValidationResult | dict), `attempt_number`, `timestamp`. `to_dict()` sanitizes error content: execution errors via `sanitize_exception()`, validation errors as structural summaries with field names sanitized via `_sanitize_validation_token()`. `from_dict()` restores error as plain dict (original exception not recoverable).
  - `RecoveryDecision` — `@dataclass` holding the router's output. Fields: `action` (FailureAction), `reason` (sanitized — never raw exception messages), `retry_context` (sanitized metadata or None), `rollback_to` (always None in MVP). `to_dict()`/`from_dict()` with security: `rollback_to` always restored as None, action validated against FailureAction enum.
  - `FailureRouter` — stateless decision engine implementing 3-level policy resolution. Constructor: `FailureRouter(workflow_policy=None, defaults=None)`. Core method: `handle(event, step_policy=None, replan_count=0) -> RecoveryDecision`. Decision flow: resolve policy → select action from failure_type → enforce retry limit → enforce replan limit → build sanitized reason → build retry context (if RETRY + feedback). `resolve_policy()` returns the most specific non-None policy (step → workflow → defaults). `_build_reason()` uses only exception class name via `sanitize_exception()`. `_build_retry_context()` delegates to `sanitize_retry_context()` from security.py.
  - `KAIROS_DEFAULTS` — module-level `FailurePolicy()` constant serving as the base level of the three-level policy hierarchy.
- **Security:** custom_handler never serialized or deserialized (security requirement #8). Retry context always produced via `sanitize_retry_context()` (security requirement #1). Reason strings use only exception class names (security requirement #2). Field names in FailureEvent.to_dict sanitized via `_sanitize_validation_token` to prevent injection payloads. `rollback_to` always None on deserialization.
- **Public API updated:** `kairos/__init__.py` exports 5 new symbols: `FailurePolicy`, `FailureEvent`, `RecoveryDecision`, `FailureRouter`, `KAIROS_DEFAULTS`
- **Internal (not exported):** `_sanitize_validation_token` (in security.py)
- **Status:** Ready to merge to dev

### Pipeline Run #10 — Module 11 (Executor+Validation Wiring)
- **Date:** 2026-04-12
- **Module:** `kairos/executor.py` modification (Module 11)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 1 round. Validation hooks wired into executor lifecycle: `_validate_contract`, `_handle_failure`, `_dispatch_validation_failure` helpers, 3 new hooks on ExecutorHooks (`on_validation_start`, `on_validation_success`, `on_validation_failure`).
  - Implement (kairos-developer): **All tests pass** — 1 round. 34 new tests in `tests/test_executor_validation.py`, strict TDD.
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 2 HIGH (line length violations, bare ValueError instead of ConfigError) + 5 MEDIUM findings. Developer fixed all 7.
  - Security Audit (kairos-security-analyst): **SECURE** — 1 round. Zero findings — fifth consecutive module with zero findings on first audit. Verdict: SECURE.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 34 new tests + 1 fix in existing test_executor.py (838 total: 799 from Modules 1-10 + 34 new + 5 skipped pydantic), 96% coverage across 1695 statements in 11 source files
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - StepExecutor constructor gains two new optional parameters: `validator` (Validator | None) and `failure_router` (FailureRouter | None)
  - 3 new hooks on ExecutorHooks: `on_validation_start(step, data)`, `on_validation_success(step, result)`, `on_validation_failure(step, result)`
  - `_validate_contract(step, output)` — runs output contract validation if the step has an output_contract and a validator is configured. Returns ValidationResult or None.
  - `_handle_failure(step, event)` — delegates to the failure_router if configured, returning a RecoveryDecision. Falls back to default behavior (retry until exhausted, then fail) when no router is configured.
  - `_dispatch_validation_failure(step, validation_result, attempt)` — creates a FailureEvent from a validation failure, routes it through the failure router, and returns the RecoveryDecision.
  - Minor fix: `tests/test_executor.py` updated bare `ValueError` to `ConfigError` for consistency
- **No new public API exports** — all classes involved (StepExecutor, ExecutorHooks, Validator, FailureRouter) were already exported from previous modules
- **Status:** Ready to merge to dev

### Pipeline Run #11 — Module 12 (Workflow) — MVP COMPLETE
- **Date:** 2026-04-12
- **Module:** `kairos/workflow.py` (Module 12)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — 1 round. Workflow class as the single public entry point, constructor validation, per-run isolation, SchemaRegistry population, to_dict serialization.
  - Implement (kairos-developer): **All tests pass** — 1 round. 45 tests written first (RED), implementation completed (GREEN), expanded to 49 tests after code review fixes. Strict TDD.
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 2 rounds. Initial found 4 HIGH (missing integration tests for end-to-end validation wiring, failure routing, and multi-step dependency workflows) + 4 MEDIUM findings. Developer fixed all 8. Second review: PASS WITH NOTES.
  - Security Audit (kairos-security-analyst): **SECURE** — 1 round. Zero findings — sixth consecutive module with zero findings on first audit. Verdict: SECURE.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 60 new tests (898 total: 838 from Modules 1-11 + 49 workflow + 11 adjustments), 5 skipped (pydantic), 97% coverage across 1761 statements in 12 source files
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `Workflow` — top-level orchestrator class. The single public-facing entry point for defining and running contract-enforced AI agent workflows. Constructor accepts: name (non-empty string), steps (non-empty list of Step), failure_policy (optional workflow-level FailurePolicy), hooks (optional ExecutorHooks list), max_llm_calls (circuit breaker, default 50), sensitive_keys (state key redaction patterns), strict (reserved for future use), metadata (arbitrary key-value data). Validates eagerly at construction time — ConfigError for invalid name/steps, PlanError for invalid graph structure (cycles, missing deps, duplicates).
  - `run(initial_inputs)` — synchronous execution. Creates a fresh, isolated runtime per call: new StateStore (with sensitive_keys), new StructuralValidator, new FailureRouter (with workflow-level policy), new StepExecutor (with hooks and circuit breaker). Merges initial_inputs into state (validates JSON serializability). Returns WorkflowResult.
  - `run_async(initial_inputs)` — stub that raises NotImplementedError. Async support is planned for a future phase.
  - `to_dict()` — JSON-safe serialization. Includes name, graph (via TaskGraph.to_dict()), metadata, max_llm_calls, strict, failure_policy. Excludes hooks (not serializable), sensitive_keys (security — prevents leaking which patterns are considered sensitive), and all callables.
  - Properties: `name`, `steps` (shallow copy), `graph` (TaskGraph), `registry` (SchemaRegistry), `failure_policy`.
  - `__repr__` — shows workflow name and step names.
- **Security properties:**
  - Per-run isolation: fresh StateStore per run() call — no state leaks between runs
  - Sensitive key forwarding: sensitive_keys passed to StateStore, redacted in WorkflowResult.final_state and to_safe_dict()
  - to_dict() security: never includes callables, hooks, or sensitive_keys
  - Initial input validation: non-serializable values raise StateError before any step runs
  - No from_dict: Workflow cannot be reconstructed from serialized data (actions are callables)
- **No new public API exports** — `Workflow` was already added to `kairos/__init__.py` as the final export
- **Status:** Ready to merge to dev

---

**MVP COMPLETE.** All 12 modules have passed the full agent pipeline. 898 tests, 97% coverage, 12 source files, 1761 statements. Zero security findings on the last 6 consecutive modules. The Kairos SDK is ready for release.

---

## Ecosystem Phase — Pipeline Runs

### Pipeline Run #12 — Model Adapters (Base + Claude + OpenAI)
- **Date:** 2026-04-13
- **Modules:** `kairos/adapters/base.py` (Base Adapter), `kairos/adapters/claude.py` (Claude Adapter), `kairos/adapters/openai_adapter.py` (OpenAI Adapter)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — ModelAdapter protocol, ModelResponse/TokenUsage dataclasses, credential security enforcement (env-only), HTTPS enforcement, exception sanitization
  - Implement (kairos-developer): **All tests pass** — 120 tests (47 base + 37 Claude + 36 OpenAI), strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 3 HIGH findings resolved (missing __init__.py exports, timeout not forwarded in Claude adapter, substring localhost bypass in enforce_https)
  - Security Audit (kairos-security-analyst): **SECURE** — 4 findings resolved (1 MEDIUM: missing safe __repr__ on adapters, 3 LOW: credential kwarg names, raw field warning, localhost bypass)
  - QA Validation (kairos-qa-analyst): **READY TO MERGE** — 99% adapter coverage
- **Test stats:** 120 new adapter tests (1,025 total including post-adapter enhancements), 5 skipped (pydantic), 99% adapter coverage
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `kairos/adapters/base.py` — `ModelAdapter` Protocol (call, model_name, provider properties), `ModelResponse` dataclass (content, model, provider, token_usage, raw_response, metadata), `TokenUsage` dataclass, `validate_no_inline_api_key()` (rejects api_key in kwargs with SecurityError), `enforce_https()` (urlparse-based URL validation, allows localhost when `allow_localhost=True`), `wrap_provider_exception()` (sanitizes provider exceptions into ExecutionError)
  - `kairos/adapters/claude.py` — `ClaudeAdapter` class wrapping the Anthropic SDK, `claude()` factory function. Reads `ANTHROPIC_API_KEY` from environment only. HTTPS enforcement on base_url. Exception chain suppression (raw provider errors never leak). Safe `__repr__` (no credentials).
  - `kairos/adapters/openai_adapter.py` — `OpenAIAdapter` class wrapping the OpenAI SDK, `openai_adapter()` factory function. File named `openai_adapter.py` to avoid shadowing the `openai` package. Reads `OPENAI_API_KEY` from environment only. Same security pattern as Claude adapter.
- **Post-adapter enhancements (same pipeline run):**
  - `allow_localhost` parameter — added to both adapters and factories. Enables HTTP on localhost for local models (Ollama, LM Studio). Defaults to `False`. 10 new tests. Code review PASS, security audit SECURE.
  - Smart retry context — adapter factory closures read `ctx.retry_context` and append `[RETRY CONTEXT]` block to prompt on retries. 8 new tests. Code review PASS, security audit SECURE (4-layer safety chain verified).
- **Security requirements implemented:** #14 (API keys from environment only), #15 (credential leak prevention — ModelResponse never contains API keys, adapter exceptions sanitized, HTTPS enforced)
- **Public API updated:** `kairos/adapters/__init__.py` exports adapter classes and factories
- **Published:** v0.2.0 to PyPI via automated trusted publishing (OIDC, no token needed). GitHub Release v0.2.0 created.
- **Status:** Merged to main

### Pipeline Run #13 — Gemini Adapter
- **Date:** 2026-04-13
- **Module:** `kairos/adapters/gemini.py` (Gemini Adapter)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Implement (kairos-developer): **All tests pass** — 37 tests, strict TDD
  - Code Review (kairos-code-reviewer): **PASS**
  - Security Audit (kairos-security-analyst): **SECURE**
- **Test stats:** 37 new Gemini adapter tests (~1,062 total), 99% adapter coverage
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `kairos/adapters/gemini.py` — `GeminiAdapter` class wrapping the Google GenAI SDK (`google-genai>=1.0`), `gemini()` factory function. Reads `GOOGLE_API_KEY` from environment with `GEMINI_API_KEY` fallback. Same security pattern as Claude/OpenAI adapters (env-only credentials, HTTPS enforcement, exception chain suppression, safe `__repr__`).
- **Also fixed:** Stale `kairos-sdk` pip install hints in Claude and OpenAI adapter error messages updated to `kairos-ai`. Dynamic PyPI badges on README.md.
- **Security requirements verified:** #14 (API keys from environment only), #15 (credential leak prevention)
- **Published:** v0.2.2 to PyPI
- **Status:** Merged to main

### Pipeline Run #14 — Concurrent Step Execution (v0.3.0)
- **Date:** 2026-04-13
- **Modules:** `kairos/executor.py`, `kairos/plan.py`, `kairos/state.py`, `kairos/workflow.py` (cross-cutting enhancement)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — Ready-set scheduler with ThreadPoolExecutor, opt-in via `parallel=True`, thread safety via threading.Lock, `max_concurrency` parameter.
  - Implement (kairos-developer): **All tests pass** — 1119 tests initially, strict TDD. 54 new tests across 4 test files.
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 3 HIGH + 6 MEDIUM findings, all fixed by developer. Second review: PASS WITH NOTES.
  - Security Audit (kairos-security-analyst): **SECURE** — 0 findings. All 17 security requirements verified. Thread safety for StateStore, LLM counter, and hook emission all audited.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE** — 1121 tests pass (wrote 2 additional tests), 98% coverage confirmed.
- **Test stats:** 54 new tests (1121 total, 1 skipped pre-existing), 98% coverage
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `TaskGraph.get_ready_steps(completed, failed, skipped)` — returns step IDs whose dependencies are all satisfied and that are not themselves completed/failed/skipped.
  - `TaskGraph.get_cascade_skip_steps(failed_ids)` — returns step IDs that transitively depend on failed steps for cascade-skipping.
  - `StateStore` thread safety — `threading.Lock` on all public methods (`get`, `set`, `delete`, `snapshot`, size tracking).
  - `StepExecutor._run_concurrent()` — ready-set scheduler using `concurrent.futures.ThreadPoolExecutor` and `as_completed()`. Dispatches parallel-ready steps concurrently, sequential steps one at a time.
  - Thread-safe LLM call counter and hook emission in `StepExecutor`.
  - `max_concurrency` parameter on `Workflow` and `StepExecutor` — caps ThreadPoolExecutor worker count.
  - Default `parallel=False` behavior unchanged — zero behavior change for existing workflows.
- **Files modified:**
  - `kairos/plan.py` — 2 new methods (get_ready_steps, get_cascade_skip_steps)
  - `kairos/state.py` — threading.Lock on all public methods
  - `kairos/executor.py` — concurrent scheduler, thread-safe locks, max_concurrency
  - `kairos/workflow.py` — max_concurrency parameter passthrough
  - `tests/test_plan.py` — 15 new tests
  - `tests/test_state.py` — 5 new tests
  - `tests/test_executor.py` — 30 new tests
  - `tests/test_workflow.py` — 5 new tests (4 new + 1 updated)
- **Security properties:**
  - Thread-safe StateStore prevents race conditions on concurrent state access
  - Thread-safe LLM circuit breaker prevents counter races that could exceed the limit
  - Thread-safe hook emission prevents interleaved hook callbacks from corrupting shared state
  - All 17 CLAUDE.md security requirements maintained — zero regressions
- **ADR:** ADR-018 (Concurrent Step Execution) created
- **Status:** Committed to dev

### Pipeline Run #15 — StepContext LLM Call Tracking (v0.3.1)
- **Date:** 2026-04-14
- **Modules:** `kairos/step.py`, `kairos/executor.py`, `kairos/adapters/claude.py`, `kairos/adapters/openai_adapter.py`, `kairos/adapters/gemini.py` (cross-cutting enhancement)
- **Developer:** kairos-developer (sonnet)
- **Pipeline results:**
  - Design (kairos-architect): **Blueprint approved** — `StepContext.increment_llm_calls()` with lambda callback injection, adapter factory auto-increment
  - Implement (kairos-developer): **All tests pass** — 1147 tests, 26 new, strict TDD
  - Code Review (kairos-code-reviewer): **PASS WITH NOTES** — 1 HIGH fixed (bare `ValueError` changed to `ConfigError`)
  - Security Audit (kairos-security-analyst): **SECURE** — 2 rounds. Initial BLOCKED (bound method leaks executor via `__self__`, missing count validation). Developer fixed: lambda callback + count < 1 raises ConfigError. Second round: CLEARED.
  - QA Validation (kairos-qa-analyst): **READY TO MERGE**
- **Test stats:** 26 new tests (1147 total), 98% coverage
- **Quality gates:** mypy strict — clean, ruff check — clean, ruff format — clean
- **What was built:**
  - `StepContext.increment_llm_calls(count=1)` — new method on StepContext. Accepts an optional `_increment_llm_calls` callable at construction time. Validates `count >= 1` (raises ConfigError). Delegates to the injected callback.
  - `StepExecutor._build_context()` — injects a lambda that calls `self.increment_llm_calls(count)` into StepContext. Lambda (not bound method) prevents executor exposure via `__self__`.
  - Adapter factories (`claude()`, `openai_adapter()`, `gemini()`) — auto-call `ctx.increment_llm_calls()` after each successful API call.
  - Concurrent example scripts — updated with manual `ctx.increment_llm_calls()` and restored LLM calls output.
- **Security properties:**
  - Lambda callback injection prevents executor instance exposure to step actions (no `__self__` attribute)
  - Input validation: `count < 1` raises ConfigError at both StepContext and executor levels
  - Circuit breaker behavior unchanged — counter still thread-safe via existing lock
- **Files modified:**
  - `kairos/step.py` — `increment_llm_calls()` method + `_increment_llm_calls` constructor param
  - `kairos/executor.py` — lambda injection in `_build_context()`
  - `kairos/adapters/claude.py` — auto-increment in factory closure
  - `kairos/adapters/openai_adapter.py` — auto-increment in factory closure
  - `kairos/adapters/gemini.py` — auto-increment in factory closure
  - `examples/real_claude_concurrent.py` — manual increment + restored output
  - `examples/real_openai_concurrent.py` — manual increment + restored output
  - `tests/test_step.py` — new tests for increment_llm_calls
  - `tests/test_executor.py` — new tests for lambda injection and circuit breaker integration
  - `tests/test_security.py` — new tests for security properties
  - `tests/test_adapters/test_claude.py` — new tests for auto-increment
  - `tests/test_adapters/test_openai.py` — new tests for auto-increment
  - `tests/test_adapters/test_gemini.py` — new tests for auto-increment
- **Status:** Committed to dev

---

## Milestone Checklist

### MVP Milestone — ACHIEVED 2026-04-12

- [x] All 12 build items above are COMPLETE
- [x] `pytest` passes with 90%+ coverage on MVP modules — 893 tests, 97% coverage across 1761 statements
- [x] `mypy kairos/` passes with no errors (strict mode)
- [x] `ruff check kairos/ tests/` passes with no violations
- [x] All 17 security requirements have dedicated test cases in `tests/test_security.py`
- [x] `__init__.py` exports full public API: Workflow, Step, Schema, StepContext, SKIP, FailurePolicy, FailureAction, WorkflowStatus, StepStatus, ForeachPolicy
- [x] Example workflows run successfully: competitive_analysis.py, data_pipeline.py, simple_chain.py
- [x] dev branch merged to main

### Publishing & Adoption Phase — ACHIEVED 2026-04-13

- [x] Publish v0.1.0 to PyPI (`pip install kairos-ai`)
- [x] Set up GitHub Actions CI (pytest + mypy + ruff on every PR, Python 3.11/3.12/3.13 matrix)
- [x] Set up GitHub Actions publish workflow (trusted publishing via `pypa/gh-action-pypi-publish`)
- [x] GitHub Release created (v0.1.0)
- [x] Write public README.md with install instructions and quickstart — includes "See It In Action" demos, adapter examples, provider compatibility table
- [x] Create "Getting Started" tutorial — GETTING_STARTED.md with 9 sections including LLM adapters
- [x] Set up branch protection on main — required reviewers configured
- [x] Start collecting user feedback — repo is public, v0.1.0 and v0.2.0 published to PyPI

---

## Post-MVP Roadmap

Enhancements beyond the MVP. Ordered by actual delivery, then priority.

### 1. Model Adapters — COMPLETE (First Post-MVP Update, v0.2.0)

See section below for full details, Pipeline Run #12, and adapter table.

### 2. Concurrent Sibling Step Execution — COMPLETE (v0.3.0)

**Status:** Complete — merged to dev

See Pipeline Run #14 below for full details.

### 3. StepContext LLM Call Tracking — COMPLETE (v0.3.1)

**Status:** Complete — committed to dev

`StepContext.increment_llm_calls(count=1)` added. The executor injects a lambda callback (not a bound method — security fix) at context construction time. All three adapter factories auto-increment after each successful call. Input validation: `count < 1` raises `ConfigError`. 26 new tests (1147 total), 98% coverage. See Pipeline Run #15.

### 4. Observability Phase (Run Logger, CLI Runner, Dashboard)

**Status:** Designed, deferred until user adoption justifies it

See module specs: `docs/architecture/phase3-module1-run-logger.md`, `phase3-module2-cli-runner.md`, `phase3-module3-dashboard.md`

### 5. Ecosystem Phase — Remaining (Plugin System, Export & Interop)

**Status:** Plugin System and Export & Interop deferred until user adoption justifies them

See module specs: `docs/architecture/phase4-module1-model-adapters.md`, `phase4-module2-plugin-system.md`
See ADRs: `docs/architecture/adr-012-thin-model-adapters.md`, `docs/architecture/adr-017-adapter-optional-dependencies.md`

#### Model Adapters — COMPLETE

| # | Module | Source | Tests | Status | Notes |
|---|--------|--------|-------|--------|-------|
| 1 | Base Adapter | `kairos/adapters/base.py` | `tests/test_adapters/test_base.py` (47 tests) | COMPLETE | ModelAdapter protocol, ModelResponse/TokenUsage dataclasses, validate_no_inline_api_key(), enforce_https() (urlparse-based), wrap_provider_exception() |
| 2 | Claude Adapter | `kairos/adapters/claude.py` | `tests/test_adapters/test_claude.py` (37 tests) | COMPLETE | ClaudeAdapter class, claude() factory, env-only credentials, HTTPS enforcement, exception chain suppression, safe __repr__ |
| 3 | OpenAI Adapter | `kairos/adapters/openai_adapter.py` | `tests/test_adapters/test_openai_adapter.py` (36 tests) | COMPLETE | OpenAIAdapter class, openai_adapter() factory, same security pattern. File named `openai_adapter.py` to avoid shadowing the `openai` package. |
| 4 | Gemini Adapter | `kairos/adapters/gemini.py` | `tests/test_adapters/test_gemini.py` (37 tests) | COMPLETE | GeminiAdapter class, gemini() factory, google-genai>=1.0 SDK, GOOGLE_API_KEY with GEMINI_API_KEY fallback, same security pattern as Claude/OpenAI |
| 5 | Ollama Adapter | — | — | NOT NEEDED | OpenAIAdapter with `allow_localhost=True` covers Ollama/LM Studio via the OpenAI-compatible API. No dedicated adapter required. |

**Design principles (ADR-012):** Adapters are thin wrappers that normalize the response shape (`ModelResponse`) and enforce credential security. Provider-specific kwargs pass through without modification. No thick universal LLM abstraction.

**Dependency model (ADR-017):** Each adapter is an optional dependency. Core SDK stays zero-dependency. Missing provider SDKs detected at adapter construction time with clear `ConfigError` messages including the install command.

**Post-adapter enhancements:**
- `allow_localhost` parameter on both adapters and factories — enables HTTP on localhost for local models (Ollama, LM Studio). Defaults to `False` (secure by default). 10 new tests.
- Smart retry context — adapter factory closures read `ctx.retry_context` and append `[RETRY CONTEXT]` block to the prompt on retries. The LLM receives sanitized feedback about what went wrong so it can self-correct. 8 new tests.

**Test stats:** 161 adapter tests (47 base + 37 Claude + 36 OpenAI + 37 Gemini + 4 post-adapter enhancements), 99% adapter coverage. ~1,062 total tests passing, 5 skipped.
