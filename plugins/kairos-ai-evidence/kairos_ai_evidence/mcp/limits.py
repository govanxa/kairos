"""MCP server input boundary limits (D3).

Pure validation helpers for the two MCP tool entry points. Enforced BEFORE any
pipeline work runs (Decision 4). Every violation raises ``InputLimitError``
with a fixed structural message (field name + limit) — the offending value is
NEVER included in the message, since it may carry a prompt-injection payload
or other untrusted content (T6/T8).

No third-party imports — stdlib only, so this module is importable and fully
unit-testable without the optional ``mcp`` SDK.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from datetime import time as _dt_time
from typing import Any

# ---------------------------------------------------------------------------
# Boundary constants (Decision 4 / 7)
# ---------------------------------------------------------------------------

MAX_DOCUMENTS: int = 50
MAX_CLAIMS: int = 20
MAX_CLAIM_LEN: int = 500
MAX_QUERY_LEN: int = 1000
MAX_RESULTS_CAP: int = 10

# SEV-001 defense-in-depth (Advisory A1): a total combined-content-size cap
# across all documents, checked BEFORE the pipeline runs. Individually-sized
# documents can each pass MAX_DOCUMENTS/per-field caps yet still add up to an
# unreasonable memory/CPU cost once they reach the content gate and the
# workflow's JSON-serializing state store. 5 MB is generous for genuine
# retrieved-web-page text while blocking pathological payloads.
MAX_TOTAL_INPUT_BYTES: int = 5 * 1024 * 1024

_MIN_RESULTS: int = 1
_DEFAULT_MAX_RESULTS: int = 5

# C3 SEV-001 pattern: re.fullmatch (never re.match) so trailing/tail bytes
# after a syntactically valid date cannot slip through.
_AS_OF_RE: re.Pattern[str] = re.compile(r"\d{4}-\d{2}-\d{2}")


class InputLimitError(ValueError):
    """Raised on a boundary-limit violation at the MCP tool surface.

    The message is ALWAYS a fixed structural string (field name + limit) —
    never the offending content. Untrusted caller input (documents, claims,
    queries) may carry prompt-injection payloads; those payloads must never
    be echoed back across the wire.
    """


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_query(query: object) -> str:
    """Validate the ``query`` argument shared by both MCP tools.

    Args:
        query: Caller-supplied value, expected to be a non-empty ``str``.

    Returns:
        The validated query string, unmodified.

    Raises:
        InputLimitError: If ``query`` is not a non-empty string within
            ``MAX_QUERY_LEN`` characters. Message never echoes the value.
    """
    if not isinstance(query, str) or not query.strip():
        raise InputLimitError("query must be a non-empty string")
    if len(query) > MAX_QUERY_LEN:
        raise InputLimitError(f"query too long: max {MAX_QUERY_LEN} characters")
    return query


def validate_claims(claims: object) -> list[str]:
    """Validate the ``claims`` argument.

    Args:
        claims: Caller-supplied value, expected to be a non-empty list of
            non-empty strings, each at most ``MAX_CLAIM_LEN`` characters.

    Returns:
        The validated list of claim strings.

    Raises:
        InputLimitError: On any structural or boundary violation. Message
            never echoes the offending claim text.
    """
    if not isinstance(claims, list):
        raise InputLimitError("claims must be a list of strings")
    if len(claims) > MAX_CLAIMS:
        raise InputLimitError(f"too many claims: max {MAX_CLAIMS}")
    if not claims:
        raise InputLimitError("claims must be a non-empty list")

    validated: list[str] = []
    for claim in claims:
        if not isinstance(claim, str) or not claim.strip():
            raise InputLimitError("each claim must be a non-empty string")
        if len(claim) > MAX_CLAIM_LEN:
            raise InputLimitError(f"claim too long: max {MAX_CLAIM_LEN} characters")
        validated.append(claim)
    return validated


def validate_documents(documents: object) -> list[dict[str, Any]]:
    """Validate the ``documents`` argument for ``evaluate_evidence``.

    Args:
        documents: Caller-supplied value, expected to be a list of dicts
            (document contents are NOT inspected here — the content_gate,
            C2, is the sole content trust boundary).

    Returns:
        The validated list of document dicts.

    Raises:
        InputLimitError: If ``documents`` is not a list, exceeds
            ``MAX_DOCUMENTS``, or contains a non-dict item.
    """
    if not isinstance(documents, list):
        raise InputLimitError("documents must be a list")
    if len(documents) > MAX_DOCUMENTS:
        raise InputLimitError(f"too many documents: max {MAX_DOCUMENTS}")

    validated: list[dict[str, Any]] = []
    for doc in documents:
        if not isinstance(doc, dict):
            raise InputLimitError("each document must be an object")
        validated.append(doc)
    return validated


def validate_total_size(documents: list[dict[str, Any]]) -> None:
    """Enforce a total combined document-content size cap (Advisory A1).

    Defense-in-depth against a caller or retriever supplying many documents
    that each individually pass ``MAX_DOCUMENTS``/per-field caps but together
    would force an expensive content-gate pass (sanitization + state
    serialization) over an unreasonable amount of text. Sums the UTF-8 byte
    length of every string value in every document dict.

    Args:
        documents: Already structurally-validated document dicts (i.e. the
            return value of ``validate_documents`` or
            ``normalize_retrieved_documents``).

    Raises:
        InputLimitError: If the combined size exceeds ``MAX_TOTAL_INPUT_BYTES``.
            The message is a fixed structural string — it never echoes any
            document content.
    """
    total = 0
    for doc in documents:
        if not isinstance(doc, dict):  # pragma: no cover - callers always pass validated dicts
            continue
        for value in doc.values():
            if isinstance(value, str):
                total += len(value.encode("utf-8"))
        if total > MAX_TOTAL_INPUT_BYTES:
            raise InputLimitError(
                f"total document content size exceeds {MAX_TOTAL_INPUT_BYTES} bytes"
            )


def validate_as_of(as_of: object) -> str | None:
    """Validate the optional ``as_of`` argument for ``evaluate_evidence``.

    Uses the C3 ``resolve_as_of`` SEV-001 pattern: ``re.fullmatch`` (never
    ``re.match``, which would accept trailing bytes after a valid-looking
    prefix) followed by ``date.fromisoformat`` (rejects calendar-invalid
    dates such as month 13 or day 99).

    Args:
        as_of: ``None`` (caller wants a machine stamp) or an ISO
            ``YYYY-MM-DD`` date string.

    Returns:
        ``None`` if ``as_of`` is ``None``; otherwise the validated date
        string, unmodified.

    Raises:
        InputLimitError: If ``as_of`` is neither ``None`` nor a valid,
            calendar-correct ``YYYY-MM-DD`` string.
    """
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not _AS_OF_RE.fullmatch(as_of):
        raise InputLimitError("as_of must be a YYYY-MM-DD date string")
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise InputLimitError("as_of must be a valid calendar date") from None
    return as_of


def clamp_max_results(max_results: object) -> int:
    """Clamp the optional ``max_results`` argument for ``verified_answer``.

    Non-int (or bool, which is a ``int`` subclass in Python) values silently
    fall back to the default. Valid ints are clamped to
    ``[1, MAX_RESULTS_CAP]`` (Decision 4) — never raises.

    Args:
        max_results: Caller-supplied value, expected to be an ``int`` or
            ``None``.

    Returns:
        An integer in ``[1, MAX_RESULTS_CAP]``.
    """
    if max_results is None or isinstance(max_results, bool) or not isinstance(max_results, int):
        return _DEFAULT_MAX_RESULTS
    return max(_MIN_RESULTS, min(max_results, MAX_RESULTS_CAP))


def stamp_today(*, today: date | None = None) -> str:
    """Return the machine-stamped current UTC date.

    Case 3 (real-world-cases.md): ``verified_answer`` re-stamps this on
    every call — never cached, never user-typed. In production (``today``
    omitted), every call reads the real clock fresh — nothing is cached.
    The optional ``today`` override exists solely so tests can inject a
    deterministic clock; it must never be wired to caller-supplied input.

    Args:
        today: Optional deterministic date override (tests only). When
            ``None`` (the default, used in production), the real system
            clock is read.

    Returns:
        ``today.isoformat()`` if provided, else
        ``datetime.now(tz=UTC).date().isoformat()``.
    """
    if today is not None:
        return today.isoformat()
    return datetime.now(tz=UTC).date().isoformat()


def stamp_now(*, today: date | None = None) -> str:
    """Return a full ISO-8601 UTC datetime stamp.

    Used for ``fetched_at`` fields (DN-5), which are full timestamps rather
    than dates — unified with the shape ``normalize_retrieved_documents``
    already stamps, so caller-supplied and retrieved documents never carry
    two different ``fetched_at`` shapes.

    Args:
        today: Optional deterministic date override (tests only); the time
            component is stamped as midnight UTC on that date. When
            ``None`` (production default), the real system clock is read.

    Returns:
        A full ISO-8601 UTC datetime string.
    """
    if today is not None:
        return datetime.combine(today, _dt_time.min, tzinfo=UTC).isoformat()
    return datetime.now(tz=UTC).isoformat()
