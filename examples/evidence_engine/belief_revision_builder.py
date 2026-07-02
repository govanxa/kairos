"""Evidence Engine belief_revision_builder — deterministic context renderer (→ C4).

Converts an EvidencePacket into a structured working_context string safe to
include in a model prompt. Quotes ONLY SourceRecord.excerpt and
extracted_values — never raw web text (guaranteed by the contract chain +
read_key wall). Truncation priority (02 §3.4): anchor → supported claims →
conflicts → warnings, trimming source excerpts first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Prompt frame constants (02 §3.4)
# ---------------------------------------------------------------------------

TEMPORAL_ANCHOR: str = (
    "CURRENT DATE: {as_of}. Your training data predates this date. "
    "The verified facts below come from live sources and SUPERSEDE your "
    "prior knowledge."
)

CLOSING_FRAME: str = (
    "You are not being asked whether this is true from memory. "
    "Answer from the verified evidence above."
)

# Working context hard cap (BUILDER_OUTPUT validator: v.length(max=8000)).
_MAX_WORKING_CONTEXT = 8000

# Anchor + closing frame are never truncated; budget remaining chars for claims.
_ANCHOR_RESERVE = 300  # generous upper bound for the anchor line
_CLOSING_RESERVE = 150  # generous upper bound for the closing frame


# ---------------------------------------------------------------------------
# Renderer (pure)
# ---------------------------------------------------------------------------


def _verdict_label(verdict: str) -> str:
    """Map a claim verdict to a human-readable rendering label."""
    match verdict:
        case "supported":
            return "VERIFIED FACT"
        case "conflicting":
            return "DISPUTED"
        case "insufficient":
            return "INSUFFICIENT EVIDENCE"
        case _:
            return "COULD NOT BE VERIFIED"


def render_working_context(packet: dict[str, Any]) -> dict[str, Any]:
    """Render an EvidencePacket into a structured working_context bundle.

    Emits only:
    - Temporal anchor (CURRENT DATE) — always first, never truncated.
    - 'supported' claims as facts with [Si] citation keys.
    - 'conflicting' claims as explicit uncertainty (not a choice).
    - 'insufficient'/'unverifiable' claims as 'could not be verified'.
    - Closing frame — appended last, never truncated.
    - Structural warnings from the packet.

    Truncation priority when rendered text > 8000 chars (02 §3.4):
    Trim source excerpts first → then conflicts → then warnings.
    Anchor and verdict lines are never trimmed.

    Args:
        packet: A validated EvidencePacket dict.

    Returns:
        Dict with keys: working_context (str), superseded_assumptions (list),
        citations (list), packet_id (str), unresolved_conflicts (list).
    """
    as_of = packet.get("as_of", "")
    anchor_line = TEMPORAL_ANCHOR.format(as_of=as_of)

    claims: list[dict[str, Any]] = packet.get("claims", [])
    sources: list[dict[str, Any]] = packet.get("sources", [])
    warnings: list[str] = packet.get("warnings", [])
    packet_id: str = packet.get("packet_id", "")

    sources_by_id: dict[str, dict[str, Any]] = {s["source_id"]: s for s in sources}

    # Build citation index and superseded assumptions list.
    citations: list[dict[str, str]] = [
        {
            "source_id": s["source_id"],
            "domain": s.get("domain", ""),
            "url": s.get("url", ""),
        }
        for s in sources
    ]

    superseded_assumptions: list[str] = []

    # Render each claim section.
    claim_lines: list[str] = []
    unresolved_conflicts: list[str] = []

    for claim in claims:
        verdict = claim.get("verdict", "unverifiable")
        claim_text = claim.get("claim_text", "")
        label = _verdict_label(verdict)

        if verdict == "supported":
            supporting_ids: list[str] = claim.get("supporting_source_ids", [])
            # Build citation keys string.
            cite_keys = " ".join(f"[{sid}]" for sid in supporting_ids)
            # Include a short excerpt from each supporting source (truncatable).
            evidence_snippets: list[str] = []
            for sid in supporting_ids:
                src = sources_by_id.get(sid, {})
                excerpt = src.get("excerpt", "")
                if excerpt:
                    # Limit per-source excerpt to 200 chars for space efficiency.
                    snippet = excerpt[:200].rstrip()
                    evidence_snippets.append(f"  [{sid}]: {snippet}")
            evidence_block = "\n".join(evidence_snippets)
            claim_section = f"[{label}] {claim_text}\nSources: {cite_keys}\n{evidence_block}"
            claim_lines.append(claim_section)
            superseded_assumptions.append(claim_text)

        elif verdict == "conflicting":
            conflicting_ids: list[str] = claim.get("conflicting_source_ids", [])
            extracted: list[dict[str, str]] = claim.get("extracted_values", [])
            dispute_parts = [
                f"  [{ev['source_id']}] says: {ev.get('value', '')[:80]}"
                for ev in extracted
                if ev.get("source_id") in conflicting_ids or not conflicting_ids
            ]
            dispute_block = "\n".join(dispute_parts)
            claim_section = (
                f"[{label}] {claim_text}\nSources disagree — do NOT pick a side:\n{dispute_block}"
            )
            claim_lines.append(claim_section)
            unresolved_conflicts.append(claim_text)

        else:
            # insufficient or unverifiable — do not render as a fact.
            claim_section = f"[{label}] {claim_text}"
            claim_lines.append(claim_section)

    # Render warning block.
    warning_lines: list[str] = [f"NOTE: {w}" for w in warnings]

    # Assemble full context.
    sections: list[str] = [anchor_line, ""]
    sections.extend(claim_lines)
    if warning_lines:
        sections.append("")
        sections.extend(warning_lines)
    sections.append("")
    sections.append(CLOSING_FRAME)

    full_text = "\n".join(sections)

    # Truncation pass if over limit.
    if len(full_text) > _MAX_WORKING_CONTEXT:
        # Trim per-source excerpt snippets first (lines starting with "  [S").
        trimmed_claims: list[str] = []
        for section in claim_lines:
            lines = section.split("\n")
            trimmed_lines = []
            for line in lines:
                if line.startswith("  [S") and len(line) > 80:
                    line = line[:80] + "…"
                trimmed_lines.append(line)
            trimmed_claims.append("\n".join(trimmed_lines))

        sections = [anchor_line, ""]
        sections.extend(trimmed_claims)
        # Drop warnings if still too long.
        candidate = "\n".join(sections + ["", CLOSING_FRAME])
        if len(candidate) <= _MAX_WORKING_CONTEXT:
            full_text = candidate
        else:
            # Last resort: truncate the body, preserving anchor + closing frame.
            body_budget = (
                _MAX_WORKING_CONTEXT
                - len(anchor_line)
                - len(CLOSING_FRAME)
                - 4  # separator newlines
            )
            body = "\n".join(trimmed_claims)[:body_budget]
            full_text = f"{anchor_line}\n\n{body}\n\n{CLOSING_FRAME}"

    return {
        "working_context": full_text,
        "superseded_assumptions": superseded_assumptions,
        "citations": citations,
        "packet_id": packet_id,
        "unresolved_conflicts": unresolved_conflicts,
    }


# ---------------------------------------------------------------------------
# Step action
# ---------------------------------------------------------------------------


def belief_revision_builder(ctx: StepContext) -> dict[str, Any]:
    """Belief revision builder step action.

    Reads 'evidence_packet' from state. Writes 'working_context_bundle'.
    Returns the bundle dict for BUILDER_OUTPUT validation.

    Args:
        ctx: StepContext with scoped state proxy.

    Returns:
        The working_context_bundle dict.
    """
    packet_obj = ctx.state.get("evidence_packet")
    packet: dict[str, Any] = packet_obj if isinstance(packet_obj, dict) else {}

    bundle = render_working_context(packet)

    ctx.state.set("working_context_bundle", bundle)
    return bundle
