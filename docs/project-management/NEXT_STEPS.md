# Next Steps — Post-MVP Roadmap

Last updated: 2026-04-14 (v0.3.1 — StepContext LLM Call Tracking)

---

## 1. Current State Summary

The Kairos SDK MVP is complete.

- **12 core modules** built with strict TDD (test-first for every module)
- **4 model adapters** (Claude, OpenAI, Gemini + OpenAI-compatible providers via `allow_localhost`)
- **Concurrent step execution** via `parallel=True` with thread-safe StateStore and ready-set scheduler
- **1147 tests** passing, **98% coverage**
- **Zero external dependencies** for the core SDK (stdlib only)
- **8 working examples** in `examples/`
- **All quality gates green**: pytest, mypy strict, ruff check, ruff format
- **17 security requirements** implemented and tested (verified under concurrency)
- **Branch**: `dev` (pending merge to `main`)
- **Version**: 0.3.1 (set in `pyproject.toml`)

---

## 2. Publishing to PyPI

Step-by-step to get `pip install kairos-ai` working.

### 2.1 Create PyPI Accounts

1. Create an account at https://pypi.org/account/register/
2. Create an account at https://test.pypi.org/account/register/ (separate account)
3. On each site, go to Account Settings > API Tokens and create a token scoped to "Entire account" (you can scope to the project after the first upload)
4. Save both tokens securely — you will need them for twine and GitHub Actions

### 2.2 Install Build Tools

```bash
pip install build twine
```

### 2.3 Build the Package

From the project root (`kairos/`):

```bash
py -3 -m build
```

This creates two files in `dist/`:
- `kairos_ai-0.1.0.tar.gz` (source distribution)
- `kairos_ai-0.1.0-py3-none-any.whl` (wheel)

### 2.4 Test on TestPyPI First

Upload to the test index to verify everything works:

```bash
py -3 -m twine upload --repository testpypi dist/*
```

Enter your TestPyPI API token when prompted (username: `__token__`, password: the token).

Verify the install works:

```bash
pip install --index-url https://test.pypi.org/simple/ kairos-ai
```

### 2.5 Publish to Real PyPI

Once verified on TestPyPI:

```bash
py -3 -m twine upload dist/*
```

Enter your PyPI API token when prompted (username: `__token__`, password: the token).

Verify:

```bash
pip install kairos-ai
```

### 2.6 Rotate API Token After First Publish

The initial API token is scoped to "Entire account" because `kairos-ai` doesn't exist on PyPI yet. After the first successful upload:

1. Go to PyPI > Account Settings > API Tokens
2. Delete the `kairos-publish` token
3. Create a new token scoped to **just `kairos-ai`**
4. Update any local config that references the old token

This limits blast radius if the token ever leaks — it can only affect `kairos-ai`, not your entire PyPI account.

### 2.7 Automate with GitHub Actions

After the first manual publish, set up automated publishing on tag push. See Section 3.3 below for the workflow file.

---

## 3. GitHub Repository Setup

### 3.1 Public README.md

The current README.md needs to be the public-facing entry point. It should include:

- One-line description: what Kairos is
- Install command: `pip install kairos-ai`
- Quickstart code example (use `simple_chain.py` as the basis)
- Feature highlights: contract validation, retry context sanitization, scoped state, failure policies
- Link to full docs and examples
- License badge (Apache 2.0)
- Python version badge (3.11+)

### 3.2 GitHub Actions CI Workflow

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main, dev]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: pip install -e ".[dev]"
      - name: Lint
        run: |
          ruff check kairos/ tests/
          ruff format --check kairos/ tests/
      - name: Type check
        run: mypy kairos/
      - name: Test
        run: pytest --cov=kairos --cov-report=term-missing --cov-fail-under=90
```

### 3.3 Publish Workflow

Create `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - "v*"

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install build tools
        run: pip install build
      - name: Build
        run: python -m build
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

To publish a new version:
1. Update `version` in `pyproject.toml`
2. Commit and push
3. Tag: `git tag v0.1.0 && git push origin v0.1.0`
4. The workflow builds and publishes automatically

