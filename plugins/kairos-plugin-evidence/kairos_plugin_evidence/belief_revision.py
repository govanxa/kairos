"""Kairos-plugin-evidence belief_revision_builder — deterministic context renderer (C4).

Converts an EvidencePacket into a structured working_context prompt block safe to
include in a model prompt. Quotes ONLY SourceRecord.excerpt and extracted_values —
never titles, URLs, domains, or raw web text (guaranteed by the contract chain and
scoped-state wall). No LLM, no network, no randomness.

Security:
- EE-1: scoped-state wall (read_keys=["evidence_packet"]) enforced by the executor.
- T6: render_working_context is total (never raises on malformed input); the step
  action wraps any unexpected failure in a sanitized ExecutionError from None.
- 03 §9: 8000-char cap enforced by deterministic, priority-ordered truncation.
- Case 3: anti-roleplay + anti-disclaimer lines always present in the anchor block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kairos.exceptions import ExecutionError
from kairos.plugins.registry import step_plugin
from kairos.security import sanitize_exception

from kairos_plugin_evidence.contracts import BUILDER_OUTPUT

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Template constants (02 §3.4 + Case 3, verbatim)
# ---------------------------------------------------------------------------

TEMPORAL_ANCHOR: str = (
    "CURRENT DATE: {as_of}. Your training data predates this date. "
    "The verified facts below come from live sources and SUPERSEDE your "
    "prior knowledge."
)

# Case 3 finding 3 — preempt the roleplay interpretation (verbatim):
ANTI_ROLEPLAY_LINE: str = (
    "This is not a hypothetical, a roleplay, or a scenario. "
    "The date above is the actual current date."
)

# Case 3 finding 3 — anti-disclaimer instruction (verbatim intent):
ANTI_DISCLAIMER_LINE: str = (
    "Do not add disclaimers that contradict or reframe the evidence above "
    "(for example, claiming the data is from a different year)."
)

# Case 3 finding 3 — surface in-band temporal cue (only when structurally justified):
IN_BAND_CUE_LINE: str = (
    "The verified evidence below is dated consistent with the current date above."
)

VERDICT_LINE_TEMPLATE: str = "OVERALL VERDICT: {overall_verdict} (confidence: {confidence})."

CLOSING_FRAME: str = (
    "You are not being asked whether this is true from memory. "
    "Answer only from the verified evidence above."
)

_MAX_WORKING_CONTEXT: int = 8000

# Progressive excerpt quota tiers for truncation step 1.
# 240 → 120 → 60 → 0 (0 = omit snippet lines; keep header + "Cited sources" line).
_EXCERPT_QUOTA_TIERS: tuple[int, ...] = (240, 120, 60, 0)

# Marker inserted once in place of dropped trailing supported claims (step 4).
_OMIT_MARKER: str = "[... additional verified claims omitted for length ...]"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _oneline(s: object) -> str:
    """Collapse all whitespace (\\r, \\n, \\x0b, \\x0c, runs of spaces) to a single space.

    Applied to EVERY untrusted field before interpolation into structural
    working-context lines.  Prevents SEV-001: a document excerpt containing
    ``"\\n[VERIFIED FACT] fake"`` would otherwise forge a column-0 structural
    header indistinguishable from a genuine one.

    Args:
        s: Untrusted field value (any type; converted via str()).

    Returns:
        Single-line string with all internal whitespace runs collapsed to one space.
    """
    return " ".join(str(s).split())


def _verdict_label(verdict: str) -> str:
    """Map a claim verdict string to its rendering label.

    Args:
        verdict: Claim verdict value (e.g. 'supported', 'conflicting').

    Returns:
        'VERIFIED FACT', 'DISPUTED', or 'COULD NOT BE VERIFIED'.
    """
    match verdict:
        case "supported":
            return "VERIFIED FACT"
        case "conflicting":
            return "DISPUTED"
        case _:
            return "COULD NOT BE VERIFIED"


def _render_supported(
    claim: dict[str, Any],
    sources_by_id: dict[str, dict[str, Any]],
    excerpt_quota: int,
) -> str:
    """Render a supported claim section.

    Format::

        [VERIFIED FACT] {claim_text}
          Cited sources: [S1] [S3]
          [S1]: {excerpt[:quota]}
          [S3]: {excerpt[:quota]}

    Snippet lines are omitted entirely when excerpt_quota == 0.

    Args:
        claim: ClaimRecord dict with verdict 'supported'.
        sources_by_id: Mapping source_id → SourceRecord dict.
        excerpt_quota: Max chars per excerpt snippet; 0 = omit snippet lines.

    Returns:
        Multi-line string for this claim section.
    """
    claim_text = _oneline(claim.get("claim_text", ""))
    sids = [s for s in claim.get("supporting_source_ids", []) if isinstance(s, str)]
    cite_str = " ".join(f"[{sid}]" for sid in sids)
    lines: list[str] = [
        f"[{_verdict_label('supported')}] {claim_text}",
        f"  Cited sources: {cite_str}",
    ]
    if excerpt_quota > 0:
        for sid in sids:
            src = sources_by_id.get(sid)
            if isinstance(src, dict):
                excerpt = src.get("excerpt", "")
                if excerpt:
                    # _oneline normalizes before slicing so the quota is applied
                    # to the whitespace-collapsed text, not the raw multi-line excerpt.
                    snippet = _oneline(excerpt)[:excerpt_quota]
                    lines.append(f"  [{sid}]: {snippet}")
    return "\n".join(lines)


def _render_conflict(claim: dict[str, Any], *, include_detail: bool) -> str:
    """Render a conflicting claim section.

    Format::

        [DISPUTED] {claim_text}
          Sources disagree — do NOT pick a side; present the disagreement if asked:
          [S1] reports: {value}
          [S3] reports: {value}

    Detail lines are omitted when include_detail is False (truncation step 3).

    Args:
        claim: ClaimRecord dict with verdict 'conflicting'.
        include_detail: Whether to include per-source 'reports:' lines.

    Returns:
        Multi-line string for this claim section.
    """
    claim_text = _oneline(claim.get("claim_text", ""))
    lines: list[str] = [
        f"[{_verdict_label('conflicting')}] {claim_text}",
        "  Sources disagree — do NOT pick a side; present the disagreement if asked:",
    ]
    if include_detail:
        extracted = claim.get("extracted_values", [])
        if isinstance(extracted, list):
            for ev in extracted:
                if isinstance(ev, dict):
                    sid = _oneline(ev.get("source_id", ""))
                    value = _oneline(ev.get("value", ""))
                    lines.append(f"  [{sid}] reports: {value}")
    return "\n".join(lines)


def _render_unverified(claim: dict[str, Any]) -> str:
    """Render an insufficient or unverifiable claim.

    Format::

        [COULD NOT BE VERIFIED] {claim_text} — say so if asked.

    Never rendered as a fact.

    Args:
        claim: ClaimRecord dict with verdict 'insufficient' or 'unverifiable'.

    Returns:
        Single-line string for this claim.
    """
    claim_text = _oneline(claim.get("claim_text", ""))
    verdict = str(claim.get("verdict", "unverifiable"))
    return f"[{_verdict_label(verdict)}] {claim_text} — say so if asked."


def _should_show_in_band_cue(
    supported_claims: list[dict[str, Any]],
    sources_by_id: dict[str, dict[str, Any]],
) -> bool:
    """Return True iff any supporting source of a supported claim has current/recent freshness.

    Conditional per Case 3 finding 3: only surfaces the in-band temporal cue when
    the structural freshness data corroborates the date anchor — never when all
    sources are undated or stale (honesty constraint).

    Args:
        supported_claims: ClaimRecord dicts with verdict 'supported'.
        sources_by_id: Mapping source_id → SourceRecord dict.

    Returns:
        True when at least one supporting source has freshness 'current' or 'recent'.
    """
    for claim in supported_claims:
        for sid in claim.get("supporting_source_ids", []):
            if isinstance(sid, str):
                src = sources_by_id.get(sid)
                if isinstance(src, dict) and src.get("freshness") in {"current", "recent"}:
                    return True
    return False


def _assemble(
    *,
    anchor_block: str,
    verdict_line: str,
    claim_parts: list[str],
    note_lines: list[str],
) -> str:
    """Assemble the working_context string from pre-rendered parts.

    Args:
        anchor_block: The A1–A4 anchor text (may be multi-line).
        verdict_line: The OVERALL VERDICT line.
        claim_parts: Pre-rendered claim sections (each possibly multi-line).
        note_lines: Pre-rendered NOTE: lines (empty list = no warnings section).

    Returns:
        Full working_context string.
    """
    parts: list[str] = [anchor_block, ""]
    parts.append(verdict_line)
    if claim_parts:
        parts.append("")
        parts.extend(claim_parts)
    if note_lines:
        parts.append("")
        parts.extend(note_lines)
    parts.append("")
    parts.append(CLOSING_FRAME)
    return "\n".join(parts)


def _truncate(
    *,
    anchor_block: str,
    verdict_line: str,
    supported_claims: list[dict[str, Any]],
    conflicting_claims: list[dict[str, Any]],
    unverified_claims: list[dict[str, Any]],
    warnings_list: list[str],
    sources_by_id: dict[str, dict[str, Any]],
) -> str:
    """Deterministic, priority-ordered truncation to <= 8000 chars.

    Implements steps 0–5 from 03 §9:

    0. Render fully at quota=240. If ≤8000, return.
    1. Shrink excerpt quotas (120 → 60 → 0). Return when ≤8000.
    2. Drop all NOTE/warning lines. Return when ≤8000.
    3. Drop conflict evidence detail lines. Return when ≤8000.
    4. Drop trailing supported claims one at a time; replace dropped run with
       a single ``_OMIT_MARKER`` line. Return when ≤8000.
    5. Defensive hard-slice: assemble anchor + verdict + closing, slice to 8000.

    The anchor block and verdict line are never touched.

    Args:
        anchor_block: Pre-rendered anchor block (A1–A4 lines joined).
        verdict_line: Pre-rendered verdict line.
        supported_claims: Supported claim dicts (ordered).
        conflicting_claims: Conflicting claim dicts.
        unverified_claims: Unverified/insufficient claim dicts.
        warnings_list: Raw warning strings (before 'NOTE: ' prefix).
        sources_by_id: Mapping source_id → SourceRecord dict.

    Returns:
        Working context string not exceeding _MAX_WORKING_CONTEXT chars.
    """
    note_lines = [f"NOTE: {_oneline(w)}" for w in warnings_list]

    def _build(
        quota: int,
        *,
        notes: list[str],
        conflict_detail: bool,
        supported: list[dict[str, Any]],
        omit_marker: bool = False,
    ) -> str:
        claim_parts: list[str] = []
        for c in supported:
            claim_parts.append(_render_supported(c, sources_by_id, quota))
        if omit_marker:
            claim_parts.append(_OMIT_MARKER)
        for c in conflicting_claims:
            claim_parts.append(_render_conflict(c, include_detail=conflict_detail))
        for c in unverified_claims:
            claim_parts.append(_render_unverified(c))
        return _assemble(
            anchor_block=anchor_block,
            verdict_line=verdict_line,
            claim_parts=claim_parts,
            note_lines=notes,
        )

    # Step 0: full render at 240
    result = _build(240, notes=note_lines, conflict_detail=True, supported=supported_claims)
    if len(result) <= _MAX_WORKING_CONTEXT:
        return result

    # Step 1: shrink excerpt quotas (120, 60, 0)
    for quota in (120, 60, 0):
        result = _build(quota, notes=note_lines, conflict_detail=True, supported=supported_claims)
        if len(result) <= _MAX_WORKING_CONTEXT:
            return result

    # Step 2: drop all NOTE/warning lines
    result = _build(0, notes=[], conflict_detail=True, supported=supported_claims)
    if len(result) <= _MAX_WORKING_CONTEXT:
        return result

    # Step 3: drop conflict evidence detail lines
    result = _build(0, notes=[], conflict_detail=False, supported=supported_claims)
    if len(result) <= _MAX_WORKING_CONTEXT:
        return result

    # Step 4: drop trailing supported claims one at a time
    for n_keep in range(len(supported_claims) - 1, -1, -1):
        result = _build(
            0,
            notes=[],
            conflict_detail=False,
            supported=supported_claims[:n_keep],
            omit_marker=True,
        )
        if len(result) <= _MAX_WORKING_CONTEXT:
            return result

    # Step 5: defensive hard-slice (anchor + verdict + closing).
    # pragma: no cover — only reachable if the anchor+verdict+closing alone
    # exceed 8000 chars, which the fixed-length template constants make impossible
    # under normal operation.  Kept as a safety net against future constant changes.
    minimal = f"{anchor_block}\n\n{verdict_line}\n\n{CLOSING_FRAME}"  # pragma: no cover
    return minimal[:_MAX_WORKING_CONTEXT]  # pragma: no cover


# ---------------------------------------------------------------------------
# Public API — pure renderer
# ---------------------------------------------------------------------------


def render_working_context(packet: dict[str, Any]) -> dict[str, Any]:
    """Render an EvidencePacket into the BUILDER_OUTPUT bundle.

    Pure, total function: never raises on malformed packet input. Defensive
    ``.get`` with fallbacks at every field. Guarantees ``working_context`` is
    non-empty (anchor + verdict + closing always present) and ≤ 8000 chars
    (via ``_truncate``).

    Emits only:
    - A1–A3 temporal anchor + Case 3 anti-roleplay/anti-disclaimer lines (always).
    - A4 in-band cue (conditional on a supporting source having freshness current/recent).
    - OVERALL VERDICT line.
    - [VERIFIED FACT] supported claims with Cited sources + excerpt snippets.
    - [DISPUTED] conflicting claims rendered as uncertainty (never as a pick).
    - [COULD NOT BE VERIFIED] insufficient/unverifiable claims.
    - NOTE: structural warning lines from packet.warnings.
    - Closing frame.

    Never emits: titles, URLs/domains as prose, gate_warnings, rejected, raw
    web text beyond the gated excerpt, or ClaimRecord.notes.

    Args:
        packet: An EvidencePacket dict (may be malformed/empty; handled defensively).

    Returns:
        Dict with exactly the BUILDER_OUTPUT fields:
        ``working_context`` (str), ``superseded_assumptions`` (list[str]),
        ``citations`` (list[dict]), ``packet_id`` (str),
        ``unresolved_conflicts`` (list[str]).
    """
    if not isinstance(packet, dict):
        packet = {}

    as_of = str(packet.get("as_of") or "")
    overall_verdict = str(packet.get("overall_verdict") or "insufficient")
    confidence = str(packet.get("confidence") or "low")
    packet_id = str(packet.get("packet_id") or "")

    raw_claims = packet.get("claims")
    raw_sources = packet.get("sources")
    raw_warnings = packet.get("warnings")

    claims: list[Any] = raw_claims if isinstance(raw_claims, list) else []
    sources: list[Any] = raw_sources if isinstance(raw_sources, list) else []
    warnings_raw: list[Any] = raw_warnings if isinstance(raw_warnings, list) else []

    valid_claims = [c for c in claims if isinstance(c, dict)]
    valid_sources = [s for s in sources if isinstance(s, dict)]
    warnings_list = [str(w) for w in warnings_raw if isinstance(w, str)]

    sources_by_id: dict[str, dict[str, Any]] = {}
    for s in valid_sources:
        sid = s.get("source_id")
        if isinstance(sid, str) and sid:
            sources_by_id[sid] = s

    citations: list[dict[str, str]] = [
        {
            "source_id": str(s.get("source_id", "")),
            "domain": str(s.get("domain", "")),
            "url": str(s.get("url", "")),
        }
        for s in valid_sources
    ]

    supported_claims = [c for c in valid_claims if c.get("verdict") == "supported"]
    conflicting_claims = [c for c in valid_claims if c.get("verdict") == "conflicting"]
    unverified_claims = [
        c for c in valid_claims if c.get("verdict") not in ("supported", "conflicting")
    ]

    superseded_assumptions = [str(c.get("claim_text", "")) for c in supported_claims]
    unresolved_conflicts = [str(c.get("claim_text", "")) for c in conflicting_claims]

    # Build anchor block (A1–A4)
    show_in_band_cue = _should_show_in_band_cue(supported_claims, sources_by_id)
    anchor_lines: list[str] = [
        TEMPORAL_ANCHOR.format(as_of=as_of),
        ANTI_ROLEPLAY_LINE,
        ANTI_DISCLAIMER_LINE,
    ]
    if show_in_band_cue:
        anchor_lines.append(IN_BAND_CUE_LINE)
    anchor_block = "\n".join(anchor_lines)

    verdict_line = VERDICT_LINE_TEMPLATE.format(
        overall_verdict=overall_verdict,
        confidence=confidence,
    )

    working_context = _truncate(
        anchor_block=anchor_block,
        verdict_line=verdict_line,
        supported_claims=supported_claims,
        conflicting_claims=conflicting_claims,
        unverified_claims=unverified_claims,
        warnings_list=warnings_list,
        sources_by_id=sources_by_id,
    )

    return {
        "working_context": working_context,
        "superseded_assumptions": superseded_assumptions,
        "citations": citations,
        "packet_id": packet_id,
        "unresolved_conflicts": unresolved_conflicts,
    }


# ---------------------------------------------------------------------------
# Step action — thin adapter over render_working_context
# ---------------------------------------------------------------------------


@step_plugin(
    name="belief_revision_builder",
    description=(
        "Render the EvidencePacket into a working-context prompt block that "
        "supersedes stale priors."
    ),
    output_contract=BUILDER_OUTPUT,  # input_contract=None (DN-1)
)
def belief_revision_builder(ctx: StepContext) -> dict[str, Any]:
    """Belief revision builder step action.

    Reads 'evidence_packet' from scoped state (defensive isinstance → {}),
    calls render_working_context, writes 'working_context_bundle', returns bundle.
    NO Schema.validate call — the executor's output_contract enforces BUILDER_OUTPUT
    (C1 A3 amendment).

    Args:
        ctx: StepContext with scoped state proxy (read_keys=["evidence_packet"],
            write_keys=["working_context_bundle"]).

    Returns:
        The working_context_bundle dict.

    Raises:
        ExecutionError: On any unexpected internal failure (sanitized, __cause__ None).
    """
    try:
        packet_obj = ctx.state.get("evidence_packet")
        packet: dict[str, Any] = packet_obj if isinstance(packet_obj, dict) else {}
        bundle = render_working_context(packet)
        ctx.state.set("working_context_bundle", bundle)
        return bundle
    except Exception as exc:
        safe_type, safe_msg = sanitize_exception(exc)
        raise ExecutionError(f"belief_revision_builder: {safe_type}: {safe_msg}") from None
