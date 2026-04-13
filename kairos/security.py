"""Kairos security — sanitization utilities used by all downstream modules.

These functions form the security boundary of the SDK:
- sanitize_exception: strips credentials and file paths from exception messages
- sanitize_retry_context: produces injection-safe retry metadata (never raw output)
- redact_sensitive: redacts sensitive keys from state dicts before logging/export
- sanitize_path: enforces safe filenames and prevents path traversal
"""

from __future__ import annotations

import fnmatch
import os
import re

from kairos.exceptions import ConfigError, SecurityError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SANITIZED_LENGTH = 500

DEFAULT_SENSITIVE_PATTERNS: list[str] = [
    "*api_key*",
    "*apikey*",
    "*secret*",
    "*password*",
    "*passwd*",
    "*token*",
    "*credential*",
    "*auth*",
    "*bearer*",
    "*private_key*",
    "*access_key*",
]

# Compiled credential patterns applied by sanitize_exception.
# Order matters: more specific patterns should come before broader ones.
_CREDENTIAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[a-zA-Z0-9_-]+"), "[REDACTED_KEY]"),
    (re.compile(r"key-[a-zA-Z0-9_-]+"), "[REDACTED_KEY]"),
    # Authorization header (must come before bare Bearer pattern to match full value)
    (re.compile(r"Authorization:\s*\S+(?:\s+\S+)?"), "Authorization: [REDACTED]"),
    (re.compile(r"Bearer\s+\S+"), "Bearer [REDACTED]"),
    (re.compile(r"token=[^\s&,;]+"), "token=[REDACTED]"),
    (re.compile(r"password=[^\s&,;]+"), "password=[REDACTED]"),
    (re.compile(r"passwd=[^\s&,;]+"), "passwd=[REDACTED]"),
    (re.compile(r"secret=[^\s&,;]+"), "secret=[REDACTED]"),
    (re.compile(r"api_key=[^\s&,;]+"), "api_key=[REDACTED]"),
    (re.compile(r"apikey=[^\s&,;]+"), "apikey=[REDACTED]"),
]

# Matches Unix absolute paths like /foo/bar/baz.py
_UNIX_PATH_RE = re.compile(r"(/(?:[^\s/]+/)+)([^\s/]+)")

# Matches relative paths like ../../config/secrets.yaml or ./data/config.json
# Requires at least one directory component before the filename.
_RELATIVE_PATH_RE = re.compile(r"(?:\.{1,2}/(?:[^\s/]+/)*)([^\s/]+)")

# Matches Windows absolute paths like C:\foo\bar\baz.py or \\server\share\file
_WIN_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)(?:[^\s\\]+\\)+([^\s\\]+)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _redact_credentials(message: str) -> str:
    """Apply all credential redaction patterns to *message*.

    Args:
        message: Raw exception message text.

    Returns:
        Message with credential patterns replaced by safe placeholders.
    """
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


def _strip_file_paths(message: str) -> str:
    """Replace full file paths with their basename only.

    Handles both Unix-style paths (``/a/b/file.py``) and Windows-style paths
    (``C:\\a\\b\\file.py``).  The directory component is removed; only the
    final filename segment is kept.

    Args:
        message: Text that may contain file system paths.

    Returns:
        Message with directory components stripped, filenames preserved.
    """
    # Strip Windows paths first (more specific) to avoid partial Unix matches.
    message = _WIN_PATH_RE.sub(r"\1", message)
    # Strip absolute Unix paths before relative paths (more specific).
    message = _UNIX_PATH_RE.sub(r"\2", message)
    # Strip relative paths (../../foo/bar.py → bar.py, ./data/x.json → x.json).
    message = _RELATIVE_PATH_RE.sub(r"\1", message)
    return message


_VALIDATION_FIELD_RE = re.compile(r"[^a-z0-9_.\[\]-]")
_VALIDATION_TYPE_RE = re.compile(r"[^a-z0-9_|\[\],]")
# Field names can be longer (e.g., nested paths like "result.items[0].score").
_VALIDATION_FIELD_MAX_LEN = 100
# Type names are short Python identifiers; strict limit prevents sentence injection.
_VALIDATION_TYPE_MAX_LEN = 40


