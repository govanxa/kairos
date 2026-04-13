"""Kairos validators — built-in validator factories and validator orchestrators."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Protocol, runtime_checkable

from kairos.enums import Severity
from kairos.exceptions import ConfigError
from kairos.schema import FieldValidationError, Schema, ValidationResult
from kairos.security import sanitize_exception

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

# A FieldValidator returns True on pass, or an error message string on failure.
FieldValidator = Callable[[Any], "bool | str"]

# Default timeout for pattern() regex matching
_DEFAULT_REGEX_TIMEOUT: float = 5.0

# Default timeout for LLMValidator LLM calls
_DEFAULT_LLM_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# Validator Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Validator(Protocol):
    """Protocol for all Kairos validator orchestrators.

    Any object implementing this protocol can be used wherever a Validator
    is expected: StructuralValidator, LLMValidator, CompositeValidator, or
    a custom implementation.
    """

    def validate(self, data: Any, schema: Schema | None = None) -> ValidationResult:
        """Run validation against data, optionally constrained by a Schema.

        Args:
            data: The data to validate (typically a dict from step output).
            schema: Optional schema contract. If None, behaviour varies by
                implementation (e.g. StructuralValidator returns valid=True).

        Returns:
            A ValidationResult with valid=True/False and a list of errors.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Built-in validator factory functions
# ---------------------------------------------------------------------------


def range_(*, min: float | int | None = None, max: float | int | None = None) -> FieldValidator:
    """Validate that a numeric value falls within an inclusive [min, max] range.

    Rejects non-numeric values and booleans (even though bool is a subclass of
    int in Python, boolean fields should use dedicated boolean validators).

    Args:
        min: Inclusive lower bound (None = no lower bound).
        max: Inclusive upper bound (None = no upper bound).

    Returns:
        A FieldValidator callable: True on pass, error string on failure.

    Example:
        >>> validator = range_(min=0, max=100)
        >>> validator(50)   # True
        >>> validator(150)  # "Value 150 exceeds maximum of 100"
    """

    def _validate(value: Any) -> bool | str:
        # Reject booleans — they subclass int but are semantically boolean.
        if isinstance(value, bool):
            return "Expected a numeric value, got bool. Use a boolean field instead."
        if not isinstance(value, (int, float)):
            return f"Expected a numeric value, got {type(value).__name__!r}."
        if min is not None and value < min:
            return f"Value {value} is below the minimum of {min}."
        if max is not None and value > max:
            return f"Value {value} exceeds the maximum of {max}."
        return True

    return _validate


# Alias: `range` shadows the Python builtin inside this module's scope, but the
# factory is named `range_` to avoid that collision. The alias lets callers use
# `v.range(min=0, max=10)` which is the more natural spelling.
range = range_  # noqa: A001


def length(*, min: int | None = None, max: int | None = None) -> FieldValidator:
    """Validate the length of a string or list falls within an inclusive [min, max] range.

    Args:
        min: Inclusive minimum length (None = no minimum).
        max: Inclusive maximum length (None = no maximum).

    Returns:
        A FieldValidator callable: True on pass, error string on failure.

    Example:
        >>> validator = length(min=1, max=10)
        >>> validator("hello")     # True
        >>> validator("")          # "String length 0 is below the minimum of 1"
    """

    def _validate(value: Any) -> bool | str:
        if not isinstance(value, (str, list)):
            return f"Expected a string or list, got {type(value).__name__!r}."
        length_val = len(value)
        kind = "string" if isinstance(value, str) else "list"
        if min is not None and length_val < min:
            return f"{kind.capitalize()} length {length_val} is below the minimum of {min}."
        if max is not None and length_val > max:
            return f"{kind.capitalize()} length {length_val} exceeds the maximum of {max}."
        return True

    return _validate


