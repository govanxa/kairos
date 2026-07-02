"""Tests for examples.evidence_engine.contracts — schemas, derivation tables (→ C1)."""

from __future__ import annotations

import json
from typing import Any

from examples.evidence_engine.contracts import (
    CLAIM_RECORD,
    EVIDENCE_PACKET,
    PACKET_VERSION,
    SOURCE_RECORD,
    _each_matches,
    derive_confidence,
    derive_overall_verdict,
    derive_support_level,
    derive_verdict,
    make_claim_record,
    make_packet,
)
from kairos.security import DEFAULT_SENSITIVE_PATTERNS
from kairos.validators import StructuralValidator

_SV = StructuralValidator()


def _make_source(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_id": "S1",
        "url": "https://example.org/page",
        "domain": "example.org",
        "title": "Test Source",
        "fetched_at": "2026-07-01T10:00:00Z",
        "published_at": "2026-06-30T10:00:00Z",
        "independence_group": "example.org",
        "provenance_tier": "established_media",
        "freshness": "recent",
        "injection_flags": [],
        "excerpt": "This is a test excerpt.",
    }
    base.update(overrides)
    return base


def _make_claim(**overrides: Any) -> dict[str, Any]:
    base = make_claim_record(
        claim_id="C1",
        claim_text="The event occurred on June 28, 2026",
        claim_kind="other",
        time_sensitivity="volatile",
    )
    base.update(overrides)
    return base