def sanitize_validation_token(value: str) -> str:
    """Sanitize a validation field name for safe inclusion in retry context.

    Normalizes to lowercase first, then allows only ``[a-z0-9_.[\\]-]`` —
    characters expected in Python attribute paths and list-index notation.
    All other characters are replaced with ``_``.  Result is truncated to
    100 characters.

    Lowercasing is intentional security hardening: it eliminates injection
    payloads that rely on uppercase keywords (e.g., "SYSTEM:", "IGNORE") while
    preserving all valid Python field name characters.

    Args:
        value: Raw field name string from a validation error entry.

    Returns:
        Sanitized field name safe to include in retry context metadata.
    """
    return _VALIDATION_FIELD_RE.sub("_", value.lower())[:_VALIDATION_FIELD_MAX_LEN]


def _sanitize_type_token(value: str) -> str:
    """Sanitize a type name string for safe inclusion in retry context.

    Normalizes to lowercase first, then allows only ``[a-z0-9_|[\\],]`` —
    characters expected in Python type names and union notation.  All other
    characters are replaced with ``_``.  Result is truncated to 100 characters.

    Lowercasing is intentional security hardening: it prevents injection
    payloads using uppercase instruction keywords while preserving all valid
    Python type name characters.

    Args:
        value: Raw expected/actual type name from a validation error entry.

    Returns:
        Sanitized type name safe to include in retry context metadata.
    """
    return _VALIDATION_TYPE_RE.sub("_", value.lower())[:_VALIDATION_TYPE_MAX_LEN]


def _redact_list(items: list[object], patterns: list[str]) -> list[object]:
    """Recursively redact dicts (and nested lists) within *items*.

    Args:
        items: A list that may contain dicts, nested lists, or scalars.
        patterns: Sensitive-key glob patterns forwarded to ``redact_sensitive``.

    Returns:
        A new list with dict elements redacted and list elements recursed into.
    """
    result: list[object] = []
    for item in items:
        if isinstance(item, dict):
            result.append(redact_sensitive(item, sensitive_patterns=patterns))
        elif isinstance(item, list):
            result.append(_redact_list(item, patterns))
        else:
            result.append(item)
    return result


def _is_sensitive_key(key: str, patterns: list[str]) -> bool:
    """Return True if *key* matches any pattern in *patterns* (case-insensitive).

    Args:
        key: State dict key to test.
        patterns: List of ``fnmatch``-style glob patterns.

    Returns:
        True when the key matches at least one pattern.
    """
    lower_key = key.lower()
    return any(fnmatch.fnmatch(lower_key, pattern.lower()) for pattern in patterns)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_exception(exc: Exception) -> tuple[str, str]:
    """Sanitize an exception into a safe (error_type, message) pair.

    Performs the following in order:
    1. Extracts the exception class name and message via ``str(exc)``.
    2. Redacts credential patterns (API keys, tokens, passwords).
    3. Strips file paths to filenames only.
    4. Truncates the message to ``_MAX_SANITIZED_LENGTH`` characters.

    The returned tuple is always JSON-serializable (both elements are ``str``).

    Args:
        exc: Any exception instance.

    Returns:
        A 2-tuple ``(error_type, sanitized_message)`` where *error_type* is
        the exception class name and *sanitized_message* is the cleaned text.
    """
    error_type = type(exc).__name__
    raw_message = str(exc)

    if not raw_message:
        return error_type, ""

    message = _redact_credentials(raw_message)
    message = _strip_file_paths(message)
    message = message[:_MAX_SANITIZED_LENGTH]

    return error_type, message


