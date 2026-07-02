"""Evidence Engine contracts — Schemas, derivation tables, and constructor helpers (→ C1).

All schemas are verbatim from 03 §2/§3/§6. Derivation functions implement
the deterministic tables from 03 §4–5. Records are plain JSON-native dicts.

F1 finding: shipped kairos/schema.py supports native list[Schema] item validation
via the list[SCHEMA_INSTANCE] DSL. C1 may adopt that and drop _each_matches.
The spike builds _each_matches as the canonical v1 mitigation per 03 §7.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from kairos.schema import Schema, ValidationResult
from kairos.validators import length, not_empty, one_of, pattern

# Type alias for field validators compatible with kairos.validators attachment.
FieldValidator = Callable[[object], bool | str]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKET_VERSION = "1.0"

# Tier and freshness ordinal ranks (lower = better quality).
_TIER_RANK: dict[str, int] = {
    "primary": 0,
    "official": 1,
    "established_media": 2,
    "aggregator": 3,
    "user_generated": 4,
    "unknown": 5,
}
_FRESHNESS_RANK: dict[str, int] = {
    "current": 0,
    "recent": 1,
    "stale": 2,
    "undated": 3,
}


# ---------------------------------------------------------------------------
# _each_matches — 03 §7 nested-validation helper
# ---------------------------------------------------------------------------


def _each_matches(item_schema: Schema) -> FieldValidator:
    """Field validator: every list item must pass item_schema.validate().

    Returns True or a structural error string 'item {i}: {field-level message}'.
    Error strings contain only schema-produced structural info — never item content.

    F1 note: kairos/schema.py supports list[Schema] natively via nested_schema.
    This helper remains canonical for C1 per 03 §7; C1 may drop it in favour
    of the native DSL after a 03 §7 amendment.

    Args:
        item_schema: Schema each list item must satisfy.

    Returns:
        A FieldValidator callable (compatible with validators={} attachment).
    """

    def _check(items: object) -> bool | str:
        if not isinstance(items, list):
            return "expected a list"
        for i, item in enumerate(items):
            result: ValidationResult = item_schema.validate(item)
            if not result.valid:
                # Structural message only — never echoes item content.
                msg = result.errors[0].message if result.errors else "invalid item"
                return f"item {i}: {msg}"
        return True

    return _check


# ---------------------------------------------------------------------------
# SOURCE_RECORD — 03 §2
# ---------------------------------------------------------------------------

SOURCE_RECORD: Schema = Schema(
    {
        "source_id": str,
        "url": str,
        "domain": str,
        "title": str | None,
        "fetched_at": str,
        "published_at": str | None,
        "independence_group": str,
        "provenance_tier": str,
        "freshness": str,
        "injection_flags": list,
        "excerpt": str,
    },
    validators={
        "source_id": [not_empty(), pattern(r"^S\d+$")],
        "url": [pattern(r"^https?://")],
        "domain": [not_empty()],
        "provenance_tier": [
            one_of(
                [
                    "primary",
                    "official",
                    "established_media",
                    "aggregator",
                    "user_generated",
                    "unknown",
                ]
            )
        ],
        "freshness": [one_of(["current", "recent", "stale", "undated"])],
        "excerpt": [length(max=2000)],
    },
)

# ---------------------------------------------------------------------------
# CLAIM_RECORD — 03 §3
# ---------------------------------------------------------------------------

CLAIM_RECORD: Schema = Schema(
    {
        "claim_id": str,
        "claim_text": str,
        "claim_kind": str,
        "time_sensitivity": str,
        "supporting_source_ids": list,
        "conflicting_source_ids": list,
        "support_level": str,
        "verdict": str,
        "extracted_values": list,
        "notes": str,
    },
    validators={
        "claim_id": [not_empty(), pattern(r"^C\d+$")],
        "claim_text": [not_empty()],
        "claim_kind": [one_of(["event_outcome", "numeric", "temporal", "entity_fact", "other"])],
        "time_sensitivity": [one_of(["static", "slow_changing", "volatile"])],
        "support_level": [
            one_of(["none", "single_source", "multi_source", "independent_multi_source"])
        ],
        "verdict": [one_of(["supported", "conflicting", "insufficient", "unverifiable"])],
    },
)

# ---------------------------------------------------------------------------
# EVIDENCE_PACKET — 03 §6
# ---------------------------------------------------------------------------

EVIDENCE_PACKET: Schema = Schema(
    {
        "packet_version": str,
        "packet_id": str,
        "query": str,
        "as_of": str,
        "generated_at": str,
        "claims": list,
        "sources": list,
        "overall_verdict": str,
        "confidence": str,
        "conflicts": list,
        "warnings": list,
        "assist_used": bool,
    },
    validators={
        "packet_version": [one_of(["1.0"])],
        "packet_id": [not_empty()],
        "as_of": [not_empty(), pattern(r"^\d{4}-\d{2}-\d{2}")],
        "overall_verdict": [one_of(["verified", "conflicting", "insufficient"])],
        "confidence": [one_of(["high", "moderate", "low"])],
        "claims": [not_empty(), _each_matches(CLAIM_RECORD)],
        "sources": [_each_matches(SOURCE_RECORD)],
    },
)

# ---------------------------------------------------------------------------
# Per-step I/O contract schemas (02 §3)
# NOTE: input contracts defined here for documentation; they are NOT used as
# Step.input_contract in pipeline.py because input_contract is validated
# against ctx.inputs (dependency outputs), not against state keys.
# ---------------------------------------------------------------------------

GATE_INPUT: Schema = Schema({"documents": list})
GATE_OUTPUT: Schema = Schema({"sources": list, "rejected": list, "gate_warnings": list})

EXTRACTOR_INPUT: Schema = Schema(
    {"claims": list},
    validators={"claims": [not_empty()]},
)
EXTRACTOR_OUTPUT: Schema = Schema({"claim_records": list})

EVALUATOR_INPUT: Schema = Schema({"claim_records": list, "sources": list})

BUILDER_OUTPUT: Schema = Schema(
    {
        "working_context": str,
        "superseded_assumptions": list,
        "citations": list,
        "packet_id": str,
        "unresolved_conflicts": list,
    },
    validators={
        "working_context": [not_empty(), length(max=8000)],
    },
)


# ---------------------------------------------------------------------------
# Constructor helpers — return JSON-native dicts
# ---------------------------------------------------------------------------


def make_source_record(
    *,
    source_id: str,
    url: str,
    domain: str,
    title: str | None,
    fetched_at: str,
    published_at: str | None,
    independence_group: str,
    provenance_tier: str,
    freshness: str,
    injection_flags: list[str],
    excerpt: str,
) -> dict[str, Any]:
    """Construct a validated SourceRecord dict.

    Args:
        source_id: Citation key (e.g. "S1").
        url: Source URL.
        domain: Registrable domain (lowercased).
        title: Page title or None.
        fetched_at: ISO 8601 UTC retrieval timestamp.
        published_at: ISO 8601 publication date or None.
        independence_group: Domain-based grouping key.
        provenance_tier: Tier classification string.
        freshness: Freshness classification string.
        injection_flags: List of canonical flag names triggered by the gate.
        excerpt: Sanitized content excerpt (max 2000 chars).

    Returns:
        A JSON-native dict matching SOURCE_RECORD schema.
    """
    return {
        "source_id": source_id,
        "url": url,
        "domain": domain,
        "title": title,
        "fetched_at": fetched_at,
        "published_at": published_at,
        "independence_group": independence_group,
        "provenance_tier": provenance_tier,
        "freshness": freshness,
        "injection_flags": injection_flags,
        "excerpt": excerpt,
    }


def make_claim_record(
    *,
    claim_id: str,
    claim_text: str,
    claim_kind: str,
    time_sensitivity: str,
    supporting_source_ids: list[str] | None = None,
    conflicting_source_ids: list[str] | None = None,
    support_level: str = "none",
    verdict: str = "unverifiable",
    extracted_values: list[dict[str, str]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Construct a ClaimRecord dict skeleton.

    Args:
        claim_id: Citation key (e.g. "C1").
        claim_text: The original claim string.
        claim_kind: One of the CLAIM_RECORD claim_kind enum values.
        time_sensitivity: One of 'static', 'slow_changing', 'volatile'.
        supporting_source_ids: Source IDs that support this claim.
        conflicting_source_ids: Source IDs that conflict with this claim.
        support_level: Derived support level.
        verdict: Derived verdict.
        extracted_values: [{source_id, value}] audit trail.
        notes: Structural notes only.

    Returns:
        A JSON-native dict matching CLAIM_RECORD schema.
    """
    return {
        "claim_id": claim_id,
        "claim_text": claim_text,
        "claim_kind": claim_kind,
        "time_sensitivity": time_sensitivity,
        "supporting_source_ids": supporting_source_ids or [],
        "conflicting_source_ids": conflicting_source_ids or [],
        "support_level": support_level,
        "verdict": verdict,
        "extracted_values": extracted_values or [],
        "notes": notes,
    }


