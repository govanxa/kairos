"""Evidence Engine contracts — schemas, enums, rank tables, derivation functions, versioning.

This is the C1 production promotion of the A1 spike (examples/evidence_engine/contracts.py).
Everything downstream in the kairos-plugin-evidence package imports from here; nothing
redefines a field (02 §4).

Divergences from the spike, all resolved in favour of 03:
  1. ``_each_matches`` removed — native ``list[CLAIM_RECORD]`` / ``list[SOURCE_RECORD]``
     used instead (F1 verified in kairos/schema.py).
  2. HIGH/MODERATE tier & freshness use best-in-set (min rank), not all-in-set.
  3. ``derive_confidence`` gates high/moderate behind ``overall_verdict == verified``.
  4. ``assist_used`` / injection-flag cap retained (already matched 03 §5 + EE-3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kairos.schema import Schema
from kairos.validators import length, not_empty, one_of, pattern

# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

PACKET_VERSION: str = "1.0"
SUPPORTED_PACKET_VERSIONS: frozenset[str] = frozenset({"1.0"})


def is_supported_packet_version(version: str) -> bool:
    """Return True if *version* is in the supported packet versions set.

    Args:
        version: A packet_version string to test.

    Returns:
        True when the version is supported, False otherwise.
    """
    return version in SUPPORTED_PACKET_VERSIONS


# ---------------------------------------------------------------------------
# Vocabularies (StrEnum — Python 3.13 pattern; values are the vocabulary truth)
# ---------------------------------------------------------------------------


class ProvenanceTier(StrEnum):
    """Source provenance quality tier, ordered from most to least authoritative."""

    PRIMARY = "primary"
    OFFICIAL = "official"
    ESTABLISHED_MEDIA = "established_media"
    AGGREGATOR = "aggregator"
    USER_GENERATED = "user_generated"
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    """Source freshness classification, ordered from most to least recent."""

    CURRENT = "current"
    RECENT = "recent"
    STALE = "stale"
    UNDATED = "undated"


class ClaimKind(StrEnum):
    """Semantic kind of a claim under evaluation."""

    EVENT_OUTCOME = "event_outcome"
    NUMERIC = "numeric"
    TEMPORAL = "temporal"
    ENTITY_FACT = "entity_fact"
    OTHER = "other"


class TimeSensitivity(StrEnum):
    """How time-sensitive a claim is; drives freshness requirements."""

    STATIC = "static"
    SLOW_CHANGING = "slow_changing"
    VOLATILE = "volatile"


class SupportLevel(StrEnum):
    """Evidence support level for a single claim (03 §4)."""

    NONE = "none"
    SINGLE_SOURCE = "single_source"
    MULTI_SOURCE = "multi_source"
    INDEPENDENT_MULTI_SOURCE = "independent_multi_source"


class Verdict(StrEnum):
    """Per-claim verdict derived from the deterministic table (03 §4)."""

    SUPPORTED = "supported"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"
    UNVERIFIABLE = "unverifiable"


class OverallVerdict(StrEnum):
    """Packet-level overall verdict derived from all claim verdicts (03 §5)."""

    VERIFIED = "verified"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"


class Confidence(StrEnum):
    """Packet-level confidence derived from the deterministic table (03 §5).

    Never model-emitted, never a float — only from the auditable lookup table.
    """

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"


class InjectionFlag(StrEnum):
    """Gate-defined injection flag names (C2 produces these; values listed here for reference).

    Actual flag names used in production are defined by the content_gate (C2). These
    are the canonical example values from 03 §2. Other values may appear in injection_flags
    lists as plain strings without being members of this enum.
    """

    ROLE_MARKER = "role_marker"
    IMPERATIVE_OVERRIDE = "imperative_override"
    TOOL_CALL_SYNTAX = "tool_call_syntax"


# ---------------------------------------------------------------------------
# Ordinal rank tables (03 §4–5 quality ordering — lower rank = better quality)
# Keys are enum values so they cannot drift from the vocabulary.
# ---------------------------------------------------------------------------

_TIER_RANK: dict[str, int] = {
    ProvenanceTier.PRIMARY: 0,
    ProvenanceTier.OFFICIAL: 1,
    ProvenanceTier.ESTABLISHED_MEDIA: 2,
    ProvenanceTier.AGGREGATOR: 3,
    ProvenanceTier.USER_GENERATED: 4,
    ProvenanceTier.UNKNOWN: 5,
}

_FRESHNESS_RANK: dict[str, int] = {
    Freshness.CURRENT: 0,
    Freshness.RECENT: 1,
    Freshness.STALE: 2,
    Freshness.UNDATED: 3,
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_str_list(value: Any) -> list[str]:
    """Coerce *value* to a list[str], silently dropping non-str items.

    Used by the derive_* functions so they are genuinely total — they never
    raise on malformed caller input (wrong-typed field values, non-list inputs,
    etc.).  The conservative degradation path is the same regardless of whether
    the bad input came from a bug in the caller or from partial/truncated data.

    Args:
        value: Anything; expected to be a ``list[str]`` extracted from a record
            dict field such as ``supporting_source_ids``.

    Returns:
        A new list containing only the ``str`` items from *value*, or ``[]``
        if *value* is not a list at all.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


