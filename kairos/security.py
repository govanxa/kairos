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
import unicodedata
from dataclasses import dataclass
from typing import cast

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
            typed_item = cast(dict[str, object], item)
            result.append(redact_sensitive(typed_item, sensitive_patterns=patterns))
        elif isinstance(item, list):
            result.append(_redact_list(cast(list[object], item), patterns))
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
            typed_val = cast(dict[str, object], value)
            result[key] = redact_sensitive(typed_val, sensitive_patterns=patterns)
        elif isinstance(value, list):
            result[key] = _redact_list(cast(list[object], value), patterns)
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


# ---------------------------------------------------------------------------
# Untrusted-text gate primitives (B1)
# Promoted from the Evidence Engine A1 concept spike.
# Reference: docs/projects/evidence-engine/blueprints/blueprint-b1-untrusted-text.md
# ---------------------------------------------------------------------------

# Canonical injection-flag vocabulary.  Values emitted into SanitizedText.flags;
# NEVER contain raw matched text (03 §2 injection_flags rule).
FLAG_ROLE_MARKER: str = "role_marker"
FLAG_TEMPLATE_TOKEN: str = "template_token"  # noqa: S105
FLAG_IMPERATIVE: str = "imperative_override"
FLAG_TOOL_CALL: str = "tool_call_syntax"

INJECTION_FLAGS: frozenset[str] = frozenset(
    {FLAG_ROLE_MARKER, FLAG_TEMPLATE_TOKEN, FLAG_IMPERATIVE, FLAG_TOOL_CALL}
)

# Replacement token — visible, inert, never a valid instruction keyword.
_NEUTRALIZED = "[NEUTRALIZED]"

# ---------------------------------------------------------------------------
# Pre-compiled patterns (module-level — T9, never re-compiled per call)
# ---------------------------------------------------------------------------

# Zero-width and invisible Unicode characters — explicit codepoints so the
# class is reviewable by eye: ZWSP, ZWNJ, ZWJ, word joiner, BOM/ZWNBSP, soft hyphen.
_ZERO_WIDTH_CODEPOINTS: tuple[int, ...] = (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD)
_ZERO_WIDTH_RE: re.Pattern[str] = re.compile("[" + "".join(map(chr, _ZERO_WIDTH_CODEPOINTS)) + "]")

# Role markers: "system:", "assistant:", "user:", "human:", "ai:" at line start
# or as bracketed tags; case-insensitive.
_ROLE_MARKER_RES: list[re.Pattern[str]] = [
    re.compile(r"^\s*(system|assistant|user|human|ai)\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\[(system|assistant|user|human|ai)\]", re.IGNORECASE),
]

# Chat template tokens — bounded quantifiers (T9 — prevents catastrophic backtracking).
_TEMPLATE_TOKEN_RES: list[re.Pattern[str]] = [
    re.compile(r"<\|im_start\|>|<\|im_end\|>", re.IGNORECASE),
    re.compile(r"<\|[^|\n]{0,30}\|>"),  # bounded: max 30 chars between pipes
    re.compile(r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>", re.IGNORECASE),
    re.compile(r"<s>|</s>", re.IGNORECASE),  # LLaMA sentence boundaries
]

# Imperative injection phrases — common prompt injection patterns.
# Use non-greedy, bounded quantifiers throughout (T9).
_IMPERATIVE_RES: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(?:your|the|all|any)\s+\S{0,30}", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|another)\b", re.IGNORECASE),
    re.compile(r"forget\s+(?:everything|what)\s+you\s+(?:were|have)", re.IGNORECASE),
    re.compile(r"your\s+new\s+(?:instructions?|role|persona|task)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:if|an?\s+\S{1,30}|though)", re.IGNORECASE),
    re.compile(
        r"(?:reveal|print|show|output)\s+(?:your|the)\s+(?:system|instructions?|prompt)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:pretend|imagine)\s+you\s+(?:are|were|have)", re.IGNORECASE),
    re.compile(r"jailbreak|DAN\s+mode|developer\s+mode", re.IGNORECASE),
]

# Tool-call syntax shapes — JSON function-call patterns (bounded).
_TOOL_CALL_RES: list[re.Pattern[str]] = [
    re.compile(r'"function"\s*:\s*\{[^}]{0,200}\}', re.IGNORECASE),
    re.compile(r'"name"\s*:\s*"[^"]{0,60}"\s*,\s*"arguments"\s*:', re.IGNORECASE),
    re.compile(r"<tool_call>|</tool_call>|<tool_result>|</tool_result>", re.IGNORECASE),
    re.compile(r"\btools?\b\s*:\s*\[", re.IGNORECASE),
]

# Instruction-critical homoglyph map: Cyrillic/Greek lookalikes → ASCII.
# Applied AFTER NFKC (which handles many standard compatibility mappings) to
# catch residual confusables used to obfuscate keywords.
_HOMOGLYPH_MAP: dict[str, str] = {
    # Cyrillic lowercase
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "r",
    "с": "s",
    "у": "u",
    "х": "x",
    "і": "i",  # Ukrainian/Belarusian dotted i
    # Cyrillic capitals
    "А": "A",
    "Е": "E",
    "О": "O",
    "Р": "R",
    "С": "S",
    "Т": "T",
    "Х": "X",
    # Greek lowercase
    "α": "a",
    "ε": "e",
    "ο": "o",  # omicron
    "ν": "v",  # nu — resembles v
    "τ": "t",  # tau
    "υ": "u",  # upsilon
    # Greek capitals
    "Α": "A",
    "Ε": "E",
    "Ο": "O",
}