def sanitize_retry_context(
    step_output: object,
    exception: Exception | None,
    attempt: int,
    failure_type: str,
    validation_errors: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Produce injection-safe retry context metadata.

    CRITICAL SECURITY FUNCTION.  The ``step_output`` and ``exception``
    arguments exist *only* to derive structural metadata (class names and
    field names from ``validation_errors``).  Their string content is NEVER
    included in the returned dict — doing so would risk prompt injection.

    Args:
        step_output: The raw step output (used only for type introspection;
            its content is discarded).
        exception: The exception that caused the failure (used only for the
            class name; its message is discarded).
        attempt: The 1-based attempt number for this retry.
        failure_type: Either ``"execution"`` or ``"validation"``.
        validation_errors: Optional list of dicts with keys ``"field"``,
            ``"expected"``, and ``"actual"`` (type name strings only).

    Returns:
        A dict containing only structured metadata safe to pass back to an
        LLM as retry guidance.

    Raises:
        ConfigError: When *failure_type* is not ``"execution"`` or
            ``"validation"``.
    """
    match failure_type:
        case "execution":
            error_class = type(exception).__name__ if exception is not None else "Unknown"
            return {
                "attempt": attempt,
                "error_type": "execution",
                "error_class": error_class,
                "guidance": ("Step execution failed. Review the action logic."),
            }

        case "validation":
            errors = validation_errors or []
            failed_fields: list[str] = [sanitize_validation_token(e["field"]) for e in errors]
            # Use the sanitized field name as the key to avoid injection via key names.
            expected_types: dict[str, str] = {
                sanitize_validation_token(e["field"]): _sanitize_type_token(e["expected"])
                for e in errors
            }
            actual_types: dict[str, str] = {
                sanitize_validation_token(e["field"]): _sanitize_type_token(e["actual"])
                for e in errors
            }
            return {
                "attempt": attempt,
                "error_type": "validation",
                "failed_fields": failed_fields,
                "expected_types": expected_types,
                "actual_types": actual_types,
                "guidance": (
                    "Output failed structural validation. "
                    "Ensure all required fields are present with correct types."
                ),
            }

        case _:
            raise ConfigError(
                f"Unsupported failure_type: {failure_type!r}. Expected 'execution' or 'validation'."
            )


def redact_sensitive(
    data: dict[str, object],
    sensitive_patterns: list[str] | None = None,
) -> dict[str, object]:
    """Return a copy of *data* with sensitive values replaced by ``[REDACTED]``.

    Key matching uses ``fnmatch`` glob patterns and is case-insensitive.
    Nested dicts are recursively redacted.  The original ``data`` dict is
    never modified.

    Args:
        data: The dict to sanitize.
        sensitive_patterns: Glob patterns to match against dict keys.
            Passing ``None`` applies ``DEFAULT_SENSITIVE_PATTERNS``.
            Passing ``[]`` redacts nothing.

    Returns:
        A new dict with sensitive values replaced by the string
        ``"[REDACTED]"``.
    """
    patterns = DEFAULT_SENSITIVE_PATTERNS if sensitive_patterns is None else sensitive_patterns

    result: dict[str, object] = {}
    for key, value in data.items():
        if _is_sensitive_key(key, patterns):
            result[key] = "[REDACTED]"
        elif isinstance(value, dict):
            result[key] = redact_sensitive(value, sensitive_patterns=patterns)
        elif isinstance(value, list):
            result[key] = _redact_list(value, patterns)
        else:
            result[key] = value
    return result


def sanitize_path(name: str, base_dir: str | None = None) -> str:
    """Sanitize a file/directory name and optionally verify it stays within *base_dir*.

    Characters outside ``[a-zA-Z0-9_-]`` are replaced with underscores.
    If the result is empty, a ``SecurityError`` is raised.

    When *base_dir* is provided the sanitized name is joined to it, the
    combined path is canonicalized via ``os.path.realpath()``, and verified
    to reside within the canonicalized *base_dir*.  Path traversal attempts
    (``..``, absolute paths that escape the base) raise ``SecurityError``.

    Args:
        name: The raw name to sanitize (workflow name, run ID, step ID, etc.).
        base_dir: Optional directory that the resulting path must remain within.

    Returns:
        The sanitized name (without *base_dir*) or the full canonicalized path
        (with *base_dir*).

    Raises:
        SecurityError: When the sanitized name is empty or when the
            canonicalized path escapes *base_dir*.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)

    if not sanitized:
        raise SecurityError(
            "Path sanitization produced an empty name. "
            "The input contained only disallowed characters."
        )

    if base_dir is None:
        return sanitized

    canonical_base = os.path.realpath(base_dir)
    full_path = os.path.realpath(os.path.join(canonical_base, sanitized))

    if not full_path.startswith(canonical_base + os.sep) and full_path != canonical_base:
        # Defense-in-depth backstop: sanitization should prevent this, but we
        # verify anyway in case of future changes to the sanitization logic.
        raise SecurityError(  # pragma: no cover
            f"Sanitized path escapes the configured base directory. "
            f"Attempted traversal detected for name: {name!r}"
        )

    return full_path