Note: This uses PyPI's trusted publisher flow (no API token needed in GitHub Secrets). You configure the trusted publisher on PyPI under your project settings. Alternatively, store a `PYPI_API_TOKEN` secret and use the `password` parameter in the publish action.

### 3.4 Branch Protection

On GitHub, go to Settings > Branches > Add rule for `main`:

- Require pull request reviews before merging
- Require status checks to pass (select the CI workflow)
- Do not allow force pushes
- Do not allow deletions

### 3.5 Issue Templates

Create `.github/ISSUE_TEMPLATE/bug_report.md` and `.github/ISSUE_TEMPLATE/feature_request.md` with standard fields. GitHub provides default templates when you create them through the web UI.

### 3.6 CONTRIBUTING.md

Create a `CONTRIBUTING.md` covering:

- How to set up the dev environment (`pip install -e ".[dev]"`)
- How to run tests (`pytest`)
- TDD requirement for core modules
- Conventional commit format
- PR process (target `dev`, not `main`)

---

## 4. Running Examples Locally

All examples are in the `examples/` directory. Run from the project root after installing with `pip install -e ".[dev]"`.

**Quick start:**

```bash
# Run the examples
py -3 examples/simple_chain.py
py -3 examples/data_pipeline.py
py -3 examples/competitive_analysis.py

# Run the full test suite
py -3 -m pytest --cov=kairos --cov-report=term-missing
```

### simple_chain.py — Basic Workflow

```bash
python examples/simple_chain.py
```

Demonstrates: step dependencies, state passing between steps, running a workflow and inspecting results. Three steps in a linear chain: prepare, process, summarize.

### data_pipeline.py — Validation and Failure Recovery

```bash
python examples/data_pipeline.py
```

Demonstrates: input/output contracts via Schema, field-level validators (range, not_empty, pattern), failure policies with retry, foreach fan-out over a collection, sensitive key redaction in the final result.

### competitive_analysis.py — Full Feature Showcase

```bash
python examples/competitive_analysis.py
```

Demonstrates: diamond dependency pattern, foreach fan-out, output contracts with Schema validation, scoped state access (read_keys/write_keys), failure policies with retry and abort, SKIP sentinel for optional steps, sensitive key redaction. This is the canonical Kairos demo.

---

## 5. Running Tests

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=kairos --cov-report=term-missing

# Run a specific test file
pytest tests/test_executor.py

# Run a specific test class
pytest tests/test_security.py::TestRetryContextSanitization

# Run a specific test
pytest tests/test_state.py::TestStateSecurity::test_sensitive_key_redacted_in_safe_dict

# Run tests matching a keyword
pytest -k "retry"

# Type checking
mypy kairos/

# Linting
ruff check kairos/ tests/

# Formatting check
ruff format --check kairos/ tests/

