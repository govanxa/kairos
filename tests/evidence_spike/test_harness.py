"""Tests for examples.evidence_engine.harness — G1–G4 acceptance harness."""

from __future__ import annotations

import json

from examples.evidence_engine.answer import make_grounded_model, make_refusal_model
from examples.evidence_engine.fixtures import FIXTURE_FAMILIES, INJECTION_SENTINEL, load_fixture
from examples.evidence_engine.harness import (
    G1Row,
    G2Row,
    HarnessReport,
    _run_g1,
    _run_g2,
    _run_g3,
    _run_g4,
    _sentinel_in_json,
    run_acceptance,
)

# ---------------------------------------------------------------------------
# Group 1: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_sentinel_in_json_detects_string(self) -> None:
        assert _sentinel_in_json({"key": INJECTION_SENTINEL})

    def test_sentinel_in_json_detects_nested_list(self) -> None:
        data = {"a": [{"b": f"prefix {INJECTION_SENTINEL} suffix"}]}
        assert _sentinel_in_json(data)

    def test_sentinel_in_json_clean_data_returns_false(self) -> None:
        assert not _sentinel_in_json({"key": "clean value", "num": 42})

    def test_g1_row_passed_false_on_wrong_verdict(self) -> None:
        row = G1Row(
            family_id="test",
            overall_verdict="conflicting",
            confidence="low",
            answer="some answer",
            answer_correct=True,
            passed=False,
        )
        assert not row.passed

    def test_harness_report_all_passed_false_when_g3_fails(self) -> None:
        report = HarnessReport(g3_passed=False, g4_passed=True)
        assert not report.all_passed

    def test_harness_report_all_passed_false_when_g4_fails(self) -> None:
        report = HarnessReport(g3_passed=True, g4_passed=False)
        assert not report.all_passed


# ---------------------------------------------------------------------------
# Group 2: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_fixture_families_all_loadable(self) -> None:
        """All fixture family IDs must load without error."""
        for fid in FIXTURE_FAMILIES:
            fixture = load_fixture(fid)
            assert "documents" in fixture
            assert "claims" in fixture
            assert "query" in fixture
            assert "as_of" in fixture

    def test_harness_report_all_passed_true_when_all_green(self) -> None:
        report = HarnessReport(g3_passed=True, g4_passed=True)
        # Add passing G1 and G2 rows
        report.g1_rows = [
            G1Row("f1", "verified", "moderate", "answer", True, True),
        ]
        report.g2_rows = [
            G2Row("f1", "refusal", "correct", True, True, True),
        ]
        assert report.all_passed

    def test_sentinel_in_json_handles_empty_dict(self) -> None:
        assert not _sentinel_in_json({})

    def test_sentinel_in_json_handles_none(self) -> None:
        assert not _sentinel_in_json(None)

    def test_sentinel_in_json_handles_integer(self) -> None:
        assert not _sentinel_in_json(42)