def make_packet(
    *,
    packet_id: str | None = None,
    query: str,
    as_of: str,
    generated_at: str | None = None,
    claims: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    overall_verdict: str,
    confidence: str,
    conflicts: list[dict[str, Any]],
    warnings: list[str],
    assist_used: bool = False,
) -> dict[str, Any]:
    """Construct an EvidencePacket dict.

    Args:
        packet_id: Unique run ID (auto-generated if None).
        query: The original user question.
        as_of: ISO date the evidence reflects.
        generated_at: ISO 8601 UTC timestamp (defaults to now).
        claims: List of ClaimRecord dicts.
        sources: List of SourceRecord dicts.
        overall_verdict: Packet-level verdict.
        confidence: Packet-level confidence.
        conflicts: List of conflict descriptors.
        warnings: Structural caveat strings.
        assist_used: True iff LLM-assisted matching contributed.

    Returns:
        A JSON-native dict matching EVIDENCE_PACKET schema.
    """
    return {
        "packet_version": PACKET_VERSION,
        "packet_id": packet_id or str(uuid.uuid4()),
        "query": query,
        "as_of": as_of,
        "generated_at": generated_at or datetime.now(tz=UTC).isoformat(),
        "claims": claims,
        "sources": sources,
        "overall_verdict": overall_verdict,
        "confidence": confidence,
        "conflicts": conflicts,
        "warnings": warnings,
        "assist_used": assist_used,
    }


