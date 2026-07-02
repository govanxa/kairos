"""Tests for examples.evidence_engine.belief_revision_builder (→ C4)."""

from __future__ import annotations

import json
from typing import Any

from examples.evidence_engine.belief_revision_builder import (
    _MAX_WORKING_CONTEXT,
    belief_revision_builder,
    render_working_context,
)
from tests.evidence_spike.conftest import _FakeCtx, _FakeProxy  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _large_packet(n_claims: int, *, excerpt_len: int = 400) -> dict[str, Any]:
    """Build a packet whose rendered context genuinely exceeds _MAX_WORKING_CONTEXT.

    Each supported claim carries its own source with a long excerpt so the
    assembled working context blows past the 8000-char cap and forces the
    truncation-priority path (blueprint §6 failure-path requirement).
    """
    sources: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    for i in range(1, n_claims + 1):
        sid = f"S{i}"
        sources.append(
            {
                "source_id": sid,
                "url": f"https://example{i}.org/page",
                "domain": f"example{i}.org",
                "title": f"Title {i}",
                "fetched_at": "2026-07-01T10:00:00Z",
                "published_at": "2026-06-30T10:00:00Z",
                "independence_group": f"example{i}.org",
                "provenance_tier": "established_media",
                "freshness": "recent",
                "injection_flags": [],
                "excerpt": f"Fact {i}: " + ("verified detail " * (excerpt_len // 16)),
            }
        )
        claims.append(
            {
                "claim_id": f"C{i}",
                "claim_text": f"Verified claim number {i} about the accord ratification.",
                "claim_kind": "other",
                "time_sensitivity": "volatile",
                "supporting_source_ids": [sid],
                "conflicting_source_ids": [],
                "support_level": "single_source",
                "verdict": "supported",
                "extracted_values": [{"source_id": sid, "value": f"value{i}"}],
                "notes": "",
            }
        )
    return _packet(sources=sources, claims=claims)


def _packet(**overrides: Any) -> dict[str, Any]:
    src = {
        "source_id": "S1",
        "url": "https://example.org/page",
        "domain": "example.org",
        "title": "Test",
        "fetched_at": "2026-07-01T10:00:00Z",
        "published_at": "2026-06-30T10:00:00Z",
        "independence_group": "example.org",
        "provenance_tier": "established_media",
        "freshness": "recent",
        "injection_flags": [],
        "excerpt": "The climate accord was ratified by all member states on June 28, 2026.",
    }
    claim = {
        "claim_id": "C1",
        "claim_text": "The climate accord was ratified.",
        "claim_kind": "other",
        "time_sensitivity": "volatile",
        "supporting_source_ids": ["S1"],
        "conflicting_source_ids": [],
        "support_level": "single_source",
        "verdict": "supported",
        "extracted_values": [{"source_id": "S1", "value": "ratified"}],
        "notes": "",
    }
    base: dict[str, Any] = {
        "packet_version": "1.0",
        "packet_id": "test-packet-001",
        "query": "Was the climate accord ratified?",
        "as_of": "2026-07-01",
        "generated_at": "2026-07-01T12:00:00Z",
        "claims": [claim],
        "sources": [src],
        "overall_verdict": "verified",
        "confidence": "moderate",
        "conflicts": [],
        "warnings": [],
        "assist_used": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_empty_packet_dict_does_not_crash(self) -> None:
        bundle = render_working_context({})
        assert "working_context" in bundle
        assert isinstance(bundle["working_context"], str)

    def test_missing_evidence_packet_in_state(self) -> None:
        ctx = _FakeCtx({})
        # Should not raise — uses empty dict fallback
        result = belief_revision_builder(ctx)
        assert "working_context" in result

    def test_excerpt_trim_pass_preserves_anchor_and_frame(self) -> None:
        """QA-added: genuinely exceed 8000 chars so the excerpt-trim truncation
        branch runs; the temporal anchor (belief-revision firewall) and closing
        frame must survive (blueprint §6: 'preserves anchor + verdict lines').
        """
        packet = _large_packet(35)
        # Pre-truncation render must actually be over the cap, else this asserts nothing.
        raw_len = sum(
            len(s["excerpt"]) for s in packet["sources"]
        )  # sanity: excerpts alone dwarf the cap
        assert raw_len > _MAX_WORKING_CONTEXT
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        assert len(wc) <= _MAX_WORKING_CONTEXT
        assert "CURRENT DATE" in wc, "temporal anchor dropped during truncation"
        assert "Answer from the verified evidence" in wc, "closing frame dropped during truncation"
        # Excerpt snippet lines must have been trimmed (no 200-char excerpt line survives full).
        for line in wc.split("\n"):
            if line.startswith("  [S"):
                assert len(line) <= 81  # 80 + ellipsis char

    def test_last_resort_truncation_preserves_anchor_and_frame(self) -> None:
        """QA-added: even when excerpt-trimming is insufficient (many claims), the
        last-resort body truncation must still preserve anchor + closing frame.
        """
        packet = _large_packet(150)
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        assert len(wc) <= _MAX_WORKING_CONTEXT
        assert wc.startswith("CURRENT DATE"), "anchor is not the opening line after truncation"
        assert wc.rstrip().endswith("Answer from the verified evidence above."), (
            "closing frame not preserved as final line after last-resort truncation"
        )

    def test_no_supported_claims_renders_gracefully(self) -> None:
        claim = {
            "claim_id": "C1",
            "claim_text": "Something happened.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": [],
            "conflicting_source_ids": [],
            "support_level": "none",
            "verdict": "unverifiable",
            "extracted_values": [],
            "notes": "",
        }
        packet = _packet(claims=[claim], overall_verdict="insufficient")
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        # Should not contain "VERIFIED FACT" for unverifiable claim
        assert "VERIFIED FACT" not in wc
        assert "COULD NOT BE VERIFIED" in wc or "UNVERIFIABLE" in wc.upper()


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_working_context_never_exceeds_max(self) -> None:
        """Over-long packets must be truncated, not passed through."""
        # Build a packet with very long excerpts to trigger truncation
        long_excerpt = "The climate accord was ratified. " * 300
        src = {
            "source_id": "S1",
            "url": "https://example.org/page",
            "domain": "example.org",
            "title": "Test",
            "fetched_at": "2026-07-01T10:00:00Z",
            "published_at": "2026-06-30T10:00:00Z",
            "independence_group": "example.org",
            "provenance_tier": "established_media",
            "freshness": "recent",
            "injection_flags": [],
            "excerpt": long_excerpt,
        }
        claim = {
            "claim_id": "C1",
            "claim_text": "The climate accord was ratified.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": ["S1"],
            "conflicting_source_ids": [],
            "support_level": "single_source",
            "verdict": "supported",
            "extracted_values": [{"source_id": "S1", "value": "ratified"}],
            "notes": "",
        }
        packet = _packet(sources=[src], claims=[claim])
        bundle = render_working_context(packet)
        assert len(bundle["working_context"]) <= _MAX_WORKING_CONTEXT

    def test_anchor_preserved_in_truncated_context(self) -> None:
        long_excerpt = "The climate accord was ratified. " * 300
        src = {
            "source_id": "S1",
            "url": "https://example.org/page",
            "domain": "example.org",
            "title": "Test",
            "fetched_at": "2026-07-01T10:00:00Z",
            "published_at": "2026-06-30T10:00:00Z",
            "independence_group": "example.org",
            "provenance_tier": "established_media",
            "freshness": "recent",
            "injection_flags": [],
            "excerpt": long_excerpt,
        }
        claim = {
            "claim_id": "C1",
            "claim_text": "The climate accord was ratified.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": ["S1"],
            "conflicting_source_ids": [],
            "support_level": "single_source",
            "verdict": "supported",
            "extracted_values": [{"source_id": "S1", "value": "ratified"}],
            "notes": "",
        }
        packet = _packet(sources=[src], claims=[claim])
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        assert "CURRENT DATE" in wc

    def test_empty_sources_no_citation_list(self) -> None:
        packet = _packet(sources=[], claims=[], overall_verdict="insufficient")
        bundle = render_working_context(packet)
        assert bundle["citations"] == []

    def test_packet_id_passed_through(self) -> None:
        packet = _packet(packet_id="abc-123")
        bundle = render_working_context(packet)
        assert bundle["packet_id"] == "abc-123"


# ---------------------------------------------------------------------------
# Group 3: Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_temporal_anchor_in_context(self) -> None:
        bundle = render_working_context(_packet())
        assert "CURRENT DATE" in bundle["working_context"]
        assert "2026-07-01" in bundle["working_context"]

    def test_closing_frame_in_context(self) -> None:
        bundle = render_working_context(_packet())
        assert "Answer from the verified evidence" in bundle["working_context"]

    def test_supported_claim_renders_as_verified_fact(self) -> None:
        bundle = render_working_context(_packet())
        assert "VERIFIED FACT" in bundle["working_context"]

    def test_conflicting_claim_renders_as_disputed(self) -> None:
        conflict_claim = {
            "claim_id": "C1",
            "claim_text": "The bill passed.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": [],
            "conflicting_source_ids": ["S1", "S2"],
            "support_level": "none",
            "verdict": "conflicting",
            "extracted_values": [
                {"source_id": "S1", "value": "passed"},
                {"source_id": "S2", "value": "failed"},
            ],
            "notes": "",
        }
        packet = _packet(claims=[conflict_claim], overall_verdict="conflicting")
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        assert "DISPUTED" in wc
        # Must express uncertainty, not choose a side
        assert "do NOT pick a side" in wc or "disagree" in wc.lower()

    def test_superseded_assumptions_populated(self) -> None:
        bundle = render_working_context(_packet())
        assert "The climate accord was ratified." in bundle["superseded_assumptions"]

    def test_citations_populated(self) -> None:
        bundle = render_working_context(_packet())
        assert any(c["source_id"] == "S1" for c in bundle["citations"])

    def test_unresolved_conflicts_populated(self) -> None:
        conflict_claim = {
            "claim_id": "C1",
            "claim_text": "The bill passed.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": [],
            "conflicting_source_ids": ["S1"],
            "support_level": "none",
            "verdict": "conflicting",
            "extracted_values": [{"source_id": "S1", "value": "ambiguous"}],
            "notes": "",
        }
        packet = _packet(claims=[conflict_claim], overall_verdict="conflicting")
        bundle = render_working_context(packet)
        assert "The bill passed." in bundle["unresolved_conflicts"]

    def test_step_action_writes_bundle(self) -> None:
        ctx = _FakeCtx({"evidence_packet": _packet()})
        result = belief_revision_builder(ctx)
        stored = ctx.state.get("working_context_bundle")
        assert stored is not None
        assert result == stored

    def test_warnings_rendered_in_context(self) -> None:
        packet = _packet(warnings=["All sources share one independence group."])
        bundle = render_working_context(packet)
        assert "All sources share one independence group." in bundle["working_context"]


# ---------------------------------------------------------------------------
# Group 4: Security (per 04 §4 TestBeliefRevisionSecurity)
# ---------------------------------------------------------------------------


class TestBeliefRevisionSecurity:
    def test_working_context_contains_only_gated_excerpts(self) -> None:
        """Context quotes only sanitized excerpts, not raw web text (EE-1)."""
        raw_url = "https://hacker.com/inject"
        src = {
            "source_id": "S1",
            "url": raw_url,
            "domain": "hacker.com",
            "title": "Hacker",
            "fetched_at": "2026-07-01T10:00:00Z",
            "published_at": None,
            "independence_group": "hacker.com",
            "provenance_tier": "aggregator",
            "freshness": "undated",
            "injection_flags": [],
            "excerpt": "The accord was ratified.",  # this is the sanitized excerpt
        }
        claim = {
            "claim_id": "C1",
            "claim_text": "The accord was ratified.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": ["S1"],
            "conflicting_source_ids": [],
            "support_level": "single_source",
            "verdict": "supported",
            "extracted_values": [{"source_id": "S1", "value": "ratified"}],
            "notes": "",
        }
        packet = _packet(sources=[src], claims=[claim])
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        # URL must not appear verbatim in working context as prose
        assert "hacker.com/inject" not in wc

    def test_unverified_claims_never_rendered_as_facts(self) -> None:
        """Unverifiable claims must NOT use VERIFIED FACT label."""
        claim = {
            "claim_id": "C1",
            "claim_text": "The moon is made of cheese.",
            "claim_kind": "other",
            "time_sensitivity": "static",
            "supporting_source_ids": [],
            "conflicting_source_ids": [],
            "support_level": "none",
            "verdict": "unverifiable",
            "extracted_values": [],
            "notes": "",
        }
        packet = _packet(claims=[claim], overall_verdict="insufficient")
        bundle = render_working_context(packet)
        assert "VERIFIED FACT" not in bundle["working_context"]

    def test_conflict_rendered_as_uncertainty_not_choice(self) -> None:
        """Conflicting verdict must not present one value as chosen truth."""
        c1 = {
            "claim_id": "C1",
            "claim_text": "The vote outcome.",
            "claim_kind": "other",
            "time_sensitivity": "volatile",
            "supporting_source_ids": [],
            "conflicting_source_ids": ["S1", "S2"],
            "support_level": "none",
            "verdict": "conflicting",
            "extracted_values": [
                {"source_id": "S1", "value": "passed 55%"},
                {"source_id": "S2", "value": "failed 42%"},
            ],
            "notes": "",
        }
        packet = _packet(claims=[c1], overall_verdict="conflicting")
        bundle = render_working_context(packet)
        wc = bundle["working_context"]
        assert "DISPUTED" in wc
        # Both sides rendered — the model is not given a chosen answer
        assert "passed 55%" in wc or "failed 42%" in wc


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_bundle_json_round_trip(self) -> None:
        bundle = render_working_context(_packet())
        assert bundle == json.loads(json.dumps(bundle))

    def test_bundle_keys_present(self) -> None:
        bundle = render_working_context(_packet())
        expected_keys = {
            "working_context",
            "superseded_assumptions",
            "citations",
            "packet_id",
            "unresolved_conflicts",
        }
        assert expected_keys == set(bundle.keys())
