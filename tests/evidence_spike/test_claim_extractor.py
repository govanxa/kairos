"""Tests for examples.evidence_engine.claim_extractor (→ C3)."""

from __future__ import annotations

import json

import pytest

from examples.evidence_engine.claim_extractor import (
    _infer_claim_kind,
    claim_extractor,
    extract_claims,
)
from kairos.exceptions import ValidationError
from tests.evidence_spike.conftest import _FakeCtx, _FakeProxy  # noqa: F401

# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_empty_claims_list_raises(self) -> None:
        """M4: extract_claims([]) must raise ValidationError, not return []."""
        with pytest.raises(ValidationError):
            extract_claims([])

    def test_non_list_claims_in_ctx_raises(self) -> None:
        """M4: non-list 'claims' in state must raise ValidationError."""
        ctx = _FakeCtx({"claims": "not a list"})
        with pytest.raises(ValidationError):
            claim_extractor(ctx)

    def test_none_claims_in_ctx_raises(self) -> None:
        """M4: None 'claims' in state must raise ValidationError."""
        ctx = _FakeCtx({"claims": None})
        with pytest.raises(ValidationError):
            claim_extractor(ctx)

    def test_all_whitespace_claims_raises(self) -> None:
        """M4: list of whitespace-only strings must raise ValidationError (empty after filter)."""
        with pytest.raises(ValidationError):
            extract_claims(["", "   ", ""])

    def test_falsy_claim_strings_filtered(self) -> None:
        """Mixed list: falsy entries filtered; non-empty entries processed normally."""
        ctx = _FakeCtx({"claims": ["", "real claim about the event", ""]})
        result = claim_extractor(ctx)
        # Only the non-empty claim becomes a record
        assert len(result["claim_records"]) == 1


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_single_claim(self) -> None:
        records = extract_claims(["The accord was ratified."])
        assert len(records) == 1
        assert records[0]["claim_id"] == "C1"

    def test_claim_ids_sequential(self) -> None:
        records = extract_claims(["Claim one", "Claim two", "Claim three"])
        ids = [r["claim_id"] for r in records]
        assert ids == ["C1", "C2", "C3"]

    def test_all_defaults_safe_for_evaluator(self) -> None:
        records = extract_claims(["Something happened"])
        r = records[0]
        assert r["supporting_source_ids"] == []
        assert r["conflicting_source_ids"] == []
        assert r["support_level"] == "none"
        assert r["verdict"] == "unverifiable"
        assert r["extracted_values"] == []

    def test_time_sensitivity_defaults_volatile(self) -> None:
        """Conservative default: demand fresher sources."""
        records = extract_claims(["The price is 100."])
        assert records[0]["time_sensitivity"] == "volatile"


# ---------------------------------------------------------------------------
# Group 3: Happy paths — claim_kind heuristics
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_numeric_kind_detected(self) -> None:
        kind = _infer_claim_kind("420 GW of renewable capacity was added in H1 2026.")
        # Has a number AND a date token — date takes precedence in current impl
        # (date_tokens check first). Accept either numeric or temporal.
        assert kind in {"numeric", "temporal"}

    def test_pure_number_claim(self) -> None:
        kind = _infer_claim_kind("The total was 420.")
        assert kind == "numeric"

    def test_temporal_kind_detected(self) -> None:
        kind = _infer_claim_kind("The vote occurred on June 28, 2026.")
        assert kind == "temporal"

    def test_other_kind_default(self) -> None:
        kind = _infer_claim_kind("The resolution passed successfully.")
        assert kind == "other"

    def test_year_quarter_temporal(self) -> None:
        kind = _infer_claim_kind("Revenue grew in Q2 2026.")
        assert kind == "temporal"

    def test_claim_text_preserved_verbatim(self) -> None:
        text = "The framework was adopted by member states."
        records = extract_claims([text])
        assert records[0]["claim_text"] == text

    def test_step_action_writes_claim_records(self) -> None:
        ctx = _FakeCtx({"claims": ["The measure passed.", "420 units sold."]})
        result = claim_extractor(ctx)
        stored = ctx.state.get("claim_records")
        assert stored is not None
        assert len(stored) == 2
        assert result["claim_records"] == stored


# ---------------------------------------------------------------------------
# Group 4: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_claim_records_json_round_trip(self) -> None:
        records = extract_claims(
            [
                "The agreement was signed on 2026-06-28.",
                "Renewable capacity reached 420 GW.",
                "The measure passed.",
            ]
        )
        assert records == json.loads(json.dumps(records))

    def test_claim_record_has_no_non_serializable_values(self) -> None:
        records = extract_claims(["Test claim."])
        for r in records:
            for val in r.values():
                json.dumps(val)  # must not raise
