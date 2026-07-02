"""Shared fixtures for kairos-plugin-evidence test suite (C1).

Fixtures span ≥3 claim_kinds (numeric, temporal, entity_fact, event_outcome).
Only one fixture family may be event-outcome related (generality rule — 07).
No sports or World-Cup naming anywhere in this file.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairos_plugin_evidence.contracts import (
    ClaimKind,
    Confidence,
    Freshness,
    OverallVerdict,
    ProvenanceTier,
    SupportLevel,
    TimeSensitivity,
    Verdict,
    make_claim_record,
    make_packet,
    make_source_record,
)

# ---------------------------------------------------------------------------
# Source record fixtures — diverse tiers and freshness for derivation tests
# ---------------------------------------------------------------------------


@pytest.fixture
def src_primary_current() -> dict[str, Any]:
    """Primary-tier, current-freshness source with no injection flags."""
    return make_source_record(
        source_id="S1",
        url="https://stats.example.gov/co2",
        domain="stats.example.gov",
        title="Atmospheric CO2 Monitoring Report 2025",
        fetched_at="2025-06-01T10:00:00Z",
        published_at="2025-05-31T00:00:00Z",
        independence_group="stats.example.gov",
        provenance_tier=ProvenanceTier.PRIMARY,
        freshness=Freshness.CURRENT,
        injection_flags=[],
        excerpt="Atmospheric CO2 concentration reached 421 ppm in May 2025.",
    )


@pytest.fixture
def src_official_recent() -> dict[str, Any]:
    """Official-tier, recent-freshness source with no injection flags."""
    return make_source_record(
        source_id="S2",
        url="https://agency.example.org/report",
        domain="agency.example.org",
        title="Annual Climate Agency Report",
        fetched_at="2025-06-01T10:00:00Z",
        published_at="2025-03-15T00:00:00Z",
        independence_group="agency.example.org",
        provenance_tier=ProvenanceTier.OFFICIAL,
        freshness=Freshness.RECENT,
        injection_flags=[],
        excerpt="CO2 levels are at 421 ppm according to monitoring stations.",
    )


@pytest.fixture
def src_established_media_recent() -> dict[str, Any]:
    """Established-media-tier, recent-freshness source with no injection flags."""
    return make_source_record(
        source_id="S3",
        url="https://news.example.com/science/co2",
        domain="news.example.com",
        title="Global CO2 Hits New Record",
        fetched_at="2025-06-01T10:00:00Z",
        published_at="2025-04-20T00:00:00Z",
        independence_group="news.example.com",
        provenance_tier=ProvenanceTier.ESTABLISHED_MEDIA,
        freshness=Freshness.RECENT,
        injection_flags=[],
        excerpt="Scientists report CO2 at 421 ppm, a new annual high.",
    )


@pytest.fixture
def src_aggregator_stale() -> dict[str, Any]:
    """Aggregator-tier, stale-freshness source with no injection flags."""
    return make_source_record(
        source_id="S4",
        url="https://aggregator.example.io/data",
        domain="aggregator.example.io",
        title="Climate Data Roundup",
        fetched_at="2025-06-01T10:00:00Z",
        published_at="2024-01-10T00:00:00Z",
        independence_group="aggregator.example.io",
        provenance_tier=ProvenanceTier.AGGREGATOR,
        freshness=Freshness.STALE,
        injection_flags=[],
        excerpt="CO2 was approximately 419 ppm as of early 2024.",
    )


@pytest.fixture
def src_with_injection_flag() -> dict[str, Any]:
    """Primary-tier source that carries an injection flag."""
    return make_source_record(
        source_id="S5",
        url="https://flagged.example.com/data",
        domain="flagged.example.com",
        title="Flagged Source",
        fetched_at="2025-06-01T10:00:00Z",
        published_at="2025-05-01T00:00:00Z",
        independence_group="flagged.example.com",
        provenance_tier=ProvenanceTier.PRIMARY,
        freshness=Freshness.CURRENT,
        injection_flags=["role_marker"],
        excerpt="Some content with a detected injection pattern.",
    )


# ---------------------------------------------------------------------------
# Claim record fixtures — diverse claim kinds
# ---------------------------------------------------------------------------


@pytest.fixture
def claim_numeric_supported_independent(
    src_primary_current: dict[str, Any],
    src_official_recent: dict[str, Any],
) -> dict[str, Any]:
    """Numeric claim supported by independent multi-source evidence."""
    return make_claim_record(
        claim_id="C1",
        claim_text="Atmospheric CO2 concentration is 421 ppm.",
        claim_kind=ClaimKind.NUMERIC,
        time_sensitivity=TimeSensitivity.VOLATILE,
        supporting_source_ids=["S1", "S2"],
        conflicting_source_ids=[],
        support_level=SupportLevel.INDEPENDENT_MULTI_SOURCE,
        verdict=Verdict.SUPPORTED,
        extracted_values=[
            {"source_id": "S1", "value": "421 ppm"},
            {"source_id": "S2", "value": "421 ppm"},
        ],
        notes="Two independent authoritative sources agree.",
    )


@pytest.fixture
def claim_temporal_single_source() -> dict[str, Any]:
    """Temporal claim supported by a single official source."""
    return make_claim_record(
        claim_id="C2",
        claim_text="The Eiffel Tower was inaugurated on March 31, 1889.",
        claim_kind=ClaimKind.TEMPORAL,
        time_sensitivity=TimeSensitivity.STATIC,
        supporting_source_ids=["S2"],
        conflicting_source_ids=[],
        support_level=SupportLevel.SINGLE_SOURCE,
        verdict=Verdict.SUPPORTED,
        extracted_values=[{"source_id": "S2", "value": "March 31, 1889"}],
        notes="",
    )


@pytest.fixture
def claim_entity_fact_multi_source() -> dict[str, Any]:
    """Entity-fact claim supported by multi-source evidence (same independence group)."""
    return make_claim_record(
        claim_id="C3",
        claim_text="Python programming language was created by Guido van Rossum.",
        claim_kind=ClaimKind.ENTITY_FACT,
        time_sensitivity=TimeSensitivity.STATIC,
        supporting_source_ids=["S1", "S3"],
        conflicting_source_ids=[],
        support_level=SupportLevel.MULTI_SOURCE,
        verdict=Verdict.SUPPORTED,
        extracted_values=[
            {"source_id": "S1", "value": "Guido van Rossum"},
            {"source_id": "S3", "value": "Guido van Rossum"},
        ],
        notes="",
    )


@pytest.fixture
def claim_event_outcome_insufficient() -> dict[str, Any]:
    """Event-outcome claim with insufficient evidence (aggregator source only)."""
    return make_claim_record(
        claim_id="C4",
        claim_text="The annual developer tools award was won by the Kairos project.",
        claim_kind=ClaimKind.EVENT_OUTCOME,
        time_sensitivity=TimeSensitivity.SLOW_CHANGING,
        supporting_source_ids=["S4"],
        conflicting_source_ids=[],
        support_level=SupportLevel.SINGLE_SOURCE,
        verdict=Verdict.INSUFFICIENT,
        extracted_values=[{"source_id": "S4", "value": "Kairos"}],
        notes="Aggregator source only; no authoritative confirmation.",
    )


# ---------------------------------------------------------------------------
# Full packet fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_packet_verified_high(
    claim_numeric_supported_independent: dict[str, Any],
    src_primary_current: dict[str, Any],
    src_official_recent: dict[str, Any],
) -> dict[str, Any]:
    """A fully valid EvidencePacket with overall_verdict=verified and confidence=high."""
    return make_packet(
        packet_id="PKT-001",
        query="What is the current atmospheric CO2 concentration?",
        as_of="2025-06-01",
        generated_at="2025-06-01T12:00:00Z",
        claims=[claim_numeric_supported_independent],
        sources=[src_primary_current, src_official_recent],
        overall_verdict=OverallVerdict.VERIFIED,
        confidence=Confidence.HIGH,
        conflicts=[],
        warnings=[],
        assist_used=False,
    )


@pytest.fixture
def valid_packet_insufficient(
    claim_event_outcome_insufficient: dict[str, Any],
    src_aggregator_stale: dict[str, Any],
) -> dict[str, Any]:
    """A valid EvidencePacket with overall_verdict=insufficient and confidence=low."""
    return make_packet(
        packet_id="PKT-002",
        query="Who won the annual developer tools award?",
        as_of="2025-06-01",
        generated_at="2025-06-01T12:00:00Z",
        claims=[claim_event_outcome_insufficient],
        sources=[src_aggregator_stale],
        overall_verdict=OverallVerdict.INSUFFICIENT,
        confidence=Confidence.LOW,
        conflicts=[],
        warnings=["Single aggregator source — insufficient to verify."],
        assist_used=False,
    )


@pytest.fixture
def sources_by_id_high(
    src_primary_current: dict[str, Any],
    src_official_recent: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """sources_by_id map for high-confidence derivation tests."""
    return {"S1": src_primary_current, "S2": src_official_recent}


@pytest.fixture
def sources_by_id_with_flag(
    src_primary_current: dict[str, Any],
    src_with_injection_flag: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """sources_by_id map containing a flagged source."""
    return {"S1": src_primary_current, "S5": src_with_injection_flag}