# ---------------------------------------------------------------------------
# Record Schemas — verbatim field names from 03 §2/§3/§6; do not rename.
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
        "provenance_tier": [one_of([t.value for t in ProvenanceTier])],
        "freshness": [one_of([f.value for f in Freshness])],
        "excerpt": [length(max=2000)],
    },
)

CLAIM_RECORD: Schema = Schema(
    {
        "claim_id": str,
        "claim_text": str,
        "claim_kind": str,
        "time_sensitivity": str,
        "supporting_source_ids": list[str],
        "conflicting_source_ids": list[str],
        "support_level": str,
        "verdict": str,
        "extracted_values": list,
        "notes": str,
    },
    validators={
        "claim_id": [not_empty(), pattern(r"^C\d+$")],
        "claim_text": [not_empty()],
        "claim_kind": [one_of([k.value for k in ClaimKind])],
        "time_sensitivity": [one_of([s.value for s in TimeSensitivity])],
        "support_level": [one_of([s.value for s in SupportLevel])],
        "verdict": [one_of([v.value for v in Verdict])],
    },
)

# EVIDENCE_PACKET uses native list[CLAIM_RECORD] / list[SOURCE_RECORD] for per-item
# structural validation (F1 — kairos/schema.py supports this natively). The spike's
# _each_matches helper is not needed and is not present in C1 (divergence #1).
EVIDENCE_PACKET: Schema = Schema(
    {
        "packet_version": str,
        "packet_id": str,
        "query": str,
        "as_of": str,
        "generated_at": str,
        "claims": list[CLAIM_RECORD],  # type: ignore[valid-type]
        "sources": list[SOURCE_RECORD],  # type: ignore[valid-type]
        "overall_verdict": str,
        "confidence": str,
        "conflicts": list,
        "warnings": list,
        "assist_used": bool,
    },
    validators={
        "packet_version": [one_of(list(SUPPORTED_PACKET_VERSIONS))],
        "packet_id": [not_empty()],
        "as_of": [not_empty(), pattern(r"^\d{4}-\d{2}-\d{2}")],
        "overall_verdict": [one_of([v.value for v in OverallVerdict])],
        "confidence": [one_of([c.value for c in Confidence])],
        "claims": [not_empty()],
        "sources": [],
    },
)

# ---------------------------------------------------------------------------
# Per-step I/O contract schemas (02 §3)
# ---------------------------------------------------------------------------

GATE_INPUT: Schema = Schema({"documents": list})
GATE_OUTPUT: Schema = Schema({"sources": list, "rejected": list, "gate_warnings": list})

EXTRACTOR_INPUT: Schema = Schema(
    {"claims": list},
    validators={"claims": [not_empty()]},
)
EXTRACTOR_OUTPUT: Schema = Schema({"claim_records": list})