def pattern(regex: str, *, timeout: float = _DEFAULT_REGEX_TIMEOUT) -> FieldValidator:
    """Validate that a string matches a regular expression.

    The regex is pre-compiled at definition time — invalid patterns raise
    ConfigError immediately, not during validation. Regex matching runs in a
    separate thread to enforce the timeout, protecting against ReDoS attacks.

    Args:
        regex: The regular expression pattern string.
        timeout: Seconds before the match attempt is abandoned (default 5.0).
            Timed-out validation returns an error string mentioning "timed out"
            and "ReDoS".

    Returns:
        A FieldValidator callable: True on pass, error string on failure.

    Raises:
        ConfigError: If the regex pattern is invalid (at definition time).

    Example:
        >>> validator = pattern(r"^\\d+$")
        >>> validator("123")  # True
        >>> validator("abc")  # "Value does not match pattern ..."
    """
    try:
        compiled = re.compile(regex)
    except re.error as exc:
        raise ConfigError(f"Invalid regex pattern {regex!r}: {exc}") from exc

    def _validate(value: Any) -> bool | str:
        if not isinstance(value, str):
            return f"Expected a string, got {type(value).__name__!r}."

        # DESIGN NOTE: A new ThreadPoolExecutor is created per call intentionally.
        # Python cannot forcibly kill a thread — a timed-out regex running in C
        # extension code continues until it finishes. If we used a shared pool,
        # leaked threads from timed-out evaluations would accumulate indefinitely.
        # Per-call pools with shutdown(wait=False, cancel_futures=True) isolate
        # each leaked thread to its own pool, which GC can collect once the
        # thread eventually terminates.
        pool = ThreadPoolExecutor(max_workers=1)
        future: Future[re.Match[str] | None] = pool.submit(compiled.match, value)
        try:
            match = future.result(timeout=timeout)
        except (FutureTimeoutError, TimeoutError):
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            return (
                f"Pattern validation timed out after {timeout}s. "
                "Possible ReDoS vulnerability in the pattern. "
                f"Pattern: {regex!r}"
            )
        else:
            pool.shutdown(wait=False)
            if not match:
                return f"Value does not match the required pattern {regex!r}."
            return True

    return _validate


def one_of(values: list[Any]) -> FieldValidator:
    """Validate that a value is one of a fixed set of allowed values.

    Args:
        values: The allowlist. Empty list means nothing can pass.

    Returns:
        A FieldValidator callable: True on pass, error string on failure.

    Example:
        >>> validator = one_of(["red", "green", "blue"])
        >>> validator("red")    # True
        >>> validator("purple") # "Value 'purple' is not one of ..."
    """

    def _validate(value: Any) -> bool | str:
        if value not in values:
            allowed = ", ".join(repr(v) for v in values)
            return f"Value {value!r} is not one of the allowed values: [{allowed}]."
        return True

    return _validate


def not_empty() -> FieldValidator:
    """Validate that a string or list is non-empty.

    For strings, whitespace-only strings are treated as empty (stripped before
    the emptiness check). None always fails.

    Returns:
        A FieldValidator callable: True on pass, error string on failure.

    Example:
        >>> validator = not_empty()
        >>> validator("hello")  # True
        >>> validator("")       # "Value must not be empty."
        >>> validator([])       # "List must not be empty."
    """

    def _validate(value: Any) -> bool | str:
        if value is None:
            return "Value must not be None."
        if isinstance(value, str):
            if not value.strip():
                return "Value must not be empty or whitespace-only."
            return True
        if isinstance(value, list):
            if len(value) == 0:
                return "List must not be empty."
            return True
        # For other types, just verify truthiness
        return True

    return _validate


def custom(fn: Callable[[Any], Any]) -> FieldValidator:
    """Wrap an arbitrary callable as a field validator.

    The wrapped function's exceptions are caught and sanitized — only the
    exception class name is included in the error message. The raw exception
    message is never exposed (it may contain sensitive data or prompt injection
    payloads).

    Args:
        fn: Any callable that accepts a single value and returns True (pass)
            or a falsy/string (fail). Exceptions are treated as failures.

    Returns:
        A FieldValidator callable: True on pass, error string on failure.

    Example:
        >>> validator = custom(lambda x: x > 0)
        >>> validator(5)   # True
        >>> validator(-1)  # "Custom validation failed."
    """

    def _validate(value: Any) -> bool | str:
        try:
            result = fn(value)
        except Exception as exc:
            # SECURITY: sanitize_exception returns only (class_name, cleaned_message).
            # We use ONLY the class name — never the cleaned_message — to prevent
            # any path by which the exception content reaches external consumers.
            error_type, _sanitized_msg = sanitize_exception(exc)
            return f"Custom validator raised {error_type}."
        else:
            if result is True:
                return True
            if isinstance(result, str):
                return result
            # False, None, 0, or any other falsy value
            return "Custom validation failed."

    return _validate