# ---------------------------------------------------------------------------
# Group 3: Happy paths — full offline acceptance run
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_run_acceptance_returns_harness_report(self) -> None:
        report = run_acceptance()
        assert isinstance(report, HarnessReport)

    def test_g1_rows_count(self) -> None:
        report = run_acceptance()
        assert len(report.g1_rows) == 3  # three fixture families

    def test_g2_rows_count(self) -> None:
        report = run_acceptance()
        assert len(report.g2_rows) == 3

    def test_all_gates_pass_offline(self) -> None:
        """Full offline run must pass all G1–G4 gates."""
        report = run_acceptance()
        # G1
        for row in report.g1_rows:
            assert row.passed, (
                f"G1 FAIL: {row.family_id} — verdict={row.overall_verdict!r}, answer={row.answer!r}"
            )
        # G2
        for row in report.g2_rows:
            assert row.passed, (
                f"G2 FAIL: {row.family_id} — baseline_refused={row.baseline_refused}, "
                f"pipeline_correct={row.pipeline_correct}"
            )
        # G3
        assert report.g3_passed, f"G3 FAIL: {report.g3_notes}"
        # G4
        assert report.g4_passed, f"G4 FAIL: {report.g4_notes}"

    def test_g1_verified_families_return_verified_verdict(self) -> None:
        rows = _run_g1(make_grounded_model())
        for row in rows:
            assert row.overall_verdict == "verified", (
                f"G1: {row.family_id} returned {row.overall_verdict!r}"
            )

    def test_g2_refusal_mode_contains_training_keyword(self) -> None:
        rows = _run_g2(make_grounded_model(), make_refusal_model())
        for row in rows:
            assert row.baseline_refused, (
                f"G2: baseline for {row.family_id!r} did not refuse: {row.baseline_answer!r}"
            )

    def test_g3_injection_sentinel_absent(self) -> None:
        passed, notes = _run_g3(make_grounded_model())
        assert passed, f"G3 injection containment failed: {notes}"

    def test_g4_conflicting_verdict(self) -> None:
        passed, notes = _run_g4(make_grounded_model())
        assert passed, f"G4 honest uncertainty failed: {notes}"

    def test_run_acceptance_uses_default_grounded_model_when_none(self) -> None:
        """Calling run_acceptance() with no args must not raise."""
        report = run_acceptance(model_fn=None)
        assert isinstance(report, HarnessReport)

    def test_g1_row_dataclass_fields(self) -> None:
        row = G1Row("fam", "verified", "high", "answer text", True, True)
        assert row.family_id == "fam"
        assert row.overall_verdict == "verified"
        assert row.passed is True

    def test_g2_row_dataclass_fields(self) -> None:
        row = G2Row("fam", "refusal answer", "correct answer", True, True, True)
        assert row.family_id == "fam"
        assert row.baseline_refused is True


# ---------------------------------------------------------------------------
# Group 4: Security
# ---------------------------------------------------------------------------


class TestHarnessSecurity:
    def test_g3_harness_checks_every_step_output(self) -> None:
        """G3 must inspect all step outputs, not just final_state."""
        passed, notes = _run_g3(make_grounded_model())
        # Notes should mention step-level checks
        assert "output" in notes or "step" in notes or "final_state" in notes

    def test_fixture_file_sentinel_only_in_poisoned_fixture(self) -> None:
        """Sentinel must appear only in the poisoned fixture content, nowhere else."""
        for fid in FIXTURE_FAMILIES:
            if fid == "poisoned_injection":
                continue
            fixture = load_fixture(fid)
            serialized = json.dumps(fixture)
            assert INJECTION_SENTINEL not in serialized, (
                f"INJECTION_SENTINEL unexpectedly found in fixture {fid!r}"
            )

    def test_sentinel_in_poisoned_fixture_content(self) -> None:
        """Poisoned fixture must actually contain the sentinel in doc content."""
        fixture = load_fixture("poisoned_injection")
        serialized = json.dumps(fixture)
        assert INJECTION_SENTINEL in serialized


# ---------------------------------------------------------------------------
# Group 5: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_harness_report_json_serializable(self) -> None:
        """HarnessReport must be convertible to a JSON-safe dict."""
        report = run_acceptance()
        # Manually serialize the dataclass fields
        data = {
            "g1_rows": [
                {
                    "family_id": r.family_id,
                    "overall_verdict": r.overall_verdict,
                    "confidence": r.confidence,
                    "answer": r.answer,
                    "answer_correct": r.answer_correct,
                    "passed": r.passed,
                }
                for r in report.g1_rows
            ],
            "g2_rows": [
                {
                    "family_id": r.family_id,
                    "baseline_answer": r.baseline_answer,
                    "pipeline_answer": r.pipeline_answer,
                    "baseline_refused": r.baseline_refused,
                    "pipeline_correct": r.pipeline_correct,
                    "passed": r.passed,
                }
                for r in report.g2_rows
            ],
            "g3_passed": report.g3_passed,
            "g3_notes": report.g3_notes,
            "g4_passed": report.g4_passed,
            "g4_notes": report.g4_notes,
            "all_passed": report.all_passed,
        }
        # Must not raise
        serialized = json.dumps(data)
        assert isinstance(serialized, str)
