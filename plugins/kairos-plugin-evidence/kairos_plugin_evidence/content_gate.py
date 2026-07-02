"""Kairos-plugin-evidence content_gate — the trust boundary (C2).

Converts raw fetched documents into sanitized SourceRecords. Everything
upstream is hostile; everything downstream trusts the gate. NO LLM inside
this module (EE-4).

Security: sanitizes EVERY string field per-document (T2). Rejected content
is discarded — never stored in state, never in exceptions (EE-2). All
rejection reasons come from the fixed REJECTION_REASONS vocabulary.

URL sanitization policy (SEV-001): the raw URL is sanitized via
sanitize_untrusted_text immediately after structural URL validation
(scheme gate). If sanitization mutates the URL — credentials scrubbed (T7)
or injection patterns neutralized — the document is rejected with
'invalid_url'. Only the sanitized URL is ever stored anywhere (SourceRecord,
rejected records, citations). This is the reject-on-mutation policy; a
mutated URL indicates the source cannot be trusted as a valid reference.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from kairos.exceptions import ExecutionError
from kairos.plugins.registry import step_plugin
from kairos.security import (
    SanitizedText,
    is_predominantly_instructional,
    sanitize_exception,
    sanitize_untrusted_text,
)

from kairos_plugin_evidence.contracts import (
    GATE_OUTPUT,
    Freshness,
    ProvenanceTier,
    make_source_record,
)

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Fixed structural rejection vocabulary (EE-2: reasons are structural names,
# never raw content). All emitted rejection reasons must be members of this set.
# ---------------------------------------------------------------------------

REJECTION_REASONS: frozenset[str] = frozenset(
    {
        "empty_after_cleaning",
        "predominantly_instructional",
        "missing_required_field",
        "invalid_url",
        "oversized",
    }
)

# ---------------------------------------------------------------------------
# Caps (see blueprint §Caps for rationale)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_DOCUMENTS: int = 50
_DEFAULT_MAX_EXCERPT: int = 2000
_MAX_RAW_CONTENT_CHARS: int = 50_000
_MAX_TITLE_LEN: int = 200
_MAX_TOTAL_OUTPUT_CHARS: int = 500_000

# ---------------------------------------------------------------------------
# Only regex in this module: linear, anchored, T9-safe by construction.
# Applied to already-validated (structurally) URL strings only; never to
# untrusted body text (that surface is owned by B1's pre-compiled patterns).
# ---------------------------------------------------------------------------

_URL_RE: re.Pattern[str] = re.compile(r"^https?://", re.IGNORECASE)

# Content field aliases — canonical first (DN-2). First non-empty str wins.
_CONTENT_KEYS: tuple[str, ...] = ("content", "text", "snippet")


# ---------------------------------------------------------------------------
# Stdlib registrable-domain helper (06 §3; no tldextract)
# ---------------------------------------------------------------------------


def registrable_domain(url: str) -> str:
    """Extract an approximate registrable domain from a URL using stdlib only.

    Known limitation: multi-level public suffixes (.co.uk, .com.au) yield the
    second-to-last and last label pair (the public suffix) instead of the true
    registrable domain. Trust-policy pins (C3) are the mitigation for high-stakes
    sources. Always lowercased; returns empty string on parse failure.

    Args:
        url: A URL string (expected to start with http:// or https://).

    Returns:
        Lowercased last-two-label pair (e.g. "example.org") or the full
        hostname when fewer than two labels exist. Empty string on failure.
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
# Pure gate core (no StepContext, no I/O, no LLM)
# ---------------------------------------------------------------------------


def gate_documents(
    documents: list[Any],
    *,
    max_documents: int = _DEFAULT_MAX_DOCUMENTS,
    max_excerpt: int = _DEFAULT_MAX_EXCERPT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Gate raw documents into sanitized SourceRecords.

    Deterministic. No LLM, no network, no clock, no state. Per document,
    in the exact order specified by 02 §3.1:

      1. Structural gate: dict? url present + non-empty str?
         → else reject 'missing_required_field'.
      2. URL scheme gate: _URL_RE.match(url)?
         → else reject 'invalid_url' (stored url: "" — SEV-001).
      3. URL sanitize (SEV-001): sanitize_untrusted_text(url). If mutated
         (credential scrubbed / injection neutralized) → reject 'invalid_url',
         store sanitized[:200]; never raw attacker text.
      4. Body resolution via _CONTENT_KEYS (DN-2): first non-empty str in
         (content, text, snippet). None found → reject 'missing_required_field'.
      5. fetched_at present + non-empty str? (DN-5)
         → else reject 'missing_required_field'.
      6. Raw size gate: len(body) > _MAX_RAW_CONTENT_CHARS
         → reject 'oversized' (before sanitization, guards OOM).
      7. Sanitize body (max_len=max_excerpt) and title (max_len=_MAX_TITLE_LEN).
      8. is_predominantly_instructional check
         → reject 'predominantly_instructional'.
      9. Empty-after-cleaning check → reject 'empty_after_cleaning'.
     10. Count cap: accepted count >= max_documents → warn once + reject 'oversized'.
     11. Total-output cap: running excerpt chars > _MAX_TOTAL_OUTPUT_CHARS
         → warn once + reject 'oversized'.
     12. Build SourceRecord via make_source_record with placeholders
         (tier=UNKNOWN, freshness=UNDATED, independence_group=registrable domain,
         injection_flags=sorted union of body + title flags).

    Rejected raw content is discarded — only {url, reason} is kept (EE-2).
    Invariant: len(sources) + len(rejected) == len(documents).

    Args:
        documents: Raw document dicts from the caller. Non-dict items are
            accepted at the list level and rejected at step 1.
        max_documents: Hard cap on the number of accepted SourceRecords (T8).
        max_excerpt: Max characters in the sanitized excerpt field (T8).
            Values above 2000 are silently clamped to 2000 to prevent
            SOURCE_RECORD ``length(max=2000)`` violations.

    Returns:
        A 3-tuple (sources, rejected, gate_warnings).
        sources: List of SourceRecord dicts (sanitized, placeholder-stamped).
        rejected: List of ``{url, reason}`` dicts — no raw content.
        gate_warnings: Structural warning strings — no raw content.
    """
    # Clamp max_excerpt to [0, 2000]: the schema-enforced ceiling stops
    # SOURCE_RECORD length(max=2000) violations; the zero floor stops negative
    # values from widening the excerpt via negative slicing (SEV-ADV-003).
    max_excerpt = max(0, min(max_excerpt, _DEFAULT_MAX_EXCERPT))

    sources: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    gate_warnings: list[str] = []

    source_counter: int = 0
    count_cap_warned: bool = False
    total_cap_warned: bool = False
    running_total_chars: int = 0

    for doc in documents:
        # Step 1 — structural gate
        if not isinstance(doc, dict):
            rejected.append({"url": "", "reason": "missing_required_field"})
            continue

        url_raw: Any = doc.get("url", "")
        if not isinstance(url_raw, str) or not url_raw:
            rejected.append({"url": "", "reason": "missing_required_field"})
            continue
        url: str = url_raw

        # Step 2 — URL scheme gate (SEV-001: scheme-invalid is arbitrary attacker text;
        # nothing from it is stored — mirrors missing-field precedent).
        if not _URL_RE.match(url):
            rejected.append({"url": "", "reason": "invalid_url"})
            continue

        # Step 3 — URL sanitization (SEV-001 reject-on-mutation).
        # If sanitization changes the URL in any way (credential scrubbed,
        # injection pattern neutralized), the URL is untrustworthy as a reference.
        san_url: SanitizedText = sanitize_untrusted_text(url, max_len=2000)
        if san_url.text != url:
            # Store only the sanitized version (max 200 chars) — never raw attacker text.
            rejected.append({"url": san_url.text[:200], "reason": "invalid_url"})
            continue
        stored_url: str = san_url.text  # safe, sanitization-verified URL

        # Step 4 — body resolution via content-key aliases (DN-2).
        body: str | None = None
        for key in _CONTENT_KEYS:
            val: Any = doc.get(key)
            if isinstance(val, str) and val:
                body = val
                break
        if body is None:
            rejected.append({"url": stored_url[:200], "reason": "missing_required_field"})
            continue

        # Step 5 — fetched_at required (DN-5: retrieval time must always be known).
        fetched_at_raw: Any = doc.get("fetched_at", "")
        if not isinstance(fetched_at_raw, str) or not fetched_at_raw:
            rejected.append({"url": stored_url[:200], "reason": "missing_required_field"})
            continue
        fetched_at: str = fetched_at_raw

        # Step 6 — raw size gate (BEFORE sanitization — guards against OOM on huge pages).
        if len(body) > _MAX_RAW_CONTENT_CHARS:
            rejected.append({"url": stored_url[:200], "reason": "oversized"})
            continue

        # Step 7 — sanitize body and title.
        raw_len: int = len(body)
        san_body: SanitizedText = sanitize_untrusted_text(body, max_len=max_excerpt)

        raw_title: Any = doc.get("title")
        san_title: SanitizedText | None = None
        if isinstance(raw_title, str) and raw_title:
            san_title = sanitize_untrusted_text(raw_title, max_len=_MAX_TITLE_LEN)

        # Step 8 — predominantly-instructional check.
        if is_predominantly_instructional(san_body, raw_len=raw_len):
            rejected.append({"url": stored_url[:200], "reason": "predominantly_instructional"})
            continue

        # Step 9 — empty-after-cleaning check.
        # Note: is_predominantly_instructional (step 8) already returns True when
        # san_body.text is empty/whitespace-only, so this branch is a defence-in-depth
        # fallback for future changes to that function.  pragma: no cover intentional.
        if not san_body.text.strip():  # pragma: no cover
            rejected.append({"url": stored_url[:200], "reason": "empty_after_cleaning"})
            continue

        # Step 10 — count cap.
        if source_counter >= max_documents:
            if not count_cap_warned:
                gate_warnings.append(
                    f"Document count cap ({max_documents}) reached; additional documents discarded."
                )
                count_cap_warned = True
            rejected.append({"url": stored_url[:200], "reason": "oversized"})
            continue

        # Step 11 — total-output cap.
        excerpt_len: int = len(san_body.text)
        if running_total_chars + excerpt_len > _MAX_TOTAL_OUTPUT_CHARS:
            if not total_cap_warned:
                gate_warnings.append(
                    f"Total output cap ({_MAX_TOTAL_OUTPUT_CHARS:,} chars) reached; "
                    "remaining documents discarded."
                )
                total_cap_warned = True
            rejected.append({"url": stored_url[:200], "reason": "oversized"})
            continue

        # Step 12 — build SourceRecord with placeholder provenance fields.
        running_total_chars += excerpt_len
        source_counter += 1
        source_id: str = f"S{source_counter}"
        domain: str = registrable_domain(stored_url)

        published_at_raw: Any = doc.get("published_at")
        published_at: str | None = published_at_raw if isinstance(published_at_raw, str) else None

        title_text: str | None = san_title.text if san_title is not None else None

        # Collect injection flags from body + title (URL flags always empty for accepted
        # docs — a mutated URL was rejected at step 3).
        all_flags: list[str] = sorted(
            set(san_body.flags) | (set(san_title.flags) if san_title is not None else set())
        )

        source = make_source_record(
            source_id=source_id,
            url=stored_url,
            domain=domain,
            title=title_text,
            fetched_at=fetched_at,
            published_at=published_at,
            independence_group=domain,
            provenance_tier=str(ProvenanceTier.UNKNOWN),
            freshness=str(Freshness.UNDATED),
            injection_flags=all_flags,
            excerpt=san_body.text,
        )
        sources.append(source)

        if all_flags:
            gate_warnings.append(
                f"{source_id}: injection pattern(s) detected and neutralized "
                f"({', '.join(all_flags)})."
            )

    return sources, rejected, gate_warnings


# ---------------------------------------------------------------------------
# Step action (thin @step_plugin adapter over gate_documents)
# ---------------------------------------------------------------------------


@step_plugin(
    name="content_gate",
    description="Sanitize raw web documents into SourceRecords — the trust boundary.",
    output_contract=GATE_OUTPUT,
    # input_contract intentionally omitted (DN-1): the gate is the entry step
    # and reads raw_documents from scoped state (ctx.inputs == {}); wiring
    # GATE_INPUT here would fail input validation on every run.
)
def content_gate(ctx: StepContext) -> dict[str, Any]:
    """Step action: gate raw documents from state into sanitized SourceRecords.

    Reads ``raw_documents`` from scoped state (read_keys wall enforced by the
    executor). Calls ``gate_documents``. Writes ``sources``, ``rejected``, and
    ``gate_warnings`` to state. Returns the same dict for GATE_OUTPUT executor
    validation.

    EE-1: Only this step may read ``raw_documents``; all downstream steps see
    only the sanitized ``sources`` key.
    EE-4: No model_fn parameter — the gate is deterministic by construction.

    Args:
        ctx: StepContext with a scoped state proxy configured with
            ``read_keys=["raw_documents"]`` and
            ``write_keys=["sources", "rejected", "gate_warnings"]``.

    Returns:
        ``{"sources": [...], "rejected": [...], "gate_warnings": [...]}``.

    Raises:
        ExecutionError: On unexpected internal failure (state I/O error, etc.).
            Message is sanitized via ``sanitize_exception()``; ``__cause__`` is
            suppressed (``from None``) so no raw traceback content escapes (T6).
    """
    try:
        raw_docs_obj: Any = ctx.state.get("raw_documents")
        documents: list[Any] = list(raw_docs_obj) if isinstance(raw_docs_obj, list) else []

        sources, rejected, gate_warnings = gate_documents(documents)

        ctx.state.set("sources", sources)
        ctx.state.set("rejected", rejected)
        ctx.state.set("gate_warnings", gate_warnings)

        return {"sources": sources, "rejected": rejected, "gate_warnings": gate_warnings}

    except Exception as exc:
        error_type, error_msg = sanitize_exception(exc)
        raise ExecutionError(f"content_gate failed: {error_type}: {error_msg}") from None
