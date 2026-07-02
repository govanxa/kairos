"""Evidence Engine content_gate — the trust boundary (→ C2).

Converts raw fetched documents into sanitized SourceRecords. Everything
upstream is hostile; everything downstream trusts the gate. NO LLM inside
this module (EE-4).

Security: sanitizes EVERY string field per-document (T2). Rejected content
is discarded — never stored in state, never in exceptions (EE-2). All
rejection reasons come from the fixed REJECTION_REASONS vocabulary.

URL sanitization policy (SEV-001): the raw URL is sanitized via
sanitize_untrusted_text immediately after structural URL validation
(i.e., before any other field is processed). If sanitization mutates the
URL — credentials scrubbed (T7) or injection patterns neutralized — the
document is rejected with 'invalid_url'. Only the sanitized URL text is
stored in SourceRecord.url, rejected[].url, and citations. This is the
"reject on mutation" policy; the alternative (store-sanitized-and-flag)
was rejected because a mutated URL indicates the source cannot be trusted
as a valid reference.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from examples.evidence_engine.contracts import make_source_record
from examples.evidence_engine.untrusted_text import (
    SanitizedText,
    is_predominantly_instructional,
    sanitize_untrusted_text,
)
from kairos.exceptions import ExecutionError
from kairos.security import sanitize_exception

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fixed structural vocabulary for rejection reasons (EE-2: never content).
REJECTION_REASONS: frozenset[str] = frozenset(
    {
        "empty_after_cleaning",
        "predominantly_instructional",
        "missing_required_field",
        "invalid_url",
        "oversized",
    }
)

# Max raw document content length before gating (guard against huge pages).
_MAX_RAW_CONTENT_BYTES = 50_000

# URL validation pattern — must start with http:// or https://.
_URL_RE: re.Pattern[str] = re.compile(r"^https?://", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Domain extraction helper (D6: stdlib-only approximation; no tldextract)
# ---------------------------------------------------------------------------


def registrable_domain(url: str) -> str:
    """Extract registrable domain from a URL (stdlib approximation).

    Known limitation (D6): multi-level TLDs like .co.uk will return "co.uk"
    instead of the actual registrable domain. Trust-policy pins are the
    mitigation for high-stakes sources.

    Args:
        url: A URL string (http or https).

    Returns:
        Lowercased registrable domain (last two hostname components), or
        the full hostname if it has fewer than two components.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        parts = hostname.split(".")
        if len(parts) >= 2:
            return f"{parts[-2]}.{parts[-1]}"
        return hostname
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Core gate logic (pure — no StepContext dependency)
# ---------------------------------------------------------------------------