# ---------------------------------------------------------------------------
# Deterministic derivation tables (03 §4–5)
# ---------------------------------------------------------------------------


def derive_support_level(supporting_ids: list[str], groups: dict[str, str]) -> str:
    """Derive support level from supporting source ids and their independence groups.

    03 §4: none (0) · single_source (1) · multi_source (≥2, 1 group) ·
    independent_multi_source (≥2 groups).

    Args:
        supporting_ids: Source IDs that address (support) the claim.
        groups: Mapping of source_id → independence_group key.

    Returns:
        One of 'none', 'single_source', 'multi_source', 'independent_multi_source'.
    """
    if not supporting_ids:
        return "none"
    if len(supporting_ids) == 1:
        return "single_source"
    unique_groups = {groups.get(sid, sid) for sid in supporting_ids}
    if len(unique_groups) >= 2:
        return "independent_multi_source"
    return "multi_source"


def derive_verdict(claim: dict[str, Any], sources_by_id: dict[str, dict[str, Any]]) -> str:
    """Derive per-claim verdict using the priority table from 03 §4.

    Priority order:
    1. ≥1 conflicting extracted value among non-denied sources → conflicting
    2. No source addresses the claim (no extracted_values) → unverifiable
    3. support_level is independent_multi_source, OR single/multi where every
       supporting source is tier primary/official → supported
    4. Otherwise → insufficient

    Args:
        claim: ClaimRecord dict (must have conflicting_source_ids, extracted_values,
               support_level, supporting_source_ids).
        sources_by_id: Mapping of source_id → SourceRecord dict.

    Returns:
        One of 'supported', 'conflicting', 'insufficient', 'unverifiable'.
    """
    if claim.get("conflicting_source_ids"):
        return "conflicting"

    if not claim.get("extracted_values"):
        return "unverifiable"

    support_level = claim.get("support_level", "none")

    if support_level == "independent_multi_source":
        return "supported"

    if support_level in ("single_source", "multi_source"):
        supporting_ids: list[str] = claim.get("supporting_source_ids", [])
        all_authoritative = all(
            sources_by_id.get(sid, {}).get("provenance_tier") in ("primary", "official")
            for sid in supporting_ids
        )
        if all_authoritative:
            return "supported"

    return "insufficient"