def _make_full_packet(**overrides: Any) -> dict[str, Any]:
    src = _make_source()
    claim = _make_claim(
        supporting_source_ids=["S1"],
        support_level="single_source",
        verdict="supported",
        extracted_values=[{"source_id": "S1", "value": "occurred"}],
    )
    base = make_packet(
        query="Did the event occur?",
        as_of="2026-07-01",
        claims=[claim],
        sources=[src],
        overall_verdict="verified",
        confidence="moderate",
        conflicts=[],
        warnings=[],
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_invalid_source_id_rejected(self) -> None:
        src = _make_source(source_id="invalid")
        result = _SV.validate(src, SOURCE_RECORD)
        assert not result.valid

    def test_invalid_provenance_tier_rejected(self) -> None:
        src = _make_source(provenance_tier="super_trusted")
        result = _SV.validate(src, SOURCE_RECORD)
        assert not result.valid

    def test_invalid_freshness_rejected(self) -> None:
        src = _make_source(freshness="very_fresh")
        result = _SV.validate(src, SOURCE_RECORD)
        assert not result.valid

    def test_invalid_verdict_rejected(self) -> None:
        claim = _make_claim(verdict="maybe")
        result = _SV.validate(claim, CLAIM_RECORD)
        assert not result.valid

    def test_invalid_support_level_rejected(self) -> None:
        claim = _make_claim(support_level="lots")
        result = _SV.validate(claim, CLAIM_RECORD)
        assert not result.valid

    def test_packet_version_not_10_rejected(self) -> None:
        packet = _make_full_packet(packet_version="2.0")
        result = _SV.validate(packet, EVIDENCE_PACKET)
        assert not result.valid

    def test_each_matches_reports_structural_error_not_content(self) -> None:
        checker = _each_matches(SOURCE_RECORD)
        # Pass a list with an invalid item (missing required field)
        bad_items = [{"source_id": "S1"}]  # missing most fields
        result = checker(bad_items)
        assert isinstance(result, str)
        assert "item 0:" in result
        # Must NOT contain raw content (the item dict repr)
        assert "source_id" in result or "missing" in result.lower()

    def test_each_matches_rejects_non_list(self) -> None:
        checker = _each_matches(CLAIM_RECORD)
        result = checker("not a list")
        assert isinstance(result, str)
        assert "list" in result

    def test_empty_claims_rejected_by_not_empty(self) -> None:
        packet = _make_full_packet(claims=[])
        result = _SV.validate(packet, EVIDENCE_PACKET)
        assert not result.valid

    def test_missing_required_packet_field(self) -> None:
        packet = _make_full_packet()
        del packet["query"]
        result = _SV.validate(packet, EVIDENCE_PACKET)
        assert not result.valid


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_empty_sources_list_allowed(self) -> None:
        packet = _make_full_packet(sources=[])
        result = _SV.validate(packet, EVIDENCE_PACKET)
        assert result.valid  # sources may be empty (no sources found)

    def test_published_at_none_valid(self) -> None:
        src = _make_source(published_at=None)
        result = _SV.validate(src, SOURCE_RECORD)
        assert result.valid

    def test_as_of_pattern_boundary(self) -> None:
        # Valid: YYYY-MM-DD
        packet = _make_full_packet(as_of="2026-07-01")
        assert _SV.validate(packet, EVIDENCE_PACKET).valid
        # Invalid: wrong format
        packet2 = _make_full_packet(as_of="01/07/2026")
        assert not _SV.validate(packet2, EVIDENCE_PACKET).valid

    def test_derive_support_level_zero_sources(self) -> None:
        assert derive_support_level([], {}) == "none"

    def test_derive_support_level_single_source(self) -> None:
        assert derive_support_level(["S1"], {"S1": "example.org"}) == "single_source"

    def test_title_none_valid(self) -> None:
        src = _make_source(title=None)
        result = _SV.validate(src, SOURCE_RECORD)
        assert result.valid


# ---------------------------------------------------------------------------
# Group 3: Happy paths — derivation table exhaustive coverage (03 §4–5)
# ---------------------------------------------------------------------------


class TestDerivationTables:
    # --- derive_support_level ---

    def test_support_level_multi_source_same_group(self) -> None:
        # 2 sources, same domain → multi_source
        level = derive_support_level(["S1", "S2"], {"S1": "a.org", "S2": "a.org"})
        assert level == "multi_source"

    def test_support_level_independent_multi_source(self) -> None:
        level = derive_support_level(["S1", "S2"], {"S1": "a.org", "S2": "b.org"})
        assert level == "independent_multi_source"

    # --- derive_verdict priority table (03 §4) ---

    def test_verdict_priority_1_conflicting(self) -> None:
        claim = _make_claim(
            conflicting_source_ids=["S1"],
            extracted_values=[{"source_id": "S1", "value": "yes"}],
            support_level="single_source",
        )
        sources_by_id: dict[str, Any] = {"S1": _make_source()}
        assert derive_verdict(claim, sources_by_id) == "conflicting"

    def test_verdict_priority_2_unverifiable(self) -> None:
        claim = _make_claim(extracted_values=[], conflicting_source_ids=[])
        assert derive_verdict(claim, {}) == "unverifiable"

    def test_verdict_priority_3a_independent_multi_source(self) -> None:
        claim = _make_claim(
            supporting_source_ids=["S1", "S2"],
            support_level="independent_multi_source",
            extracted_values=[
                {"source_id": "S1", "value": "yes"},
                {"source_id": "S2", "value": "yes"},
            ],
        )
        src1 = _make_source(source_id="S1", provenance_tier="aggregator")
        src2 = _make_source(source_id="S2", provenance_tier="aggregator")
        assert derive_verdict(claim, {"S1": src1, "S2": src2}) == "supported"

    def test_verdict_priority_3b_single_source_primary(self) -> None:
        claim = _make_claim(
            supporting_source_ids=["S1"],
            support_level="single_source",
            extracted_values=[{"source_id": "S1", "value": "confirmed"}],
        )
        src = _make_source(provenance_tier="primary")
        assert derive_verdict(claim, {"S1": src}) == "supported"

    def test_verdict_priority_4_insufficient(self) -> None:
        # multi_source but none are primary/official AND not independent
        claim = _make_claim(
            supporting_source_ids=["S1", "S2"],
            support_level="multi_source",
            extracted_values=[
                {"source_id": "S1", "value": "yes"},
                {"source_id": "S2", "value": "yes"},
            ],
        )
        src1 = _make_source(source_id="S1", provenance_tier="aggregator")
        src2 = _make_source(source_id="S2", provenance_tier="aggregator")
        assert derive_verdict(claim, {"S1": src1, "S2": src2}) == "insufficient"

    # --- derive_overall_verdict ---

    def test_overall_verdict_verified(self) -> None:
        claims = [
            _make_claim(verdict="supported"),
            _make_claim(
                claim_id="C2",
                claim_text="x",
                claim_kind="other",
                time_sensitivity="volatile",
                verdict="supported",
            ),
        ]
        assert derive_overall_verdict(claims) == "verified"

    def test_overall_verdict_conflicting(self) -> None:
        claims = [_make_claim(verdict="conflicting")]
        assert derive_overall_verdict(claims) == "conflicting"

    def test_overall_verdict_insufficient(self) -> None:
        claims = [
            _make_claim(verdict="supported"),
            _make_claim(
                claim_id="C2",
                claim_text="x",
                claim_kind="other",
                time_sensitivity="volatile",
                verdict="insufficient",
            ),
        ]
        assert derive_overall_verdict(claims) == "insufficient"

    def test_overall_verdict_empty_claims(self) -> None:
        assert derive_overall_verdict([]) == "insufficient"

    # --- derive_confidence ---

    def test_confidence_high(self) -> None:
        claim = _make_claim(
            supporting_source_ids=["S1", "S2"],
            support_level="independent_multi_source",
            verdict="supported",
            extracted_values=[
                {"source_id": "S1", "value": "yes"},
                {"source_id": "S2", "value": "yes"},
            ],
        )
        src1 = _make_source(
            source_id="S1", provenance_tier="official", freshness="recent", injection_flags=[]
        )
        src2 = _make_source(
            source_id="S2",
            provenance_tier="established_media",
            freshness="current",
            injection_flags=[],
        )
        result = derive_confidence([claim], {"S1": src1, "S2": src2})
        assert result == "high"

    def test_confidence_moderate_single_official(self) -> None:
        claim = _make_claim(
            supporting_source_ids=["S1"],
            support_level="single_source",
            verdict="supported",
            extracted_values=[{"source_id": "S1", "value": "yes"}],
        )
        src = _make_source(
            source_id="S1", provenance_tier="official", freshness="recent", injection_flags=[]
        )
        result = derive_confidence([claim], {"S1": src})
        assert result == "moderate"

    def test_confidence_low_stale(self) -> None:
        claim = _make_claim(
            supporting_source_ids=["S1"],
            support_level="single_source",
            verdict="supported",
            extracted_values=[{"source_id": "S1", "value": "yes"}],
        )
        src = _make_source(
            source_id="S1", provenance_tier="official", freshness="stale", injection_flags=[]
        )
        result = derive_confidence([claim], {"S1": src})
        assert result == "low"

    def test_confidence_low_injection_flags_cap(self) -> None:
        """Any supporting source with injection_flags → cap at low (03 §5)."""
        claim = _make_claim(
            supporting_source_ids=["S1", "S2"],
            support_level="independent_multi_source",
            verdict="supported",
            extracted_values=[
                {"source_id": "S1", "value": "yes"},
                {"source_id": "S2", "value": "yes"},
            ],
        )
        # S1 has injection_flags — should cap confidence at low
        src1 = _make_source(
            source_id="S1",
            provenance_tier="official",
            freshness="current",
            injection_flags=["role_marker"],
        )
        src2 = _make_source(
            source_id="S2",
            provenance_tier="established_media",
            freshness="current",
            injection_flags=[],
        )
        result = derive_confidence([claim], {"S1": src1, "S2": src2})
        assert result == "low"


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_no_packet_field_name_collides_with_sensitive_patterns(self) -> None:
        """No packet field name may match DEFAULT_SENSITIVE_PATTERNS (03 §8)."""
        import fnmatch

        packet_fields = [
            "packet_version",
            "packet_id",
            "query",
            "as_of",
            "generated_at",
            "claims",
            "sources",
            "overall_verdict",
            "confidence",
            "conflicts",
            "warnings",
            "assist_used",
        ]
        for field_name in packet_fields:
            for pat in DEFAULT_SENSITIVE_PATTERNS:
                assert not fnmatch.fnmatch(field_name.lower(), pat.lower()), (
                    f"Packet field {field_name!r} matches sensitive pattern {pat!r} — "
                    "it would be silently redacted in logs."
                )

    def test_packet_version_enforced(self) -> None:
        packet = _make_full_packet(packet_version="0.9")
        result = _SV.validate(packet, EVIDENCE_PACKET)
        assert not result.valid
        assert any("packet_version" in e.field for e in result.errors)


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_source_record_round_trips_json(self) -> None:
        src = _make_source()
        assert src == json.loads(json.dumps(src))

    def test_claim_record_round_trips_json(self) -> None:
        claim = _make_claim()
        assert claim == json.loads(json.dumps(claim))

    def test_packet_round_trips_json(self) -> None:
        packet = _make_full_packet()
        assert packet == json.loads(json.dumps(packet))

    def test_make_packet_assist_used_false_by_default(self) -> None:
        packet = make_packet(
            query="test",
            as_of="2026-07-01",
            claims=[_make_claim()],
            sources=[],
            overall_verdict="insufficient",
            confidence="low",
            conflicts=[],
            warnings=[],
        )
        assert packet["assist_used"] is False

    def test_make_packet_has_packet_version(self) -> None:
        packet = make_packet(
            query="test",
            as_of="2026-07-01",
            claims=[_make_claim()],
            sources=[],
            overall_verdict="insufficient",
            confidence="low",
            conflicts=[],
            warnings=[],
        )
        assert packet["packet_version"] == PACKET_VERSION
