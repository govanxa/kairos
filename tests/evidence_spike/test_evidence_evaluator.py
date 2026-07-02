"""Tests for examples.evidence_engine.evidence_evaluator (→ C3)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from examples.evidence_engine.evidence_evaluator import (
    TrustPolicy,
    assign_independence_groups,
    classify_freshness,
    classify_tier,
    detect_conflicts,
    extract_values,
    make_evidence_evaluator,
)
from kairos.exceptions import ConfigError
from tests.evidence_spike.conftest import _FakeCtx, _FakeProxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_id": "S1",
        "url": "https://example.org/page",
        "domain": "example.org",
        "title": "Test",
        "fetched_at": "2026-07-01T10:00:00Z",
        "published_at": "2026-06-30T10:00:00Z",
        "independence_group": "example.org",
        "provenance_tier": "unknown",
        "freshness": "undated",
        "injection_flags": [],
        "excerpt": "The climate accord was ratified by all member states.",
    }
    base.update(overrides)
    return base


def _claim(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "C1",
        "claim_text": "The climate accord was ratified.",
        "claim_kind": "other",
        "time_sensitivity": "volatile",
        "supporting_source_ids": [],
        "conflicting_source_ids": [],
        "support_level": "none",
        "verdict": "unverifiable",
        "extracted_values": [],
        "notes": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_malformed_trust_policy_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            make_evidence_evaluator(trust_policy="not_a_dict")  # type: ignore[arg-type]

    def test_malformed_tier_overrides_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            make_evidence_evaluator(trust_policy={"tier_overrides": "bad"})

    def test_empty_sources_produces_unverifiable(self) -> None:
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [],
                "query": "Was the accord ratified?",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        assert packet["overall_verdict"] in {"insufficient", "conflicting"}
        assert packet["claims"][0]["verdict"] == "unverifiable"

    def test_empty_claims_records(self) -> None:
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [],
                "sources": [_source()],
                "query": "Test",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        # No claims → overall_verdict insufficient
        assert packet["overall_verdict"] == "insufficient"

    def test_detect_conflicts_empty_extracted_values(self) -> None:
        claim = _claim(extracted_values=[])
        assert detect_conflicts(claim) == []

    def test_detect_conflicts_single_source_no_conflict(self) -> None:
        claim = _claim(extracted_values=[{"source_id": "S1", "value": "ratified"}])
        assert detect_conflicts(claim) == []


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_trust_policy_none_is_permissive(self) -> None:
        policy = TrustPolicy.from_config(None)
        assert policy.pins == frozenset()
        assert policy.denies == frozenset()
        assert policy.tier_overrides == {}

    def test_classify_freshness_no_published_at(self) -> None:
        src = _source(published_at=None)
        assert classify_freshness(src, "volatile", "2026-07-01") == "undated"

    def test_classify_freshness_future_dated(self) -> None:
        src = _source(published_at="2026-08-01")
        assert classify_freshness(src, "volatile", "2026-07-01") == "current"

    def test_classify_freshness_same_day(self) -> None:
        src = _source(published_at="2026-07-01")
        assert classify_freshness(src, "volatile", "2026-07-01") == "current"

    def test_classify_freshness_stale(self) -> None:
        src = _source(published_at="2026-06-01")
        assert classify_freshness(src, "volatile", "2026-07-01") == "stale"

    def test_classify_freshness_recent(self) -> None:
        src = _source(published_at="2026-06-25")
        assert classify_freshness(src, "volatile", "2026-07-01") == "recent"

    def test_assign_independence_groups_sets_domain(self) -> None:
        sources = [_source(source_id="S1"), _source(source_id="S2", domain="other.org")]
        assign_independence_groups(sources)
        assert sources[0]["independence_group"] == "example.org"
        assert sources[1]["independence_group"] == "other.org"

    def test_extract_values_unrelated_excerpt_returns_empty(self) -> None:
        claim = _claim(claim_text="The accord was ratified.")
        src = _source(excerpt="Unrelated content about something else entirely.")
        result = extract_values(claim, src)
        assert result == []

    def test_extract_values_empty_excerpt_returns_empty(self) -> None:
        claim = _claim()
        src = _source(excerpt="")
        assert extract_values(claim, src) == []


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_classify_tier_gov_tld(self) -> None:
        policy = TrustPolicy.from_config(None)
        src = _source(domain="data.gov")
        assert classify_tier(src, policy) == "official"

    def test_classify_tier_org_tld(self) -> None:
        policy = TrustPolicy.from_config(None)
        src = _source(domain="example.org")
        assert classify_tier(src, policy) == "established_media"

    def test_classify_tier_net_tld(self) -> None:
        policy = TrustPolicy.from_config(None)
        src = _source(domain="news.net")
        assert classify_tier(src, policy) == "aggregator"

    def test_classify_tier_pinned_domain(self) -> None:
        policy = TrustPolicy.from_config({"pins": ["example.net"]})
        src = _source(domain="example.net")
        assert classify_tier(src, policy) == "official"

    def test_classify_tier_denied_domain(self) -> None:
        policy = TrustPolicy.from_config({"denies": ["spam.com"]})
        src = _source(domain="spam.com")
        assert classify_tier(src, policy) == "unknown"

    def test_classify_tier_override(self) -> None:
        policy = TrustPolicy.from_config({"tier_overrides": {"example.org": "primary"}})
        src = _source(domain="example.org")
        assert classify_tier(src, policy) == "primary"

    def test_extract_values_numeric_claim(self) -> None:
        claim = _claim(
            claim_text="Renewable capacity reached 420 GW.",
            claim_kind="numeric",
        )
        src = _source(excerpt="Total renewable capacity added: 420 GW in H1 2026.")
        result = extract_values(claim, src)
        assert len(result) >= 1
        assert "420" in result[0]

    def test_detect_conflicts_two_different_values(self) -> None:
        claim = _claim(
            extracted_values=[
                {"source_id": "S1", "value": "passed"},
                {"source_id": "S2", "value": "failed"},
            ]
        )
        conflicts = detect_conflicts(claim)
        assert len(conflicts) == 1
        assert "S1" in conflicts[0]["source_ids"]
        assert "S2" in conflicts[0]["source_ids"]

    def test_detect_conflicts_same_value_case_insensitive(self) -> None:
        claim = _claim(
            extracted_values=[
                {"source_id": "S1", "value": "Ratified"},
                {"source_id": "S2", "value": "ratified"},
            ]
        )
        assert detect_conflicts(claim) == []

    def test_full_evaluator_single_source_produces_packet(self) -> None:
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [_source()],
                "query": "Was the accord ratified?",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        assert "packet_version" in packet
        assert "overall_verdict" in packet
        assert "claims" in packet
        assert "sources" in packet

    def test_packet_written_to_state(self) -> None:
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [_source()],
                "query": "Test query",
                "as_of": "2026-07-01",
            }
        )
        evaluator(ctx)
        assert ctx.state.get("evidence_packet") is not None

    def test_single_independence_group_warning(self) -> None:
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [
                    _source(source_id="S1"),
                    _source(source_id="S2"),
                ],
                "query": "Test",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        warnings = packet.get("warnings", [])
        assert any("one independence group" in w for w in warnings)


# ---------------------------------------------------------------------------
# Group 4: Security (per 04 §4 TestEvaluatorSecurity)
# ---------------------------------------------------------------------------


class TestEvaluatorSecurity:
    def test_trust_policy_not_readable_from_state(self) -> None:
        """EE-5: policy never comes from ctx.state — modifying state should not affect policy."""
        evaluator = make_evidence_evaluator(trust_policy={"pins": ["safe.gov"]})

        class _TrackingProxy(_FakeProxy):
            def get(self, key: str) -> Any:
                # Trust-policy keys must never be read via state.get()
                assert key not in ("trust_policy", "pins", "denies", "tier_overrides"), (
                    f"Evaluator read trust_policy key {key!r} from state — EE-5 violation"
                )
                return super().get(key)

        ctx = _FakeCtx.__new__(_FakeCtx)
        ctx.state = _TrackingProxy(
            {
                "claim_records": [_claim()],
                "sources": [_source()],
                "query": "Test",
                "as_of": "2026-07-01",
                "trust_policy": {"pins": ["evil.com"]},  # planted in state — must not be read
            }
        )
        ctx.inputs = {}
        # Should complete without touching state trust_policy key.
        evaluator(ctx)

    def test_denied_source_never_supports_or_conflicts(self) -> None:
        """Denied domain sources must be excluded from derivation (03 §4)."""
        evaluator = make_evidence_evaluator(trust_policy={"denies": ["spam.com"]})
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [
                    _source(source_id="S1", domain="spam.com", excerpt="The accord was ratified."),
                ],
                "query": "Was the accord ratified?",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        claim = packet["claims"][0]
        assert "S1" not in claim.get("supporting_source_ids", [])
        assert "S1" not in claim.get("conflicting_source_ids", [])

    def test_injection_flagged_sources_cap_confidence_low(self) -> None:
        """Any supporting source with injection_flags caps confidence at low (03 §5)."""
        evaluator = make_evidence_evaluator()
        src1 = _source(
            source_id="S1",
            domain="gov.org",
            injection_flags=["role_marker"],
            excerpt="The climate accord was ratified.",
        )
        src2 = _source(
            source_id="S2",
            domain="other.gov",
            injection_flags=[],
            excerpt="The climate accord was ratified.",
        )
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [src1, src2],
                "query": "Was the accord ratified?",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        if packet["overall_verdict"] == "verified":
            assert packet["confidence"] == "low"

    def test_single_independence_group_warning_emitted(self) -> None:
        """T4: single-group corroboration must emit a structural warning."""
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [
                    _source(source_id="S1", domain="samesite.org"),
                    _source(source_id="S2", domain="samesite.org"),
                ],
                "query": "Test",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        assert any("independence group" in w for w in packet.get("warnings", []))


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_packet_json_round_trip(self) -> None:
        evaluator = make_evidence_evaluator()
        ctx = _FakeCtx(
            {
                "claim_records": [_claim()],
                "sources": [_source()],
                "query": "Test",
                "as_of": "2026-07-01",
            }
        )
        packet = evaluator(ctx)
        assert packet == json.loads(json.dumps(packet))

    def test_trust_policy_frozen(self) -> None:
        """TrustPolicy is a frozen dataclass — must not be mutated."""
        policy = TrustPolicy.from_config({"pins": ["example.gov"]})
        with pytest.raises((AttributeError, TypeError)):
            policy.pins = frozenset()  # type: ignore[misc]