def derive_overall_verdict(claims: list[dict[str, Any]]) -> str:
    """Derive packet-level overall_verdict from claim verdicts (03 §5).

    verified = all claims supported · conflicting = any claim conflicting ·
    insufficient = otherwise.

    Args:
        claims: List of ClaimRecord dicts with 'verdict' field set.

    Returns:
        One of 'verified', 'conflicting', 'insufficient'.
    """
    if not claims:
        return "insufficient"
    if any(c.get("verdict") == "conflicting" for c in claims):
        return "conflicting"
    if all(c.get("verdict") == "supported" for c in claims):
        return "verified"
    return "insufficient"


def derive_confidence(
    claims: list[dict[str, Any]], sources_by_id: dict[str, dict[str, Any]]
) -> str:
    """Derive packet-level confidence using the table from 03 §5.

    Cap rule (EE-3): ANY supporting source with injection_flags → 'low'.

    Args:
        claims: List of ClaimRecord dicts with verdict, support_level,
                supporting_source_ids populated.
        sources_by_id: Mapping of source_id → SourceRecord dict
                       (with provenance_tier, freshness, injection_flags set).

    Returns:
        One of 'high', 'moderate', 'low'.
    """
    # Injection-flags cap: any flagged supporting source → low immediately.
    for claim in claims:
        for sid in claim.get("supporting_source_ids", []):
            source = sources_by_id.get(sid, {})
            if source.get("injection_flags"):
                return "low"

    supported = [c for c in claims if c.get("verdict") == "supported"]
    if not supported:
        return "low"

    # --- HIGH ---
    # Every supported claim: independent_multi_source, all supporting sources
    # tier ≤ established_media (rank ≤ 2), freshness ≤ recent (rank ≤ 1).
    high_ok = True
    for claim in supported:
        if claim.get("support_level") != "independent_multi_source":
            high_ok = False
            break
        for sid in claim.get("supporting_source_ids", []):
            source = sources_by_id.get(sid, {})
            tier_rank = _TIER_RANK.get(source.get("provenance_tier", "unknown"), 99)
            freshness_rank = _FRESHNESS_RANK.get(source.get("freshness", "undated"), 99)
            if tier_rank > 2 or freshness_rank > 1:  # worse than established_media or recent
                high_ok = False
                break
        if not high_ok:
            break

    if high_ok:
        return "high"

    # --- MODERATE ---
    # Every supported claim: multi_source (any tier) OR single_source where all
    # supporting sources are tier primary/official; freshness ≥ recent for all.
    moderate_ok = True
    for claim in supported:
        support_level = claim.get("support_level", "none")
        supporting_ids: list[str] = claim.get("supporting_source_ids", [])

        if support_level == "none":
            moderate_ok = False
            break

        if support_level == "single_source":
            # Single source must be primary or official
            for sid in supporting_ids:
                tier = sources_by_id.get(sid, {}).get("provenance_tier", "unknown")
                if tier not in ("primary", "official"):
                    moderate_ok = False
                    break

        # Freshness ≥ recent required for moderate
        for sid in supporting_ids:
            freshness = sources_by_id.get(sid, {}).get("freshness", "undated")
            if _FRESHNESS_RANK.get(freshness, 99) > 1:
                moderate_ok = False
                break

        if not moderate_ok:
            break

    if moderate_ok:
        return "moderate"

    return "low"
