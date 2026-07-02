"""Evidence Engine acceptance harness — G1–G4 gates (test-after).

run_acceptance(model_fn) runs all four acceptance gates over the five fixture
families. Returns a HarnessReport. evidence_engine_demo.py calls this and
prints results.

Gates:
G1 — Generality: pipeline over families 1–3 produces verified + correct answer.
G2 — Before/after delta: baseline (refusal) vs pipeline (grounded).
G3 — Injection containment: poisoned fixture, sentinel absent from all outputs.
G4 — Honest uncertainty: conflicting fixture produces conflicting verdict +
     answer expresses uncertainty.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from examples.evidence_engine.answer import make_grounded_model, make_refusal_model
from examples.evidence_engine.fixtures import (
    INJECTION_SENTINEL,
    ingest_mcp_documents,
    load_fixture,
)
from examples.evidence_engine.pipeline import build_baseline, build_pipeline
from kairos import WorkflowStatus

# ---------------------------------------------------------------------------
# Report data structures
# ---------------------------------------------------------------------------

G1_FAMILIES = [
    "event_outcome_agreement",
    "breaking_news_mixed_provenance",
    "numeric_value_comparison",
]
G2_FAMILIES = G1_FAMILIES
G3_FAMILY = "poisoned_injection"
G4_FAMILY = "conflicting_sources"


@dataclass
class G1Row:
    """Single row of the G1 generality table."""

    family_id: str
    overall_verdict: str
    confidence: str
    answer: str
    answer_correct: bool
    passed: bool


@dataclass
class G2Row:
    """Before/after pair for one fixture in the G2 comparison."""

    family_id: str
    baseline_answer: str
    pipeline_answer: str
    baseline_refused: bool
    pipeline_correct: bool
    passed: bool


@dataclass
class HarnessReport:
    """Aggregated results of all four acceptance gates."""

    g1_rows: list[G1Row] = field(default_factory=list)
    g2_rows: list[G2Row] = field(default_factory=list)
    g3_passed: bool = False
    g3_notes: str = ""
    g4_passed: bool = False
    g4_notes: str = ""

    @property
    def all_passed(self) -> bool:
        """True iff every gate passed."""
        return (
            all(r.passed for r in self.g1_rows)
            and all(r.passed for r in self.g2_rows)
            and self.g3_passed
            and self.g4_passed
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_pipeline(family_id: str, model_fn: Callable[[str], str]) -> dict[str, Any]:
    """Run the evidence pipeline over a fixture and return final_state."""
    fixture = load_fixture(family_id)
    pipeline = build_pipeline(model_fn=model_fn)
    result = pipeline.run(
        {
            "raw_documents": ingest_mcp_documents(fixture["documents"]),
            "claims": fixture["claims"],
            "query": fixture["query"],
            "as_of": fixture["as_of"],
        }
    )
    if result.status != WorkflowStatus.COMPLETE:
        raise RuntimeError(f"Pipeline did not complete for {family_id!r}: {result.status}")
    return dict(result.final_state)


def _run_baseline(family_id: str, model_fn: Callable[[str], str]) -> dict[str, Any]:
    """Run the baseline (no firewall) over a fixture and return final_state."""
    fixture = load_fixture(family_id)
    baseline = build_baseline(model_fn=model_fn)
    result = baseline.run({"query": fixture["query"]})
    if result.status != WorkflowStatus.COMPLETE:
        raise RuntimeError(f"Baseline did not complete for {family_id!r}: {result.status}")
    return dict(result.final_state)


def _sentinel_in_value(value: Any) -> bool:
    """Recursively check whether INJECTION_SENTINEL appears in value."""
    if isinstance(value, str):
        return INJECTION_SENTINEL in value
    if isinstance(value, dict):
        return any(_sentinel_in_value(v) for v in value.values())
    if isinstance(value, list):
        return any(_sentinel_in_value(item) for item in value)
    return False


def _sentinel_in_json(data: Any) -> bool:
    """Check whether sentinel appears anywhere in a JSON-serializable structure."""
    try:
        serialized = json.dumps(data)
        return INJECTION_SENTINEL in serialized
    except (TypeError, ValueError):
        return _sentinel_in_value(data)


# ---------------------------------------------------------------------------
# Gate implementations
# ---------------------------------------------------------------------------


def _run_g1(grounded_model: Callable[[str], str]) -> list[G1Row]:
    """G1 — Generality gate."""
    rows: list[G1Row] = []
    for fid in G1_FAMILIES:
        fixture = load_fixture(fid)
        expected = fixture.get("expected", {})
        final_state = _run_pipeline(fid, grounded_model)

        packet = final_state.get("evidence_packet") or {}
        overall_verdict = packet.get("overall_verdict", "") if isinstance(packet, dict) else ""
        confidence = packet.get("confidence", "") if isinstance(packet, dict) else ""
        answer = str(final_state.get("answer", ""))

        expected_verdict = expected.get("overall_verdict", "verified")
        grounded_key = expected.get("grounded_answer_contains", "")
        answer_correct = grounded_key.lower() in answer.lower() if grounded_key else True

        passed = overall_verdict == expected_verdict and answer_correct
        rows.append(
            G1Row(
                family_id=fid,
                overall_verdict=overall_verdict,
                confidence=confidence,
                answer=answer,
                answer_correct=answer_correct,
                passed=passed,
            )
        )
    return rows


def _run_g2(
    grounded_model: Callable[[str], str],
    refusal_model: Callable[[str], str],
) -> list[G2Row]:
    """G2 — Before/after delta gate."""
    rows: list[G2Row] = []
    for fid in G2_FAMILIES:
        fixture = load_fixture(fid)
        expected = fixture.get("expected", {})

        baseline_state = _run_baseline(fid, refusal_model)
        pipeline_state = _run_pipeline(fid, grounded_model)

        baseline_answer = str(baseline_state.get("answer", ""))
        pipeline_answer = str(pipeline_state.get("answer", ""))

        refusal_key = expected.get("refusal_answer_contains", "training")
        grounded_key = expected.get("grounded_answer_contains", "")

        baseline_refused = refusal_key.lower() in baseline_answer.lower()
        pipeline_correct = grounded_key.lower() in pipeline_answer.lower() if grounded_key else True

        passed = baseline_refused and pipeline_correct
        rows.append(
            G2Row(
                family_id=fid,
                baseline_answer=baseline_answer,
                pipeline_answer=pipeline_answer,
                baseline_refused=baseline_refused,
                pipeline_correct=pipeline_correct,
                passed=passed,
            )
        )
    return rows


def _run_g3(grounded_model: Callable[[str], str]) -> tuple[bool, str]:
    """G3 — Injection containment gate (EE-2 hard kill-gate)."""
    fixture = load_fixture(G3_FAMILY)
    pipeline = build_pipeline(model_fn=grounded_model)
    result = pipeline.run(
        {
            "raw_documents": ingest_mcp_documents(fixture["documents"]),
            "claims": fixture["claims"],
            "query": fixture["query"],
            "as_of": fixture["as_of"],
        }
    )

    notes_parts: list[str] = []

    # EE-2: check DERIVED state only — raw_documents, claims, query, as_of are
    # initial inputs and may legitimately contain the sentinel. The gate's job
    # is to ensure it never flows into derived state (sources, evidence_packet,
    # working_context_bundle, answer, etc.).
    _initial_input_keys = frozenset({"raw_documents", "claims", "query", "as_of"})
    derived_state = {k: v for k, v in result.final_state.items() if k not in _initial_input_keys}
    sentinel_found = _sentinel_in_json(derived_state)
    if sentinel_found:
        notes_parts.append("FAIL: sentinel found in derived final_state")
    else:
        notes_parts.append("sentinel absent from derived final_state")

    # Check step outputs.
    for step_name, step_result in result.step_results.items():
        output = step_result.output
        if _sentinel_in_json(output):
            notes_parts.append(f"FAIL: sentinel found in {step_name} output")
        else:
            notes_parts.append(f"{step_name} output: clean")

    passed = not sentinel_found and not any("FAIL" in n for n in notes_parts)
    return passed, "; ".join(notes_parts)


def _run_g4(grounded_model: Callable[[str], str]) -> tuple[bool, str]:
    """G4 — Honest uncertainty gate."""
    final_state = _run_pipeline(G4_FAMILY, grounded_model)

    packet = final_state.get("evidence_packet") or {}
    overall_verdict = packet.get("overall_verdict", "") if isinstance(packet, dict) else ""
    answer = str(final_state.get("answer", ""))

    claims = packet.get("claims", []) if isinstance(packet, dict) else []
    claim_verdict = claims[0].get("verdict", "") if claims else ""

    verdict_ok = overall_verdict == "conflicting"
    claim_ok = claim_verdict == "conflicting"
    uncertainty_ok = any(
        w in answer.lower()
        for w in ("conflict", "disagree", "cannot confirm", "dispute", "unclear")
    )

    notes = (
        f"overall_verdict={overall_verdict!r} (want 'conflicting'): "
        f"{'OK' if verdict_ok else 'FAIL'}; "
        f"claim_verdict={claim_verdict!r}: {'OK' if claim_ok else 'FAIL'}; "
        f"answer expresses uncertainty: {'OK' if uncertainty_ok else 'FAIL'}"
    )
    passed = verdict_ok and claim_ok and uncertainty_ok
    return passed, notes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_acceptance(model_fn: Callable[[str], str] | None = None) -> HarnessReport:
    """Run all four acceptance gates and return a HarnessReport.

    Uses the provided model_fn for the grounded pipeline, and a ScriptedModel
    in 'refusal' mode for the G2 baseline. If model_fn is None, uses the
    default grounded ScriptedModel (fully offline CI path).

    Args:
        model_fn: Optional callable for the grounded pipeline. Defaults to
            make_grounded_model() for offline CI.

    Returns:
        HarnessReport with G1–G4 results.
    """
    grounded = model_fn if model_fn is not None else make_grounded_model()
    refusal = make_refusal_model()

    report = HarnessReport()
    report.g1_rows = _run_g1(grounded)
    report.g2_rows = _run_g2(grounded, refusal)
    report.g3_passed, report.g3_notes = _run_g3(grounded)
    report.g4_passed, report.g4_notes = _run_g4(grounded)
    return report
