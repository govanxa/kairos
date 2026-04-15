# Contributing to Kairos

Thanks for your interest in contributing to Kairos! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/govanxa/kairos.git
cd kairos

# Install in development mode with all dev dependencies
pip install -e ".[dev]"

# Run the test suite
pytest

# Type checking
mypy kairos/

# Linting and formatting
ruff check kairos/ tests/
ruff format kairos/ tests/
```

## How to Contribute

- **Bug reports** — file an issue with steps to reproduce
- **Feature requests** — open an issue describing the use case
- **Pull requests** — see guidelines below

## Pull Request Guidelines

1. **Target the `dev` branch**, not `main`. All work happens on `dev`.
2. **Write tests first** (TDD). Kairos uses strict test-driven development for all modules with security boundaries.
3. **Follow the coding standards:**
   - Python 3.11+ (3.13 patterns preferred)
   - `ruff check` and `ruff format` must pass
   - `mypy kairos/` must pass (strict mode)
   - Google-style docstrings
   - 100-character max line length
4. **Conventional commits:** `feat:`, `fix:`, `test:`, `docs:`, `refactor:`
5. **One logical change per PR.** Don't bundle unrelated changes.
6. **All tests must pass.** Run `pytest` before submitting.

## Project Structure

```
kairos/           Source code (12 core modules + adapters)
tests/            Test suite (1,244+ tests)
examples/         Runnable examples
.github/          CI/CD workflows
```

## Code of Conduct

We are committed to providing a welcoming experience. Please be respectful in all interactions.

## Questions?

Open an issue on GitHub or check the [Getting Started guide](GETTING_STARTED.md).