# ---------------------------------------------------------------------------
# StructuralValidator
# ---------------------------------------------------------------------------


class StructuralValidator:
    """Validates data structurally against a Schema contract.

    Phase 1 runs schema.validate() for type/required-field checks.
    Phase 2 runs field-level validator functions only on fields that:
    - Passed type checking in Phase 1
    - Are present in the data dict
    - Are not None (for optional fields)

    This class never raises exceptions — all errors are captured as
    ValidationResult(valid=False).
    """

    def validate(self, data: Any, schema: Schema | None = None) -> ValidationResult:
        """Run structural validation against data.

        Args:
            data: The value to validate (expected dict from step output).
            schema: The schema contract to validate against. When None,
                returns ValidationResult(valid=True) immediately.

        Returns:
            ValidationResult with valid=True/False and collected errors.
        """
        try:
            return self._run(data, schema)
        except Exception as exc:
            # Belt-and-suspenders: StructuralValidator must never crash.
            # Include only the exception class name — never the message, which may
            # contain sensitive data.
            error_type, _ = sanitize_exception(exc)
            return ValidationResult(
                valid=False,
                errors=[
                    FieldValidationError(
                        field="<internal>",
                        expected="no internal error",
                        actual=f"{error_type} raised",
                        message=f"Internal validation error: {error_type}.",
                        severity=Severity.ERROR,
                    )
                ],
            )

    def _run(self, data: Any, schema: Schema | None) -> ValidationResult:
        """Internal implementation — may raise; caller wraps in try/except."""
        if schema is None:
            return ValidationResult(valid=True)

        # Phase 1: structural type/required checks via Schema.validate()
        phase1 = schema.validate(data)

        # Collect the field names that failed type checking so we can skip
        # their field validators in Phase 2.
        type_failed_fields: set[str] = {e.field for e in phase1.errors}

        all_errors = list(phase1.errors)

        # Phase 2: field-level validators (only on type-passing fields)
        if isinstance(data, dict):
            for fd in schema.field_definitions:
                # Skip fields that failed type checking
                if fd.name in type_failed_fields:
                    continue

                # Skip absent optional fields
                if fd.name not in data:
                    continue

                # Skip optional fields present as None
                value = data[fd.name]
                if value is None and not fd.required:
                    continue

                # Run each validator for this field
                for validator_fn in fd.validators:
                    try:
                        result = validator_fn(value)
                    except Exception as exc:
                        # SECURITY: sanitize_exception ensures the exception message
                        # (which may contain sensitive data) is never exposed.
                        error_type, _ = sanitize_exception(exc)
                        result = f"Validator raised {error_type}."

                    if result is not True:
                        msg = result if isinstance(result, str) else "Constraint violated."
                        all_errors.append(
                            FieldValidationError(
                                field=fd.name,
                                expected="passes constraint",
                                actual="constraint violation",
                                message=msg,
                                severity=Severity.ERROR,
                            )
                        )

        return ValidationResult(
            valid=len(all_errors) == 0,
            errors=all_errors,
        )


# ---------------------------------------------------------------------------
# LLMValidator
# ---------------------------------------------------------------------------

