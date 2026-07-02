"""Evidence Engine evidence_evaluator — deterministic corroboration engine (→ C3).

Enriches sources (tier, freshness, independence), evaluates each claim
against the sanitized source pool, and produces an EvidencePacket using
the derivation tables from 03 §4–5. Fully deterministic — no LLM.

EE-5: TrustPolicy is config, not state. make_evidence_evaluator closes over
a pre-validated TrustPolicy; no code path reads policy from ctx.state.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from examples.evidence_engine.content_gate import registrable_domain
from examples.evidence_engine.contracts import (
    derive_confidence,
    derive_overall_verdict,
    derive_support_level,
    derive_verdict,
    make_packet,
)
from kairos.exceptions import ConfigError
from kairos.security import sanitize_exception

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# TrustPolicy — config-time value object (EE-5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustPolicy:
    """Trust configuration for the evidence evaluator.

    Constructed from a config dict at evaluator creation time. NEVER read
    from workflow state (EE-5, T5).

    Attributes:
        pins: Domains treated as tier 'official' regardless of heuristics.
        denies: Domains excluded before derivation (never support/conflict).
        tier_overrides: Explicit domain → tier mappings.
    """

    pins: frozenset[str] = field(default_factory=frozenset)
    denies: frozenset[str] = field(default_factory=frozenset)
    tier_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> TrustPolicy:
        """Construct and validate a TrustPolicy from a config dict.

        Args:
            cfg: Optional dict with keys 'pins', 'denies', 'tier_overrides'.
                 None produces the permissive default policy.

        Returns:
            A validated TrustPolicy instance.

        Raises:
            ConfigError: If cfg is malformed (EE-5, T5).
        """
        if cfg is None:
            return cls()
        if not isinstance(cfg, dict):
            raise ConfigError(
                f"TrustPolicy config must be a dict or None, got {type(cfg).__name__}."
            )
        try:
            pins = frozenset(cfg.get("pins") or [])
            denies = frozenset(cfg.get("denies") or [])
            overrides_raw = cfg.get("tier_overrides") or {}
            if not isinstance(overrides_raw, dict):
                raise ConfigError("tier_overrides must be a dict.")
            tier_overrides: dict[str, str] = {str(k): str(v) for k, v in overrides_raw.items()}
        except ConfigError:
            raise
        except Exception as exc:
            err_type, err_msg = sanitize_exception(exc)
            raise ConfigError(f"TrustPolicy.from_config failed ({err_type}): {err_msg}") from None
        return cls(pins=pins, denies=denies, tier_overrides=tier_overrides)


# ---------------------------------------------------------------------------
# Pure classification helpers
# ---------------------------------------------------------------------------

# Tier heuristics based on TLD / registrable domain suffix.
_GOV_SUFFIXES = frozenset({"gov", "mil", "int"})
_EDU_SUFFIXES = frozenset({"edu", "ac"})  # ac.uk, edu, etc.
_ORG_SUFFIXES = frozenset({"org"})

# Date parsing pattern for freshness calculation.
_ISO_DATE_RE: re.Pattern[str] = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

# Pre-compiled patterns for value extraction (T9).
_NUMBER_RE: re.Pattern[str] = re.compile(r"\d[\d,]*\.?\d*\s*%?")


def classify_tier(source: dict[str, Any], policy: TrustPolicy) -> str:
    """Classify a source's provenance tier using structural signals + policy.

    Order: deny check → pin check → explicit override → TLD heuristic.

    Args:
        source: SourceRecord dict with 'domain' field set.
        policy: Active TrustPolicy.

    Returns:
        One of the SourceRecord.provenance_tier enum values.
    """
    domain = source.get("domain", "")

    # Denied domains stay 'unknown' (they're excluded from derivation anyway).
    if domain in policy.denies:
        return "unknown"

    # Pinned domains are treated as 'official'.
    if domain in policy.pins:
        return "official"

    # Explicit override.
    if domain in policy.tier_overrides:
        return policy.tier_overrides[domain]

    # TLD heuristic (D6: stdlib approximation).
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    if tld in _GOV_SUFFIXES:
        return "official"
    if tld in _EDU_SUFFIXES:
        return "established_media"
    if tld in _ORG_SUFFIXES:
        return "established_media"

    return "aggregator"


def classify_freshness(source: dict[str, Any], time_sensitivity: str, as_of: str) -> str:
    """Classify source freshness relative to the pipeline's as_of date.

    Prefers published_at; falls back to 'undated' when absent or unparseable.
    Thresholds: ≤1 day → current, ≤7 days → recent, >7 days → stale.

    Args:
        source: SourceRecord dict with 'published_at' field.
        time_sensitivity: Claim's time_sensitivity (unused in v1 classification;
            included for future threshold differentiation).
        as_of: ISO date string the pipeline reflects.

    Returns:
        One of 'current', 'recent', 'stale', 'undated'.
    """
    published_at = source.get("published_at")
    if not published_at:
        return "undated"

    pub_match = _ISO_DATE_RE.match(str(published_at))
    aof_match = _ISO_DATE_RE.match(str(as_of))
    if not pub_match or not aof_match:
        return "undated"

    try:
        pub_y, pub_m, pub_d = (
            int(pub_match.group(1)),
            int(pub_match.group(2)),
            int(pub_match.group(3)),
        )
        aof_y, aof_m, aof_d = (
            int(aof_match.group(1)),
            int(aof_match.group(2)),
            int(aof_match.group(3)),
        )
        # Simple day-delta calculation (sufficient for spike; ignores DST/leap)
        delta = (date(aof_y, aof_m, aof_d) - date(pub_y, pub_m, pub_d)).days
    except (ValueError, OverflowError):
        return "undated"

    if delta < 0:
        # Future-dated — treat as current (clocks may differ)
        return "current"
    if delta <= 1:
        return "current"
    if delta <= 7:
        return "recent"
    return "stale"


def assign_independence_groups(sources: list[dict[str, Any]]) -> None:
    """Set independence_group on each source to its registrable domain.

    Modifies sources in-place (evaluator works on copies, not originals).

    Args:
        sources: List of SourceRecord dicts with 'domain' and 'url' fields.
    """
    for source in sources:
        domain = source.get("domain", "")
        source["independence_group"] = domain or registrable_domain(source.get("url", ""))


def extract_values(claim: dict[str, Any], source: dict[str, Any]) -> list[str]:
    """Extract relevant values from a source's excerpt for this claim.

    Returns a list (usually 0–1 items) of short extracted strings. Returns
    empty list if the source appears unrelated to the claim.

    Args:
        claim: ClaimRecord dict with 'claim_text', 'claim_kind'.
        source: SourceRecord dict with 'excerpt'.

    Returns:
        List of extracted value strings (never raw web text beyond 60 chars).
    """
    excerpt = source.get("excerpt", "")
    if not excerpt:
        return []

    claim_text = claim.get("claim_text", "").lower()
    claim_kind = claim.get("claim_kind", "other")
    excerpt_lower = excerpt.lower()

    # Relevance check: at least one meaningful word from the claim must appear.
    claim_words = [w for w in claim_text.split() if len(w) > 3]
    if not any(w in excerpt_lower for w in claim_words):
        return []

    # For numeric claims: extract numeric tokens.
    if claim_kind == "numeric":
        nums = _NUMBER_RE.findall(excerpt)
        return [nums[0].strip()] if nums else ["(numeric value present)"]

    # For all other kinds: extract a short phrase around the first matched keyword.
    for word in sorted(claim_words, key=len, reverse=True):
        idx = excerpt_lower.find(word)
        if idx >= 0:
            start = max(0, idx - 15)
            end = min(len(excerpt), idx + 45)
            return [excerpt[start:end].strip()]

    return ["(relevant content present)"]


def detect_conflicts(claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect conflicting extracted values within a claim.

    Conflict = ≥2 extracted values that are not equivalent (after
    case-fold + strip). Returns conflict descriptor dicts; empty list
    if all sources agree or there is only one source.

    Args:
        claim: ClaimRecord dict with 'extracted_values' already populated.

    Returns:
        List of conflict descriptor dicts (may be empty).
    """
    extracted: list[dict[str, str]] = claim.get("extracted_values", [])
    if len(extracted) < 2:
        return []

    # Normalize values for comparison (case-insensitive, whitespace-stripped).
    normalized = {ev.get("value", "").strip().lower() for ev in extracted}
    if len(normalized) <= 1:
        return []

    # Values differ — conflict among all sources that provided extracted values.
    return [
        {
            "claim_id": claim.get("claim_id", ""),
            "description": "Sources disagree on extracted value",
            "source_ids": [ev.get("source_id", "") for ev in extracted],
        }
    ]