def gate_documents(
    documents: list[dict[str, Any]],
    *,
    as_of: str,
    max_documents: int = 50,
    max_excerpt: int = 2000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Process raw documents into sanitized SourceRecords.

    For each document, in order (02 §3.1):
    1. Check required fields — reject with 'missing_required_field' if absent.
    2. Validate URL pattern — reject with 'invalid_url' if malformed.
    3. Check raw content size — reject with 'oversized' if too large.
    4. Sanitize ALL string fields via sanitize_untrusted_text.
    5. Check structural rejection signals — reject with the appropriate reason.
    6. Build SourceRecord (tier/freshness/group set to placeholders; evaluator
       enriches these).

    Rejected document's raw content is discarded — only url and reason are kept.
    Cap applied after per-doc processing: only the first max_documents accepted
    docs become SourceRecords.

    Args:
        documents: Raw document dicts from the caller (MCP wire shape).
        as_of: ISO date the pipeline run reflects.
        max_documents: Hard cap on the number of SourceRecords produced (T8).
        max_excerpt: Max characters in the excerpt field (T8).

    Returns:
        (sources, rejected, gate_warnings)
        sources: List of SourceRecord dicts.
        rejected: List of {url, reason} dicts (no raw content).
        gate_warnings: Structural warning strings (no raw content).
    """
    sources: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    gate_warnings: list[str] = []
    source_counter = 0
    capped = False

    for doc in documents:
        if not isinstance(doc, dict):
            rejected.append({"url": "", "reason": "missing_required_field"})
            continue

        url = doc.get("url", "")
        if not isinstance(url, str) or not url:
            rejected.append({"url": "", "reason": "missing_required_field"})
            continue

        # --- URL validation (structural: must start with http/https) ---
        # SEV-001: a scheme-invalid URL is arbitrary attacker text with no legitimate
        # rendering use — sanitization alone can't neutralize arbitrary payloads in it,
        # so nothing from it is stored (EE-2/T7). Mirrors the missing-field precedent.
        if not _URL_RE.match(url):
            rejected.append({"url": "", "reason": "invalid_url"})
            continue

        # --- URL sanitization (SEV-001): sanitize immediately after validation.
        # If sanitization mutates the URL (credentials scrubbed or injection
        # patterns neutralized), reject with 'invalid_url' — mutated URL means
        # the source cannot be trusted as a valid reference. Only the sanitized
        # URL text is stored anywhere downstream (SourceRecord, rejected records).
        san_url: SanitizedText = sanitize_untrusted_text(url, max_len=2000)
        if san_url.text != url:
            rejected.append({"url": san_url.text[:200], "reason": "invalid_url"})
            continue
        stored_url: str = san_url.text  # safe URL for all downstream use

        content = doc.get("content", "")
        if not isinstance(content, str):
            rejected.append({"url": stored_url[:200], "reason": "missing_required_field"})
            continue

        fetched_at = doc.get("fetched_at", "")
        if not isinstance(fetched_at, str) or not fetched_at:
            rejected.append({"url": stored_url[:200], "reason": "missing_required_field"})
            continue

        # --- Raw size cap (T8: before sanitization to avoid OOM on huge content) ---
        if len(content) > _MAX_RAW_CONTENT_BYTES:
            rejected.append({"url": stored_url[:200], "reason": "oversized"})
            continue

        # --- Sanitize remaining string fields (T2: content, title) ---
        raw_content_len = len(content)
        san_content: SanitizedText = sanitize_untrusted_text(content, max_len=max_excerpt)

        # Sanitize title (optional field)
        raw_title = doc.get("title")
        san_title: SanitizedText | None = None
        if isinstance(raw_title, str) and raw_title:
            san_title = sanitize_untrusted_text(raw_title, max_len=200)

        # --- Structural rejection checks (after sanitization) ---
        if is_predominantly_instructional(san_content, raw_len=raw_content_len):
            rejected.append({"url": stored_url[:200], "reason": "predominantly_instructional"})
            continue

        if not san_content.text.strip():
            rejected.append({"url": stored_url[:200], "reason": "empty_after_cleaning"})
            continue

        # --- Document-count cap (T8) ---
        if source_counter >= max_documents:
            if not capped:
                gate_warnings.append(
                    f"Document count cap ({max_documents}) reached; additional documents discarded."
                )
                capped = True
            rejected.append({"url": stored_url[:200], "reason": "oversized"})
            continue

        # --- Collect injection flags from sanitized content and title.
        # URL flags are always empty for accepted docs (mutated URL → rejected above).
        all_flags: list[str] = sorted(
            set(san_content.flags) | set(san_title.flags if san_title else [])
        )

        # --- Build SourceRecord ---
        source_counter += 1
        source_id = f"S{source_counter}"
        domain = registrable_domain(stored_url)
        published_at = doc.get("published_at")
        if not isinstance(published_at, str):
            published_at = None

        title_text: str | None = san_title.text if san_title else None

        source = make_source_record(
            source_id=source_id,
            url=stored_url,
            domain=domain,
            title=title_text,
            fetched_at=fetched_at,
            published_at=published_at,
            # Placeholders — enriched by evidence_evaluator
            independence_group=domain,
            provenance_tier="unknown",
            freshness="undated",
            injection_flags=all_flags,
            excerpt=san_content.text,
        )
        sources.append(source)

        # Emit warnings for flagged sources (structural only, no content)
        if all_flags:
            gate_warnings.append(
                f"{source_id}: injection pattern(s) detected and neutralized "
                f"({', '.join(all_flags)})."
            )

    return sources, rejected, gate_warnings


# ---------------------------------------------------------------------------
# Step action
# ---------------------------------------------------------------------------


def content_gate(ctx: StepContext) -> dict[str, Any]:
    """Content gate step action.

    Reads 'raw_documents' and 'as_of' from state. Calls gate_documents.
    Writes 'sources', 'rejected', 'gate_warnings' to state. Returns the
    same dict for output_contract (GATE_OUTPUT) validation.

    EE-4: NO model_fn parameter — the gate is deterministic by construction.
    EE-1: Only this step holds read_keys=['raw_documents']; all downstream
          steps see only sanitized SourceRecords in 'sources'.

    Args:
        ctx: StepContext with scoped state proxy.

    Returns:
        {'sources': [...], 'rejected': [...], 'gate_warnings': [...]}

    Raises:
        ExecutionError: On unexpected failure (message sanitized via
            sanitize_exception before wrapping).
    """
    try:
        raw_docs_obj = ctx.state.get("raw_documents")
        as_of_obj = ctx.state.get("as_of")

        documents: list[dict[str, Any]] = (
            list(raw_docs_obj) if isinstance(raw_docs_obj, list) else []
        )
        as_of: str = str(as_of_obj) if as_of_obj is not None else ""

        sources, rejected, gate_warnings = gate_documents(documents, as_of=as_of)

        ctx.state.set("sources", sources)
        ctx.state.set("rejected", rejected)
        ctx.state.set("gate_warnings", gate_warnings)

        return {"sources": sources, "rejected": rejected, "gate_warnings": gate_warnings}

    except Exception as exc:
        error_type, error_msg = sanitize_exception(exc)
        raise ExecutionError(f"content_gate failed: {error_type}: {error_msg}") from None
