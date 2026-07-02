"""Tests for kairos_plugin_evidence.claim_extractor — C3 slice 1.

Test-after (Evidence Engine exception, CLAUDE.md). Failure paths first, then
boundary, happy/heuristic, serialization — per CLAUDE.md priority order.

Generality (07): claim fixtures span event_outcome, temporal, numeric, other.
Sports-adjacent claims appear ONLY in MUST-fix #3 heuristic tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import _FakeCtx
from kairos.exceptions import ValidationError

from kairos_plugin_evidence.claim_extractor import (
    _infer_claim_kind,
    claim_extractor,
    extract_claims,
)
from kairos_plugin_evidence.contracts import CLAIM_RECORD, ClaimKind, TimeSensitivity

# ---------------------------------------------------------------------------
# G1 — Failure paths (write first)
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """extract_claims and the step action must reject bad inputs."""

    def test_empty_list_raises_validation_error(self) -> None:
        """Empty list → ValidationError."""
        with pytest.raises(ValidationError, match="must not be empty"):
            extract_claims([])

    def test_whitespace_only_strings_raise_validation_error(self) -> None:
        """List of whitespace-only strings → ValidationError after filtering."""
        with pytest.raises(ValidationError, match="must not be empty"):
            extract_claims(["   ", "\t", "\n"])

    def test_mixed_valid_and_whitespace_filters_whitespace(self) -> None:
        """Whitespace-only items are dropped; real claims are kept."""
        records = extract_claims(["  ", "CO2 reached 421 ppm", "  \t  "])
        assert len(records) == 1
        assert records[0]["claim_text"] == "CO2 reached 421 ppm"

    def test_non_list_state_raises_validation_error(self) -> None:
        """Step action raises ValidationError when 'claims' is not a list."""
        ctx = _FakeCtx({"claims": "Belgium beat Senegal 3-2"})
        with pytest.raises(ValidationError, match="must be a list"):
            claim_extractor(ctx)

    def test_none_state_raises_validation_error(self) -> None:
        """Step action raises ValidationError when 'claims' is None."""
        ctx = _FakeCtx({"claims": None})
        with pytest.raises(ValidationError, match="must be a list"):
            claim_extractor(ctx)

    def test_dict_state_raises_validation_error(self) -> None:
        """Step action raises ValidationError when 'claims' is a dict."""
        ctx = _FakeCtx({"claims": {"claim": "some text"}})
        with pytest.raises(ValidationError, match="must be a list"):
            claim_extractor(ctx)

    def test_missing_claims_key_raises_validation_error(self) -> None:
        """Step action raises ValidationError when 'claims' key is absent."""
        ctx = _FakeCtx({})
        with pytest.raises(ValidationError, match="must be a list"):
            claim_extractor(ctx)

    def test_all_none_items_raises_after_filtering(self) -> None:
        """List of None items → all dropped → ValidationError."""
        ctx = _FakeCtx({"claims": [None, None]})
        with pytest.raises(ValidationError, match="must not be empty"):
            claim_extractor(ctx)


# ---------------------------------------------------------------------------
# G2 — Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """Edge inputs and ID sequencing."""

    def test_single_claim_gets_c1_id(self) -> None:
        """Single claim → claim_id == 'C1'."""
        records = extract_claims(["Vaccination coverage reached 87 percent."])
        assert len(records) == 1
        assert records[0]["claim_id"] == "C1"

    def test_sequential_ids_c1_through_c3(self) -> None:
        """Three claims → C1, C2, C3 in order."""
        claims = [
            "CO2 is 421 ppm.",
            "The policy was adopted on March 15, 2025.",
            "Python was created by Guido van Rossum.",
        ]
        records = extract_claims(claims)
        assert [r["claim_id"] for r in records] == ["C1", "C2", "C3"]

    def test_claim_text_preserved_verbatim(self) -> None:
        """claim_text equals the original string, not stripped or modified."""
        text = "  Atmospheric CO2 concentration is 421 ppm.  "
        # leading/trailing whitespace: the text itself is preserved; only
        # whitespace-only items are filtered at the list level.
        records = extract_claims([text])
        assert records[0]["claim_text"] == text

    def test_non_str_items_coerced_to_str(self) -> None:
        """Non-str, non-None items in state list are coerced via str()."""
        ctx = _FakeCtx({"claims": [42, "The rate is 87 percent"]})
        result = claim_extractor(ctx)
        texts = [r["claim_text"] for r in result["claim_records"]]
        assert "42" in texts
        assert "The rate is 87 percent" in texts

    def test_none_items_in_list_skipped(self) -> None:
        """None items inside the list are silently dropped."""
        ctx = _FakeCtx({"claims": [None, "CO2 is 421 ppm", None]})
        result = claim_extractor(ctx)
        assert len(result["claim_records"]) == 1
        assert result["claim_records"][0]["claim_text"] == "CO2 is 421 ppm"

    def test_whitespace_only_items_in_step_action_skipped(self) -> None:
        """Whitespace-only strings in the state list are filtered out."""
        ctx = _FakeCtx({"claims": ["   ", "CO2 is 421 ppm", "\t"]})
        result = claim_extractor(ctx)
        assert len(result["claim_records"]) == 1

    def test_claim_with_score_and_date_classified_event_outcome(self) -> None:
        """Score pattern takes precedence over date tokens in claim_kind."""
        # "July 1, 2026" is a date; "3-2" is a score → score wins.
        records = extract_claims(["Belgium beat Senegal 3-2 on July 1, 2026 at the World Cup"])
        assert records[0]["claim_kind"] == ClaimKind.EVENT_OUTCOME


# ---------------------------------------------------------------------------
# G3 — Happy / heuristic paths
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    """_infer_claim_kind ordering and extract_claims / step action happy paths."""

    # --- _infer_claim_kind heuristic (MUST-fix #3 ordering) ---

    def test_score_pattern_yields_event_outcome(self) -> None:
        """Claim with 'N-M' score → event_outcome (MUST-fix #3 — score checked FIRST)."""
        assert (
            _infer_claim_kind("Belgium beat Senegal 3-2 at the 2026 World Cup") == "event_outcome"
        )

    def test_en_dash_score_yields_event_outcome(self) -> None:
        """En-dash score pattern '3–2' → event_outcome."""
        assert _infer_claim_kind("England beat DR Congo 2–1") == "event_outcome"

    def test_em_dash_score_yields_event_outcome(self) -> None:
        """Em-dash score pattern '3—2' → event_outcome."""
        assert _infer_claim_kind("Team A beat Team B 3—2") == "event_outcome"

    def test_date_claim_yields_temporal(self) -> None:
        """Claim with month name and no score → temporal."""
        assert _infer_claim_kind("The accord was signed on March 15, 2025.") == "temporal"

    def test_iso_date_claim_yields_temporal(self) -> None:
        """ISO date in claim → temporal."""
        assert _infer_claim_kind("The summit occurred on 2025-06-15.") == "temporal"

    def test_quarter_label_claim_yields_temporal(self) -> None:
        """'Q1 2025' in claim → temporal."""
        assert _infer_claim_kind("Revenue grew in Q1 2025.") == "temporal"

    def test_numeric_claim_yields_numeric(self) -> None:
        """Plain number in claim with no score/date → numeric."""
        assert _infer_claim_kind("Atmospheric CO2 is 421 ppm.") == "numeric"

    def test_percentage_claim_yields_numeric(self) -> None:
        """Percentage in claim → numeric."""
        assert _infer_claim_kind("Vaccination coverage reached 87 percent.") == "numeric"

    def test_plain_claim_yields_other(self) -> None:
        """Claim with no score, date, or digit → other."""
        assert _infer_claim_kind("Python was created by Guido van Rossum.") == "other"

    def test_empty_string_yields_other(self) -> None:
        """Empty string → other (no tokens trigger any heuristic)."""
        assert _infer_claim_kind("") == "other"

    # --- extract_claims defaults ---

    def test_default_time_sensitivity_is_volatile(self) -> None:
        """Every claim defaults to time_sensitivity='volatile' (conservative)."""
        records = extract_claims(["The interest rate rose to 5 percent."])
        assert records[0]["time_sensitivity"] == TimeSensitivity.VOLATILE

    def test_evidence_fields_default_to_safe_values(self) -> None:
        """Evidence fields are at safe defaults (empty IDs, none/unverifiable)."""
        records = extract_claims(["Some claim about CO2 levels."])
        r = records[0]
        assert r["supporting_source_ids"] == []
        assert r["conflicting_source_ids"] == []
        assert r["support_level"] == "none"
        assert r["verdict"] == "unverifiable"
        assert r["extracted_values"] == []
        assert r["notes"] == ""

    def test_records_are_structurally_valid_claim_records(self) -> None:
        """All produced records pass CLAIM_RECORD structural validation."""
        claims = [
            "Belgium beat Senegal 3-2 at the 2026 World Cup",
            "The policy was adopted on March 15, 2025.",
            "CO2 concentration is 421 ppm.",
            "Python was created by Guido van Rossum.",
        ]
        records = extract_claims(claims)
        for rec in records:
            result = CLAIM_RECORD.validate(rec)
            assert result.valid, f"CLAIM_RECORD validation failed: {result.errors}"

    # --- Step action ---

    def test_step_action_writes_claim_records_to_state(self) -> None:
        """claim_extractor writes 'claim_records' to state."""
        ctx = _FakeCtx({"claims": ["CO2 is 421 ppm."]})
        claim_extractor(ctx)
        stored: Any = ctx.state.get("claim_records")
        assert isinstance(stored, list)
        assert len(stored) == 1
        assert stored[0]["claim_id"] == "C1"

    def test_step_action_returns_claim_records_dict(self) -> None:
        """Step action returns {'claim_records': [...]}."""
        ctx = _FakeCtx({"claims": ["CO2 is 421 ppm."]})
        result = claim_extractor(ctx)
        assert "claim_records" in result
        assert isinstance(result["claim_records"], list)

    def test_step_action_return_matches_state(self) -> None:
        """Return value is the same object as what was written to state."""
        ctx = _FakeCtx({"claims": ["CO2 is 421 ppm.", "Rate is 87 percent."]})
        result = claim_extractor(ctx)
        assert result["claim_records"] == ctx.state.get("claim_records")

    def test_step_action_multiple_claims(self) -> None:
        """Step action handles a list of claims spanning multiple kinds."""
        ctx = _FakeCtx(
            {
                "claims": [
                    "Belgium beat Senegal 3-2",
                    "The accord was signed on March 15, 2025",
                    "CO2 is 421 ppm",
                    "Python was created by Guido van Rossum",
                ]
            }
        )
        result = claim_extractor(ctx)
        records = result["claim_records"]
        assert len(records) == 4
        kinds = [r["claim_kind"] for r in records]
        assert kinds[0] == "event_outcome"
        assert kinds[1] == "temporal"
        assert kinds[2] == "numeric"
        assert kinds[3] == "other"

    def test_manifest_registers_claim_extractor(self) -> None:
        """MANIFEST.steps includes 'claim_extractor' after slice-1 wiring."""
        from kairos_plugin_evidence import MANIFEST

        assert "claim_extractor" in MANIFEST.steps

    def test_manifest_claim_extractor_has_output_contract(self) -> None:
        """MANIFEST claim_extractor step has EXTRACTOR_OUTPUT as output_contract."""
        from kairos_plugin_evidence import EXTRACTOR_OUTPUT, MANIFEST

        spec = MANIFEST.steps["claim_extractor"]
        assert spec.output_contract is EXTRACTOR_OUTPUT

    def test_manifest_claim_extractor_input_contract_is_none(self) -> None:
        """MANIFEST claim_extractor step has input_contract=None (DN-1)."""
        from kairos_plugin_evidence import MANIFEST

        spec = MANIFEST.steps["claim_extractor"]
        assert spec.input_contract is None


# ---------------------------------------------------------------------------
# G2b — _MAX_CLAIM_TEXT_LEN boundary (SEV-002 truncation cap)
# ---------------------------------------------------------------------------


class TestClaimTextTruncation:
    """SEV-002: claim text is capped at _MAX_CLAIM_TEXT_LEN (2000) before storage."""

    def test_claim_exactly_2000_chars_kept_verbatim(self) -> None:
        """A claim of exactly 2000 chars is stored unchanged (boundary — not truncated)."""
        from kairos_plugin_evidence.claim_extractor import _MAX_CLAIM_TEXT_LEN

        text = "a" * _MAX_CLAIM_TEXT_LEN  # exactly 2000
        records = extract_claims([text])
        assert len(records[0]["claim_text"]) == _MAX_CLAIM_TEXT_LEN
        assert records[0]["claim_text"] == text

    def test_claim_over_2000_chars_truncated_to_cap(self) -> None:
        """A claim of 2500 chars is silently truncated to exactly 2000 (SEV-002)."""
        from kairos_plugin_evidence.claim_extractor import _MAX_CLAIM_TEXT_LEN

        text = "b" * 2500
        records = extract_claims([text])
        assert len(records[0]["claim_text"]) == _MAX_CLAIM_TEXT_LEN
        assert records[0]["claim_text"] == "b" * _MAX_CLAIM_TEXT_LEN

    def test_claim_at_2001_chars_truncated(self) -> None:
        """One char over the cap → truncated to the cap (exact boundary)."""
        from kairos_plugin_evidence.claim_extractor import _MAX_CLAIM_TEXT_LEN

        text = "c" * (_MAX_CLAIM_TEXT_LEN + 1)
        records = extract_claims([text])
        assert len(records[0]["claim_text"]) == _MAX_CLAIM_TEXT_LEN


# ---------------------------------------------------------------------------
# G5 — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """ClaimRecord output must round-trip through JSON."""

    def test_extract_claims_output_json_round_trip(self) -> None:
        """extract_claims result survives json.loads(json.dumps(...)) unchanged."""
        claims = [
            "Belgium beat Senegal 3-2 at the 2026 World Cup",
            "The climate accord was signed on March 15, 2025.",
            "Atmospheric CO2 reached 421 ppm in 2025.",
            "Python was created by Guido van Rossum.",
        ]
        records = extract_claims(claims)
        serialized = json.loads(json.dumps(records))
        assert serialized == records

    def test_claim_records_are_json_native(self) -> None:
        """All field values in ClaimRecord dicts are JSON-native types."""
        records = extract_claims(["Vaccination coverage is 87 percent."])
        for rec in records:
            for key, val in rec.items():
                # json.dumps raises if a value is not serializable.
                json.dumps({key: val})

    def test_step_action_output_is_json_serializable(self) -> None:
        """Step action return value is JSON-serializable."""
        ctx = _FakeCtx({"claims": ["CO2 is 421 ppm.", "Rate is 87 percent."]})
        result = claim_extractor(ctx)
        serialized = json.loads(json.dumps(result))
        assert serialized == result