# Pre-compiled patterns for parsing LLM response
_RESULT_RE = re.compile(r"result\s*:\s*(pass|fail)", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"confidence\s*:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)

_LLM_PROMPT_TEMPLATE = """\
You are a validation assistant. Evaluate the following output against these criteria:

Criteria: {criteria}

Output to validate:
{data}

Respond with:
- RESULT: PASS or RESULT: FAIL
- CONFIDENCE: <score between 0.0 and 1.0>
- Brief explanation
"""


def _make_llm_result(
    valid: bool,
    errors: list[FieldValidationError],
    confidence: float,
    raw_response: str,
) -> ValidationResult:
    """Build a ValidationResult with LLM-specific metadata.

    Args:
        valid: Whether validation passed.
        errors: Field-level validation errors.
        confidence: Confidence score parsed from the LLM response.
        raw_response: The raw LLM response string.

    Returns:
        ValidationResult with metadata["confidence"] and metadata["raw_response"].
    """
    return ValidationResult(
        valid=valid,
        errors=errors,
        metadata={"confidence": confidence, "raw_response": raw_response},
    )


class LLMValidator:
    """Uses an LLM to assess semantic quality of step output.

    Accepts any callable that takes a prompt string and returns a string
    response. This keeps the validator model-agnostic — it does not depend
    on any model adapter.

    Attributes:
        criteria: Natural language description of what makes output acceptable.
        llm_fn: Callable[str, str] — takes prompt, returns response.
        threshold: Minimum confidence for a PASS result (0.0–1.0).
        timeout: Seconds before the LLM call is abandoned (default 30.0).
    """

    def __init__(
        self,
        criteria: str,
        llm_fn: Callable[[str], str],
        threshold: float = 0.8,
        timeout: float = _DEFAULT_LLM_TIMEOUT,
    ) -> None:
        """Initialise LLMValidator with validation parameters.

        Args:
            criteria: Non-empty natural language description of what makes
                output acceptable.
            llm_fn: Any callable accepting a prompt string and returning a
                string response. Must be callable.
            threshold: Minimum confidence to pass (0.0–1.0 inclusive).
            timeout: Seconds before the LLM call is abandoned.

        Raises:
            ConfigError: If criteria is empty, llm_fn is not callable, or
                threshold is outside [0.0, 1.0].
        """
        if not criteria or not criteria.strip():
            raise ConfigError("LLMValidator: criteria must be a non-empty string.")
        if not callable(llm_fn):
            raise ConfigError("LLMValidator: llm_fn must be callable.")
        if not (0.0 <= threshold <= 1.0):
            raise ConfigError(
                f"LLMValidator: threshold must be between 0.0 and 1.0, got {threshold}."
            )
        self.criteria = criteria
        self.llm_fn = llm_fn
        self.threshold = threshold
        self.timeout = timeout

    def validate(self, data: Any, schema: Schema | None = None) -> ValidationResult:
        """Run semantic validation using the LLM.

        Serializes data via json.dumps(data, default=str), builds a prompt,
        calls llm_fn in a thread with a timeout, parses the response for
        RESULT: PASS|FAIL and CONFIDENCE: float.

        The returned ValidationResult has a .metadata dict attached with
        keys "confidence" (float) and "raw_response" (str).

        Args:
            data: The step output to validate (any JSON-serializable value).
            schema: Ignored by LLMValidator — present for Protocol compliance.

        Returns:
            ValidationResult(valid=True) when the LLM returns PASS with
            confidence >= threshold. ValidationResult(valid=False) otherwise.
            Always includes .metadata with confidence and raw_response.
        """
        # Serialize data safely — default=str handles most non-serializable values.
        # SECURITY: if json.dumps still fails (e.g. circular references), fall back to
        # a safe placeholder — never str(data) which could expose object representations.
        try:
            serialized = json.dumps(data, default=str)
        except Exception:
            serialized = "<non-serializable data>"

        prompt = _LLM_PROMPT_TEMPLATE.format(
            criteria=self.criteria,
            data=serialized,
        )

        # Run LLM call in a thread to enforce timeout.
        # DESIGN NOTE: Per-call pool is intentional — see the same comment in
        # pattern(). A timed-out llm_fn thread cannot be killed; per-call pools
        # prevent leaked threads from accumulating in a shared pool.
        pool = ThreadPoolExecutor(max_workers=1)
        future: Future[str] = pool.submit(self.llm_fn, prompt)

        raw_response: str = ""
        timed_out = False
        llm_error_class: str | None = None
        try:
            raw_response = future.result(timeout=self.timeout)
        except (FutureTimeoutError, TimeoutError):
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            timed_out = True
        except Exception as exc:
            # SECURITY: catch ALL exceptions from llm_fn (ConnectionError,
            # AuthenticationError, etc.) to prevent unsanitized exception messages
            # — which may contain API keys or credentials — from propagating.
            # Only the exception class name is preserved; the message is discarded.
            pool.shutdown(wait=False, cancel_futures=True)
            llm_error_class, _ = sanitize_exception(exc)
        else:
            pool.shutdown(wait=False)

        if timed_out:
            return _make_llm_result(
                valid=False,
                errors=[
                    FieldValidationError(
                        field="<llm_validation>",
                        expected="LLM response within timeout",
                        actual="timeout",
                        message=f"LLM validation timed out after {self.timeout}s.",
                        severity=Severity.ERROR,
                    )
                ],
                confidence=0.0,
                raw_response="",
            )

        if llm_error_class is not None:
            return _make_llm_result(
                valid=False,
                errors=[
                    FieldValidationError(
                        field="<llm_validation>",
                        expected="successful LLM response",
                        actual=f"{llm_error_class} raised",
                        message=f"LLM call raised {llm_error_class}.",
                        severity=Severity.ERROR,
                    )
                ],
                confidence=0.0,
                raw_response="",
            )

        # Parse response — both patterns are case-insensitive
        result_match = _RESULT_RE.search(raw_response)
        confidence_match = _CONFIDENCE_RE.search(raw_response)

        confidence: float = 0.0
        passed = False

        if result_match:
            passed = result_match.group(1).upper() == "PASS"

        if confidence_match:
            try:
                confidence = float(confidence_match.group(1))
            except ValueError:
                confidence = 0.0

        if not result_match:
            # Unparseable response — treat as FAIL with zero confidence
            passed = False
            confidence = 0.0

        valid = passed and confidence >= self.threshold

        errors: list[FieldValidationError] = []
        if not valid:
            errors.append(
                FieldValidationError(
                    field="<llm_validation>",
                    expected=f"PASS with confidence >= {self.threshold}",
                    actual=f"{'PASS' if passed else 'FAIL'} with confidence {confidence}",
                    message=(
                        f"LLM validation failed: "
                        f"{'PASS' if passed else 'FAIL'} with confidence {confidence} "
                        f"(threshold {self.threshold})."
                    ),
                    severity=Severity.ERROR,
                )
            )

        return _make_llm_result(
            valid=valid,
            errors=errors,
            confidence=confidence,
            raw_response=raw_response,
        )


# ---------------------------------------------------------------------------
# CompositeValidator
# ---------------------------------------------------------------------------


class CompositeValidator:
    """Chains multiple validators in sequence, short-circuiting on first failure.

    Validators run in the order provided. The first validator that returns
    ValidationResult(valid=False) stops the chain. All errors from that
    validator are included in the final result.

    Attributes:
        validators: The ordered list of Validator implementations to run.
    """

    def __init__(self, validators: list[Validator]) -> None:
        """Initialise CompositeValidator.

        Args:
            validators: Non-empty ordered list of validators to chain.

        Raises:
            ConfigError: If validators is empty.
        """
        if not validators:
            raise ConfigError("CompositeValidator: validators list must not be empty.")
        self.validators = validators

    def validate(self, data: Any, schema: Schema | None = None) -> ValidationResult:
        """Run validators in order, stopping at the first failure.

        Args:
            data: The data to validate.
            schema: Optional schema — passed through to each validator.

        Returns:
            ValidationResult from the first failing validator, or
            ValidationResult(valid=True) if all validators pass.
        """
        all_errors: list[FieldValidationError] = []
        for v in self.validators:
            result = v.validate(data, schema)
            if not result.valid:
                return result
            all_errors.extend(result.errors)

        return ValidationResult(valid=True, errors=all_errors)