# ---------------------------------------------------------------------------
# Evaluator factory
# ---------------------------------------------------------------------------


def make_evidence_evaluator(
    trust_policy: dict[str, Any] | None = None,
) -> Callable[[StepContext], dict[str, Any]]:
    """Factory: return an evidence_evaluator step action closed over TrustPolicy.

    Validates and locks TrustPolicy at construction time — not at run time and
    NEVER from ctx.state (EE-5, T5).

    Args:
        trust_policy: Optional config dict. None = permissive default.

    Returns:
        A step action Callable[[StepContext], dict].

    Raises:
        ConfigError: If trust_policy is malformed.
    """
    policy = TrustPolicy.from_config(trust_policy)

    def evidence_evaluator(ctx: StepContext) -> dict[str, Any]:
        """Evidence evaluator step action.

        Reads claim_records, sources, query, as_of from state (F2: evaluator
        needs query + as_of for the EvidencePacket — see blueprint §8 F2).
        Produces and writes 'evidence_packet'.

        Args:
            ctx: StepContext with scoped state proxy.

        Returns:
            The EvidencePacket dict for output_contract (EVIDENCE_PACKET) validation.
        """
        claim_records_obj = ctx.state.get("claim_records")
        sources_obj = ctx.state.get("sources")
        query_obj = ctx.state.get("query")
        as_of_obj = ctx.state.get("as_of")

        claim_records: list[dict[str, Any]] = (
            list(claim_records_obj) if isinstance(claim_records_obj, list) else []
        )
        raw_sources: list[dict[str, Any]] = (
            list(sources_obj) if isinstance(sources_obj, list) else []
        )
        query: str = str(query_obj) if query_obj is not None else ""
        as_of: str = str(as_of_obj) if as_of_obj is not None else ""

        # Work on shallow copies — evaluator enriches without mutating input state.
        sources: list[dict[str, Any]] = [dict(s) for s in raw_sources]

        # Step 1: Classify tier and assign independence groups.
        assign_independence_groups(sources)
        for source in sources:
            source["provenance_tier"] = classify_tier(source, policy)

        sources_by_id: dict[str, dict[str, Any]] = {s["source_id"]: s for s in sources}

        # Collect active (non-denied) source ids.
        denied_domains = policy.denies
        active_ids: frozenset[str] = frozenset(
            sid for sid, s in sources_by_id.items() if s.get("domain", "") not in denied_domains
        )

        # Step 2: Evaluate each claim.
        updated_claims: list[dict[str, Any]] = []
        all_conflicts: list[dict[str, Any]] = []
        all_warnings: list[str] = []

        for claim in claim_records:
            claim = dict(claim)  # copy
            time_sensitivity = claim.get("time_sensitivity", "volatile")

            # Classify freshness for each active source relative to this claim.
            for source in sources:
                if source["source_id"] in active_ids:
                    source["freshness"] = classify_freshness(source, time_sensitivity, as_of)

            # Extract values from active sources only.
            extracted: list[dict[str, str]] = []
            for source in sources:
                if source["source_id"] not in active_ids:
                    continue
                vals = extract_values(claim, source)
                if vals:
                    extracted.append({"source_id": source["source_id"], "value": vals[0]})

            claim["extracted_values"] = extracted

            # Detect conflicts.
            conflict_records = detect_conflicts(claim)
            if conflict_records:
                all_conflicts.extend(conflict_records)
                conflicting_sids: set[str] = set()
                for cr in conflict_records:
                    conflicting_sids.update(cr.get("source_ids", []))
                claim["conflicting_source_ids"] = sorted(conflicting_sids)
                claim["supporting_source_ids"] = []
            else:
                claim["supporting_source_ids"] = [ev["source_id"] for ev in extracted]
                claim["conflicting_source_ids"] = []

            # Derive support level and verdict.
            groups = {s["source_id"]: s["independence_group"] for s in sources}
            claim["support_level"] = derive_support_level(claim["supporting_source_ids"], groups)
            claim["verdict"] = derive_verdict(claim, sources_by_id)

            updated_claims.append(claim)

        # Step 3: Single-independence-group warning (T4).
        if sources:
            all_groups = {s["independence_group"] for s in sources}
            if len(all_groups) == 1:
                all_warnings.append(
                    "All sources share one independence group — corroboration is limited."
                )

        # Sources with injection flags warning.
        flagged = [s["source_id"] for s in sources if s.get("injection_flags")]
        if flagged:
            all_warnings.append(
                f"{len(flagged)} source(s) carried injection flags; confidence capped at low."
            )

        # Step 4: Packet-level derivation.
        overall_verdict = derive_overall_verdict(updated_claims)
        confidence = derive_confidence(updated_claims, sources_by_id)

        packet = make_packet(
            packet_id=str(uuid.uuid4()),
            query=query,
            as_of=as_of,
            generated_at=datetime.now(tz=UTC).isoformat(),
            claims=updated_claims,
            sources=sources,
            overall_verdict=overall_verdict,
            confidence=confidence,
            conflicts=all_conflicts,
            warnings=all_warnings,
            assist_used=False,  # EE-3: no LLM assist in spike
        )

        ctx.state.set("evidence_packet", packet)
        return packet

    return evidence_evaluator