EVALUATOR_INPUT: Schema = Schema({"claim_records": list, "sources": list})
# EVALUATOR_OUTPUT is the packet itself — alias, not a new schema.
EVALUATOR_OUTPUT: Schema = EVIDENCE_PACKET

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
# Constructor helpers — return JSON-native dicts; keyword-only arguments
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
    """Construct a SourceRecord dict (03 §2).

    Args:
        source_id: Citation key, e.g. ``"S1"``.
        url: Source URL (must start with ``https?://``).
        domain: Registrable domain, lowercased.
        title: Page title or None.
        fetched_at: ISO 8601 UTC retrieval timestamp.
        published_at: ISO 8601 publication date or None — None is honest.
        independence_group: Domain-based grouping key.
        provenance_tier: One of the ProvenanceTier enum values.
        freshness: One of the Freshness enum values.
        injection_flags: Canonical flag names triggered by the content gate.
        excerpt: Sanitized content excerpt — max 2000 characters.

    Returns:
        A JSON-native dict matching SOURCE_RECORD schema.
    """
    # Coerce vocabulary fields to plain str so StrEnum members pass the schema's strict
    # type(value) is str check and JSON round-trip identity holds (blueprint §2 decision 2).
    return {
        "source_id": source_id,
        "url": url,
        "domain": domain,
        "title": title,
        "fetched_at": fetched_at,
        "published_at": published_at,
        "independence_group": independence_group,
        "provenance_tier": str(provenance_tier),
        "freshness": str(freshness),
        "injection_flags": list(injection_flags),
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
    support_level: str = SupportLevel.NONE,
    verdict: str = Verdict.UNVERIFIABLE,
    extracted_values: list[dict[str, str]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Construct a ClaimRecord dict skeleton (03 §3).

    Args:
        claim_id: Citation key, e.g. ``"C1"``.
        claim_text: The original claim string.
        claim_kind: One of the ClaimKind enum values.
        time_sensitivity: One of the TimeSensitivity enum values.
        supporting_source_ids: Source IDs that support this claim.
        conflicting_source_ids: Source IDs that disagree with this claim.
        support_level: Derived support level (SupportLevel enum value).
        verdict: Derived verdict (Verdict enum value).
        extracted_values: Audit trail — ``[{source_id: str, value: str}]``.
        notes: Structural notes only; never raw web text.

    Returns:
        A JSON-native dict matching CLAIM_RECORD schema.
    """
    # Coerce vocabulary fields to plain str so StrEnum members pass the schema's strict
    # type(value) is str check and JSON round-trip identity holds (blueprint §2 decision 2).
    return {
        "claim_id": claim_id,
        "claim_text": claim_text,
        "claim_kind": str(claim_kind),
        "time_sensitivity": str(time_sensitivity),
        "supporting_source_ids": list(supporting_source_ids) if supporting_source_ids else [],
        "conflicting_source_ids": list(conflicting_source_ids) if conflicting_source_ids else [],
        "support_level": str(support_level),
        "verdict": str(verdict),
        "extracted_values": list(extracted_values) if extracted_values is not None else [],
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
    """Construct an EvidencePacket dict (03 §6).

    ``packet_version`` is always stamped ``PACKET_VERSION``.
    ``packet_id`` defaults to ``str(uuid.uuid4())``.
    ``generated_at`` defaults to ``datetime.now(tz=UTC).isoformat()``.

    Args:
        packet_id: Unique run ID; auto-generated if None.
        query: The original user question.
        as_of: ISO date the evidence reflects — the temporal anchor.
        generated_at: ISO 8601 UTC timestamp of packet creation; defaults to now.
        claims: List of ClaimRecord dicts.
        sources: List of SourceRecord dicts.
        overall_verdict: Packet-level overall verdict.
        confidence: Packet-level confidence.
        conflicts: Denormalised conflict descriptors (03 §6).
        warnings: Structural caveat strings.
        assist_used: True iff any LLM-assisted matching contributed.

    Returns:
        A JSON-native dict matching EVIDENCE_PACKET schema.
    """
    # Coerce vocabulary fields to plain str so StrEnum members pass the schema's strict
    # type(value) is str check and JSON round-trip identity holds (blueprint §2 decision 2).
    return {
        "packet_version": PACKET_VERSION,
        "packet_id": packet_id or str(uuid.uuid4()),
        "query": query,
        "as_of": as_of,
        "generated_at": generated_at or datetime.now(tz=UTC).isoformat(),
        "claims": claims,
        "sources": sources,
        "overall_verdict": str(overall_verdict),
        "confidence": str(confidence),
        "conflicts": conflicts,
        "warnings": warnings,
        "assist_used": assist_used,
    }


# ---------------------------------------------------------------------------
# Deterministic derivation functions (03 §4–5)
# All functions are total — they never raise on malformed input; they return
# the conservative value (insufficient / unverifiable / low).
# ---------------------------------------------------------------------------


def derive_support_level(supporting_ids: list[str], groups: dict[str, str]) -> str:
    """Derive support level from supporting source IDs and their independence groups.

    03 §4: none (0 sources) · single_source (1) · multi_source (≥2 sources, 1 group) ·
    independent_multi_source (≥2 independence groups).

    Unknown source IDs (not in *groups*) are each treated as their own group, so
    two unknown IDs always produce ``independent_multi_source``.

    Args:
        supporting_ids: Source IDs that address (support) the claim.
        groups: Mapping of ``source_id → independence_group`` key.

    Returns:
        One of ``"none"``, ``"single_source"``, ``"multi_source"``,
        ``"independent_multi_source"``.
    """
    safe_ids = _coerce_str_list(supporting_ids)
    safe_groups = groups if isinstance(groups, dict) else {}
    if not safe_ids:
        return SupportLevel.NONE.value
    if len(safe_ids) == 1:
        return SupportLevel.SINGLE_SOURCE.value
    unique_groups = {safe_groups.get(sid, sid) for sid in safe_ids}
    if len(unique_groups) >= 2:
        return SupportLevel.INDEPENDENT_MULTI_SOURCE.value
    return SupportLevel.MULTI_SOURCE.value


def derive_verdict(claim: dict[str, Any], sources_by_id: dict[str, dict[str, Any]]) -> str:
    """Derive per-claim verdict using the priority table from 03 §4.

    Priority order:
        1. ``conflicting_source_ids`` non-empty → ``conflicting``
        2. No ``extracted_values`` → ``unverifiable``
        3. ``support_level`` is ``independent_multi_source``, OR
           ``single_source``/``multi_source`` where every supporting source
           is tier ``primary`` or ``official`` → ``supported``
        4. Otherwise → ``insufficient``

    This function is total — missing keys default to the conservative verdict.

    Args:
        claim: ClaimRecord dict with ``conflicting_source_ids``, ``extracted_values``,
            ``support_level``, and ``supporting_source_ids``.
        sources_by_id: Mapping of ``source_id → SourceRecord`` dict.

    Returns:
        One of ``"supported"``, ``"conflicting"``, ``"insufficient"``, ``"unverifiable"``.
    """
    if not isinstance(claim, dict):
        return Verdict.UNVERIFIABLE.value
    safe_sources = sources_by_id if isinstance(sources_by_id, dict) else {}

    # Priority 1 — conflicting evidence
    if claim.get("conflicting_source_ids"):
        return Verdict.CONFLICTING.value

    # Priority 2 — no evidence addresses the claim
    if not claim.get("extracted_values"):
        return Verdict.UNVERIFIABLE.value

    support_level = claim.get("support_level", SupportLevel.NONE)

    # Priority 3a — independent multi-source (strongest evidence)
    if support_level == SupportLevel.INDEPENDENT_MULTI_SOURCE:
        return Verdict.SUPPORTED.value

    # Priority 3b — single/multi where every supporting source is primary/official
    if support_level in (SupportLevel.SINGLE_SOURCE, SupportLevel.MULTI_SOURCE):
        supporting_ids = _coerce_str_list(claim.get("supporting_source_ids", []))
        authoritative_tiers = {ProvenanceTier.PRIMARY, ProvenanceTier.OFFICIAL}
        all_authoritative = all(
            safe_sources.get(sid, {}).get("provenance_tier") in authoritative_tiers
            for sid in supporting_ids
        )
        if all_authoritative and supporting_ids:
            return Verdict.SUPPORTED.value

    # Priority 4 — insufficient evidence
    return Verdict.INSUFFICIENT.value


def derive_overall_verdict(claims: list[dict[str, Any]]) -> str:
    """Derive packet-level overall_verdict from claim verdicts (03 §5).

    ``verified`` = all claims ``supported`` ·
    ``conflicting`` = any claim ``conflicting`` ·
    ``insufficient`` = otherwise (including empty claims list).

    Args:
        claims: List of ClaimRecord dicts with ``verdict`` field set.

    Returns:
        One of ``"verified"``, ``"conflicting"``, ``"insufficient"``.
    """
    if not isinstance(claims, list):
        return OverallVerdict.INSUFFICIENT.value
    safe_claims = [c for c in claims if isinstance(c, dict)]
    if not safe_claims:
        return OverallVerdict.INSUFFICIENT.value
    if any(c.get("verdict") == Verdict.CONFLICTING for c in safe_claims):
        return OverallVerdict.CONFLICTING.value
    if all(c.get("verdict") == Verdict.SUPPORTED for c in safe_claims):
        return OverallVerdict.VERIFIED.value
    return OverallVerdict.INSUFFICIENT.value


def derive_confidence(
    claims: list[dict[str, Any]],
    sources_by_id: dict[str, dict[str, Any]],
) -> str:
    """Derive packet-level confidence using the deterministic table from 03 §5.

    Resolution order (divergence #3 from the spike — verified gate added):
        1. **EE-3 injection cap:** any supporting source (any claim) with non-empty
           ``injection_flags`` → ``"low"``, unconditionally.
        2. **Verified gate:** ``derive_overall_verdict(claims) != "verified"`` → ``"low"``.
           High/moderate apply only when the overall verdict is verified.
        3. **HIGH:** every claim ``supported`` with ``independent_multi_source``,
           best (min rank) tier among supporting sources ≤ rank 2 (≥ ``established_media``),
           best (min rank) freshness ≤ rank 1 (≥ ``recent``) → ``"high"``.
        4. **MODERATE:** every claim ``supported`` via ``multi_source`` (any tiers) OR
           ``single_source`` with source tier ∈ {``primary``, ``official``},
           and best freshness ≤ rank 1 → ``"moderate"``.
        5. else ``"low"``.

    Divergence from spike (#2): HIGH/MODERATE use best-in-set (min rank) rather than
    all-in-set; one high-quality source is sufficient to meet the tier/freshness bar.

    Args:
        claims: List of ClaimRecord dicts with verdict, support_level, and
            supporting_source_ids populated.
        sources_by_id: Mapping of ``source_id → SourceRecord`` dict with
            provenance_tier, freshness, and injection_flags.

    Returns:
        One of ``"high"``, ``"moderate"``, ``"low"``.
    """
    safe_claims = claims if isinstance(claims, list) else []
    safe_sources = sources_by_id if isinstance(sources_by_id, dict) else {}
    dict_claims = [c for c in safe_claims if isinstance(c, dict)]

    # Step 1 — EE-3 injection-flags cap
    for claim in dict_claims:
        for sid in _coerce_str_list(claim.get("supporting_source_ids", [])):
            source = safe_sources.get(sid, {})
            if source.get("injection_flags"):
                return Confidence.LOW.value

    # Step 2 — Verified gate (high/moderate only apply to verified packets)
    if derive_overall_verdict(dict_claims) != OverallVerdict.VERIFIED:
        return Confidence.LOW.value

    # After the verified gate every claim is supported; collect them.
    supported = [c for c in dict_claims if c.get("verdict") == Verdict.SUPPORTED]
    if not supported:  # pragma: no cover
        # Defensive: cannot happen after the verified gate (verified ↔ all supported),
        # but kept for total-function safety.
        return Confidence.LOW.value

    # Step 3 — HIGH
    # Every claim: independent_multi_source, best tier ≥ established_media (rank ≤ 2),
    # best freshness ≥ recent (rank ≤ 1). "Best" = min rank across supporting sources.
    high_ok = True
    for claim in supported:
        if claim.get("support_level") != SupportLevel.INDEPENDENT_MULTI_SOURCE:
            high_ok = False
            break
        sids = _coerce_str_list(claim.get("supporting_source_ids", []))
        if not sids:
            high_ok = False
            break
        best_tier = min(
            _TIER_RANK.get(safe_sources.get(sid, {}).get("provenance_tier", "unknown"), 99)
            for sid in sids
        )
        best_freshness = min(
            _FRESHNESS_RANK.get(safe_sources.get(sid, {}).get("freshness", "undated"), 99)
            for sid in sids
        )
        if best_tier > 2 or best_freshness > 1:  # worse than established_media / recent
            high_ok = False
            break

    if high_ok:
        return Confidence.HIGH.value

    # Step 4 — MODERATE
    # Every claim: multi_source (any tiers) OR single_source with primary/official tier,
    # and best freshness ≥ recent (rank ≤ 1).
    moderate_ok = True
    for claim in supported:
        support_level = claim.get("support_level", SupportLevel.NONE)
        sids = _coerce_str_list(claim.get("supporting_source_ids", []))

        if support_level == SupportLevel.NONE:
            moderate_ok = False
            break

        if support_level == SupportLevel.SINGLE_SOURCE:
            authoritative_tiers = {ProvenanceTier.PRIMARY, ProvenanceTier.OFFICIAL}
            for sid in sids:
                tier = safe_sources.get(sid, {}).get("provenance_tier", "unknown")
                if tier not in authoritative_tiers:
                    moderate_ok = False
                    break
            if not moderate_ok:
                break

        # For multi_source: any tier is acceptable — only freshness is gated.
        # For independent_multi_source that fell through from HIGH: not eligible for MODERATE.
        if support_level == SupportLevel.INDEPENDENT_MULTI_SOURCE:
            moderate_ok = False
            break

        if not sids:
            moderate_ok = False
            break
        best_freshness = min(
            _FRESHNESS_RANK.get(safe_sources.get(sid, {}).get("freshness", "undated"), 99)
            for sid in sids
        )
        if best_freshness > 1:  # worse than recent
            moderate_ok = False
            break

    if moderate_ok:
        return Confidence.MODERATE.value

    # Step 5 — LOW (anything else, or verified but not meeting HIGH or MODERATE)
    return Confidence.LOW.value