# Auto-format
ruff format kairos/ tests/
```

---

## 6. Post-MVP Roadmap

These phases are designed (specs in `docs/architecture/`) but deferred until user adoption validates the need.

### Phase: Observability

**Module specs:** `docs/architecture/phase3-module1-run-logger.md`, `phase3-module2-cli-runner.md`, `phase3-module3-dashboard.md`

| Module | What It Does | Notes |
|--------|-------------|-------|
| Run Logger | Structured logging of every workflow event (step start/complete/fail, state mutations, validation results) | The executor already emits lifecycle hooks — logging wires into those without code changes |
| CLI Runner | `kairos run workflow.py` — run workflows from the command line with JSON input | Depends on Run Logger for output |
| Dashboard | Localhost web UI showing run history, step traces, state timeline | Read-only, token auth, localhost-only (security requirement #17) |

Test-after is acceptable for this phase (per CLAUDE.md).

### Phase: Ecosystem

**Module specs:** `docs/architecture/phase4-module1-model-adapters.md`, `phase4-module2-plugin-system.md`

| Module | What It Does | Notes |
|--------|-------------|-------|
| Model Adapters | Thin wrappers for Claude, OpenAI, Gemini that normalize responses | COMPLETE — `pip install kairos-ai[anthropic]`, `kairos-ai[openai]`, `kairos-ai[gemini]` |
| Plugin System | Explicit plugin loading with allowlist (security requirement #16) | No auto-discovery without `allow_all=True` |
| Export & Interop | Export workflows to JSON, YAML, or other formats | Structural only — never serializes callables |

Test-after is acceptable for this phase (per CLAUDE.md).

### Enhancement: Concurrent Sibling Execution — COMPLETE (v0.3.0)

Concurrent step execution shipped in v0.3.0. Steps with `parallel=True` and all dependencies satisfied run concurrently in a `ThreadPoolExecutor` using a ready-set scheduler. Thread-safe StateStore, LLM circuit breaker, and hook emission via `threading.Lock`. `max_concurrency` parameter on Workflow and StepExecutor caps worker count. 54 new tests, all 17 security requirements verified under concurrency, zero findings. See ADR-018.

---

## 7. Immediate Priorities

Priorities 1-4 are complete. The project is now in the Ecosystem Phase, building Model Adapters.

1. ~~**Publish v0.1.0 to PyPI**~~ -- DONE. Published as `kairos-ai` v0.1.0. `pip install kairos-ai` works.

2. ~~**Set up GitHub Actions CI/CD**~~ -- DONE. CI runs pytest + mypy + ruff on Python 3.11/3.12/3.13 for every push to `main`/`dev` and every PR. Publish workflow uses trusted publishing via `pypa/gh-action-pypi-publish` on tag push.

3. ~~**Write the public README.md**~~ -- DONE. README on GitHub and PyPI with install instructions, quickstart, feature highlights, badges.

4. ~~**Create GitHub Release**~~ -- DONE. v0.1.0 release created on GitHub with release notes.

5. ~~**Build Model Adapters**~~ -- DONE. Claude, OpenAI, and Gemini adapters complete. v0.2.0 (Claude + OpenAI) and v0.2.2 (Gemini) published to PyPI. Ollama/LM Studio covered via `OpenAIAdapter` with `allow_localhost=True`.

6. ~~**Build concurrent sibling execution**~~ -- DONE. v0.3.0. Steps with `parallel=True` run concurrently via `ThreadPoolExecutor` with ready-set scheduler. Thread-safe StateStore, LLM circuit breaker, hook emission. `max_concurrency` parameter on Workflow and StepExecutor. 54 new tests (1121 total), 98% coverage, zero security findings. ADR-018 created.

7. ~~**StepContext LLM call tracking (v0.3.1)**~~ -- DONE. `StepContext.increment_llm_calls(count=1)` added. Lambda callback injection (not bound method — security fix). All three adapter factories auto-increment. Input validation: `count < 1` raises ConfigError. 26 new tests (1147 total), 98% coverage.

8. **Start collecting user feedback** -- Share the repo, get it in front of early adopters. The feedback determines priority of remaining post-MVP work (observability, plugin system).

9. **Observability Phase** -- Run Logger, CLI Runner, Dashboard. The executor already emits lifecycle hooks. Module specs in `docs/architecture/`. Test-after is acceptable.

10. **Plugin System & Export/Interop** -- Explicit plugin loading with allowlist (security requirement #16). Export workflows to JSON/YAML. Module specs in `docs/architecture/`.

---

## File Reference

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package config, version, dependencies, tool settings |
| `kairos/__init__.py` | Public API exports |
| `examples/simple_chain.py` | Basic workflow example |
| `examples/data_pipeline.py` | Validation and failure recovery example |
| `examples/competitive_analysis.py` | Full feature showcase example |
| `docs/architecture/architecture.md` | Full architecture reference |
| `docs/architecture/adr-*.md` | Architecture decision records |
| `docs/project-management/BUILD_PROGRESS.md` | Build tracker (all 12 modules complete) |
| `docs/project-management/CHANGELOG.md` | Change history |