# Translation table for fast str.translate().
_HOMOGLYPH_TABLE: dict[int, str] = {ord(k): v for k, v in _HOMOGLYPH_MAP.items()}

# Minimum non-neutralized content (chars) to consider a document salvageable.
_MIN_SALVAGEABLE_CHARS = 30
# Neutralization-count threshold for predominantly-instructional rejection.
_PREDOMINANTLY_INSTRUCTIONAL_THRESHOLD = 3


# ---------------------------------------------------------------------------
# SanitizedText — immutable output of sanitize_untrusted_text
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SanitizedText:
    """Immutable result of sanitizing an untrusted string.

    Attributes:
        text: Cleaned, neutralized, credential-scrubbed text. Safe to store
            in state (never raw web content).
        flags: Sorted, de-duped canonical flag names that fired during
            sanitization. Values are always a subset of ``INJECTION_FLAGS``;
            raw matched text is NEVER placed here.
        truncated: ``True`` if the text was capped to ``max_len``.
    """

    text: str
    flags: list[str]
    truncated: bool


# ---------------------------------------------------------------------------
# Untrusted-text public API
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC-normalize, strip zero-width chars, and homoglyph-fold an untrusted string.

    Deterministic. Applied before pattern matching so obfuscated markers are
    caught by the ``neutralize`` step.

    Args:
        text: Raw untrusted string.

    Returns:
        Normalized string safe to apply regex patterns to.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = text.translate(_HOMOGLYPH_TABLE)
    return text


def neutralize(text: str) -> tuple[str, list[str]]:
    """Defang role markers, template tokens, imperatives, and tool-call syntax.

    Applies all pattern groups in order. Matched regions are replaced with
    ``[NEUTRALIZED]``. Returns the defanged text and the set of canonical flag
    names that fired. Raw matched fragments are NEVER placed in the flags list.

    Args:
        text: Pre-normalized untrusted text.

    Returns:
        A 2-tuple ``(defanged_text, sorted_unique_flag_names)``.
    """
    flags: set[str] = set()

    for pat in _ROLE_MARKER_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_ROLE_MARKER)
            text = new_text

    for pat in _TEMPLATE_TOKEN_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_TEMPLATE_TOKEN)
            text = new_text

    for pat in _IMPERATIVE_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_IMPERATIVE)
            text = new_text

    for pat in _TOOL_CALL_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_TOOL_CALL)
            text = new_text

    return text, sorted(flags)


def scrub_credentials(text: str) -> str:
    """Redact credential patterns from free text (T7).

    Thin public wrapper over the shared ``_redact_credentials`` helper so that
    ``_CREDENTIAL_PATTERNS`` is the single canonical set used by both
    ``sanitize_exception`` (exception messages) and this function (scraped
    free text).  Applied after neutralization so credentials embedded inside
    injection phrases are still caught.

    Args:
        text: Text that may contain API keys, tokens, or passwords.

    Returns:
        Text with credential values replaced by ``[REDACTED_KEY]`` or similar.
    """
    return _redact_credentials(text)


def sanitize_untrusted_text(text: str, *, max_len: int = 2000) -> SanitizedText:
    """Full sanitization pipeline for a single untrusted string field.

    Pipeline: ``normalize`` → ``neutralize`` → ``scrub_credentials`` → cap to
    ``max_len``.  The single entry point that ``content_gate`` calls per string
    field.

    Args:
        text: Raw untrusted string (from web page content, title, URL path, etc.).
        max_len: Maximum output length in characters. Default 2000.

    Returns:
        ``SanitizedText`` with cleaned text, triggered flags, and truncation flag.
    """
    if not text:
        return SanitizedText(text="", flags=[], truncated=False)

    normed = normalize(text)
    neutralized, flags = neutralize(normed)
    scrubbed = scrub_credentials(neutralized)

    truncated = len(scrubbed) > max_len
    final_text = scrubbed[:max_len] if truncated else scrubbed

    return SanitizedText(text=final_text, flags=flags, truncated=truncated)


def is_predominantly_instructional(sanitized: SanitizedText, *, raw_len: int) -> bool:
    """Structural rejection signal: is the content mostly injection payloads?

    Returns ``True`` when the document should be rejected as unsalvageable:

    - Empty (or whitespace-only) after cleaning.
    - So small after neutralization relative to original that real content is gone.
    - Contains enough neutralization markers to indicate the page is primarily
      an injection payload.

    Args:
        sanitized: Output of ``sanitize_untrusted_text``.
        raw_len: Character length of the original (pre-sanitization) text.

    Returns:
        ``True`` if the document is predominantly instructional.
    """
    stripped = sanitized.text.strip()

    if not stripped:
        return True

    neutralized_count = sanitized.text.count(_NEUTRALIZED)
    if neutralized_count >= _PREDOMINANTLY_INSTRUCTIONAL_THRESHOLD:
        return True

    return raw_len > 0 and len(stripped) < _MIN_SALVAGEABLE_CHARS
