"""Offline quickstart for kairos-plugin-evidence — port of the A1 acceptance demo.

Loads the plugin via ``load_plugin("kairos-plugin-evidence")`` (with a documented
fallback to direct import for editable/dev environments where the RECORD limitation
of B2 applies), runs the reference workflow over five canned MCP-shaped fixture
families, and prints the G1–G4 acceptance gates.

``as_of`` is machine-stamped from the system clock at invocation — never user-typed
(Case 3 addendum: the authoritative per-query date must come from the system clock,
re-stamped every run).

Usage (from the plugin root, ``plugins/kairos-plugin-evidence/``)::

    python examples/quickstart.py
    # or, equivalently:
    python -m examples.quickstart

Prints G1–G4 results and exits non-zero on any failure. No network, no real LLM,
no real plugin install required (falls back to direct import if load_plugin fails).

The ``run()`` function returns a ``QuickstartResult`` dataclass so tests can invoke
it without capturing stdout.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure the plugin root is importable so the documented ``python examples/quickstart.py``
# invocation works from a bare checkout (not just ``python -m examples.quickstart`` or a
# wheel install). The plugin root holds both ``kairos_plugin_evidence`` and ``examples``.
_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

# ---------------------------------------------------------------------------
# Import the reference workflow — try load_plugin first, fall back to direct.
# ---------------------------------------------------------------------------
# load_plugin requires the package to be properly installed (non-editable wheel)
# because B2's containment gate checks dist.files (the RECORD).  Under a PEP 660
# editable install (hatchling default: .pth redirection), dist.files typically
# lists only the .pth + .dist-info files, not the source .py files, so the RECORD
# check fails with SecurityError.  This is a known B2 limitation, not a C4 defect.
# The fallback import always works and exercises the same code paths.

_LOAD_PLUGIN_PATH: str = "direct_import"

try:
    from kairos.plugins.registry import load_plugin as _load_plugin

    _manifest = _load_plugin("kairos-plugin-evidence")
    _build_reference_workflow = _manifest.workflows["reference"]
    _LOAD_PLUGIN_PATH = "load_plugin"
except Exception as _lp_exc:  # noqa: BLE001
    print(  # noqa: T201
        f"[quickstart] load_plugin unavailable ({type(_lp_exc).__name__}); "
        "falling back to direct import (editable-install RECORD limitation — see B2 docs)."
    )
    from kairos_plugin_evidence.workflows import (
        build_reference_workflow as _build_reference_workflow,
    )  # type: ignore[assignment]
    # reason: load_plugin returns PluginManifest which carries a Callable[[], Workflow]
    # in .workflows['reference']; the direct import is already the Callable, so mypy
    # sees a type mismatch that is safe at runtime (both call signatures are identical).

from kairos import WorkflowStatus  # noqa: E402

from examples._fixtures import (  # noqa: E402
    CONFLICTING_SOURCES,
    FIXTURE_FAMILIES,
    G1_FAMILIES,
    INJECTION_SENTINEL,
    POISONED_INJECTION,
    POISONED_STRUCTURAL_SPOOF,
    ingest_mcp_documents,
    make_grounded_model,
    make_refusal_model,
)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class G1Row:
    """Single row of the G1 generality table."""

    family_id: str
    overall_verdict: str
    answer: str
    answer_correct: bool
    passed: bool


@dataclass
class G2Row:
    """Before/after pair for the G2 baseline comparison."""

    family_id: str
    baseline_answer: str
    pipeline_answer: str
    baseline_refused: bool
    pipeline_correct: bool
    passed: bool


@dataclass
class QuickstartResult:
    """Aggregated results of all four acceptance gates.

    Attributes:
        g1_rows: Per-family G1 generality results.
        g2_rows: Per-family G2 before/after results.
        g3_passed: Whether injection sentinel was absent from all derived state.
        g3_notes: Human-readable G3 diagnosis.
        g4_passed: Whether the conflicting fixture rendered [DISPUTED].
        g4_notes: Human-readable G4 diagnosis.
        load_plugin_path: 'load_plugin' or 'direct_import'.
    """

    g1_rows: list[G1Row] = field(default_factory=list)
    g2_rows: list[G2Row] = field(default_factory=list)
    g3_passed: bool = False
    g3_notes: str = ""
    g4_passed: bool = False
    g4_notes: str = ""
    load_plugin_path: str = "direct_import"

    @property
    def all_passed(self) -> bool:
        """True iff all four gates passed."""
        return (
            all(r.passed for r in self.g1_rows)
            and all(r.passed for r in self.g2_rows)
            and self.g3_passed
            and self.g4_passed
        )


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------


def _run_reference_workflow(
    family: dict[str, Any],
    as_of: str,
) -> dict[str, Any]:
    """Run the reference workflow over a fixture family.  Returns final_state."""
    wf = _build_reference_workflow(today=None)
    docs = ingest_mcp_documents(family["documents"])
    result = wf.run(
        {
            "raw_documents": docs,
            "claims": family["claims"],
            "query": family["query"],
            "as_of": as_of,
        }
    )
    if result.status != WorkflowStatus.COMPLETE:
        raise RuntimeError(
            f"Reference workflow did not complete: {result.status}; "
            f"step_results={result.step_results!r}"
        )
    return dict(result.final_state)


def _sentinel_in_derived(final_state: dict[str, Any]) -> bool:
    """Check INJECTION_SENTINEL in derived state (excludes initial input keys)."""
    _initial_keys = frozenset({"raw_documents", "claims", "query", "as_of"})
    derived = {k: v for k, v in final_state.items() if k not in _initial_keys}
    try:
        return INJECTION_SENTINEL in json.dumps(derived)
    except (TypeError, ValueError):
        return False


def _run_g1(as_of: str, grounded_model: Any) -> list[G1Row]:
    """G1 — Generality: pipeline over families 1–3 produces verified + correct answer."""
    rows: list[G1Row] = []
    for fid in G1_FAMILIES:
        family = FIXTURE_FAMILIES[fid]
        expected = family.get("expected", {})
        try:
            final_state = _run_reference_workflow(family, as_of)
        except RuntimeError as exc:
            rows.append(
                G1Row(
                    family_id=fid,
                    overall_verdict="ERROR",
                    answer=str(exc),
                    answer_correct=False,
                    passed=False,
                )
            )
            continue

        packet: dict[str, Any] = final_state.get("evidence_packet") or {}
        overall_verdict = str(packet.get("overall_verdict", "")) if isinstance(packet, dict) else ""
        bundle: dict[str, Any] = final_state.get("working_context_bundle") or {}
        working_context = str(bundle.get("working_context", "")) if isinstance(bundle, dict) else ""

        grounded_key = expected.get("grounded_answer_contains", "")
        answer = grounded_model(f"{working_context}\n\nQUESTION: {family['query']}")
        answer_correct = grounded_key.lower() in answer.lower() if grounded_key else True

        expected_verdict = expected.get("overall_verdict", "verified")
        passed = overall_verdict == expected_verdict and answer_correct
        rows.append(
            G1Row(
                family_id=fid,
                overall_verdict=overall_verdict,
                answer=answer,
                answer_correct=answer_correct,
                passed=passed,
            )
        )
    return rows


def _run_g2(as_of: str, grounded_model: Any, refusal_model: Any) -> list[G2Row]:
    """G2 — Before/after: baseline refuses (cutoff fixation) vs pipeline answers correctly."""
    rows: list[G2Row] = []
    for fid in G1_FAMILIES:
        family = FIXTURE_FAMILIES[fid]
        expected = family.get("expected", {})
        try:
            final_state = _run_reference_workflow(family, as_of)
        except RuntimeError as exc:
            rows.append(
                G2Row(
                    family_id=fid,
                    baseline_answer="ERROR",
                    pipeline_answer=str(exc),
                    baseline_refused=False,
                    pipeline_correct=False,
                    passed=False,
                )
            )
            continue

        bundle: dict[str, Any] = final_state.get("working_context_bundle") or {}
        working_context = str(bundle.get("working_context", "")) if isinstance(bundle, dict) else ""
        query = family["query"]

        baseline_answer = refusal_model(query)
        pipeline_answer = grounded_model(f"{working_context}\n\nQUESTION: {query}")

        refusal_key = expected.get("refusal_answer_contains", "training")
        grounded_key = expected.get("grounded_answer_contains", "")

        baseline_refused = refusal_key.lower() in baseline_answer.lower()
        pipeline_correct = grounded_key.lower() in pipeline_answer.lower() if grounded_key else True

        rows.append(
            G2Row(
                family_id=fid,
                baseline_answer=baseline_answer,
                pipeline_answer=pipeline_answer,
                baseline_refused=baseline_refused,
                pipeline_correct=pipeline_correct,
                passed=baseline_refused and pipeline_correct,
            )
        )
    return rows


def _run_g3(as_of: str) -> tuple[bool, str]:
    """G3 — Injection containment: two sub-checks.

    Sub-check A: INJECTION_SENTINEL absent from all derived state (rejected-doc path).
    Sub-check B: structural-spoof document that SURVIVES the gate produces no forged
    col-0 structural headers ([VERIFIED FACT], OVERALL VERDICT:, [DISPUTED], NOTE:)
    in working_context — verifies SEV-001 fix (_oneline coercion in belief_revision).
    """
    notes_parts: list[str] = []

    # --- Sub-check A: rejected poisoned document ---
    family_a = POISONED_INJECTION
    wf_a = _build_reference_workflow(today=None)
    docs_a = ingest_mcp_documents(family_a["documents"])
    result_a = wf_a.run(
        {
            "raw_documents": docs_a,
            "claims": family_a["claims"],
            "query": family_a["query"],
            "as_of": as_of,
        }
    )

    final_state_a = dict(result_a.final_state)
    sentinel_in_state = _sentinel_in_derived(final_state_a)

    if sentinel_in_state:
        notes_parts.append("FAIL: sentinel found in derived final_state")
    else:
        notes_parts.append("sentinel absent from derived final_state")

    for step_name, step_result in result_a.step_results.items():
        output = step_result.output
        try:
            output_json = json.dumps(output)
            if INJECTION_SENTINEL in output_json:
                notes_parts.append(f"FAIL: sentinel found in {step_name} output")
            else:
                notes_parts.append(f"{step_name} output: clean")
        except (TypeError, ValueError):
            notes_parts.append(f"{step_name} output: not JSON serializable")

    # --- Sub-check B: surviving structural-spoof document ---
    family_b = POISONED_STRUCTURAL_SPOOF
    wf_b = _build_reference_workflow(today=None)
    docs_b = ingest_mcp_documents(family_b["documents"])
    result_b = wf_b.run(
        {
            "raw_documents": docs_b,
            "claims": family_b["claims"],
            "query": family_b["query"],
            "as_of": as_of,
        }
    )

    bundle_b: dict[str, Any] = result_b.final_state.get("working_context_bundle") or {}
    wc_b: str = bundle_b.get("working_context", "") if isinstance(bundle_b, dict) else ""

    # SEV-001 check: _oneline() in belief_revision collapses \n[VERIFIED FACT] injection
    # to inline text on the "  [Sx]: ..." citation line, never as a standalone col-0 header.
    wc_lines = wc_b.split("\n")
    vf_col0 = [ln for ln in wc_lines if ln.startswith("[VERIFIED FACT]")]
    ov_col0 = [ln for ln in wc_lines if ln.startswith("OVERALL VERDICT:")]
    disp_col0 = [ln for ln in wc_lines if ln.startswith("[DISPUTED]")]

    spoof_failures: list[str] = []
    # Spoof fixture has exactly 1 genuine supported claim → expect exactly 1 [VERIFIED FACT]
    if len(vf_col0) != 1:
        spoof_failures.append(f"FAIL: {len(vf_col0)} col-0 [VERIFIED FACT] lines (want 1)")
    # Exactly 1 genuine OVERALL VERDICT: line
    if len(ov_col0) != 1:
        spoof_failures.append(f"FAIL: {len(ov_col0)} col-0 OVERALL VERDICT: lines (want 1)")
    # No [DISPUTED] col-0 lines (no conflicting claims in spoof fixture)
    if disp_col0:
        spoof_failures.append(f"FAIL: forged [DISPUTED] col-0 line(s): {disp_col0[:2]!r}")

    if spoof_failures:
        notes_parts.extend(spoof_failures)
    else:
        notes_parts.append(
            "structural-spoof surviving doc: no forged col-0 headers (SEV-001 clean)"
        )

    passed = not sentinel_in_state and not any("FAIL" in n for n in notes_parts)
    return passed, "; ".join(notes_parts)


def _run_g4(as_of: str, grounded_model: Any) -> tuple[bool, str]:
    """G4 — Honest uncertainty: conflicting fixture renders [DISPUTED]."""
    family = CONFLICTING_SOURCES
    final_state = _run_reference_workflow(family, as_of)

    bundle: dict[str, Any] = final_state.get("working_context_bundle") or {}
    working_context = str(bundle.get("working_context", "")) if isinstance(bundle, dict) else ""
    unresolved = bundle.get("unresolved_conflicts", []) if isinstance(bundle, dict) else []

    packet: dict[str, Any] = final_state.get("evidence_packet") or {}
    overall_verdict = str(packet.get("overall_verdict", "")) if isinstance(packet, dict) else ""

    disputed_present = "[DISPUTED]" in working_context
    conflicts_non_empty = isinstance(unresolved, list) and len(unresolved) > 0
    verdict_conflicting = overall_verdict == "conflicting"

    answer = grounded_model(f"{working_context}\n\nQUESTION: {family['query']}")
    answer_expresses_uncertainty = any(
        w in answer.lower()
        for w in ("conflict", "disagree", "cannot confirm", "dispute", "unclear")
    )

    notes = (
        f"overall_verdict={overall_verdict!r} (want 'conflicting'): "
        f"{'OK' if verdict_conflicting else 'FAIL'}; "
        f"[DISPUTED] in working_context: {'OK' if disputed_present else 'FAIL'}; "
        f"unresolved_conflicts non-empty: {'OK' if conflicts_non_empty else 'FAIL'}; "
        f"answer expresses uncertainty: {'OK' if answer_expresses_uncertainty else 'FAIL'}"
    )
    passed = verdict_conflicting and disputed_present and conflicts_non_empty
    return passed, notes


# ---------------------------------------------------------------------------
# Public run() helper (callable from tests)
# ---------------------------------------------------------------------------


def run() -> QuickstartResult:
    """Run all four acceptance gates offline and return a QuickstartResult.

    Machine-stamps as_of from the system clock (Case 3 addendum: never
    user-typed, re-stamped every invocation).

    Returns:
        QuickstartResult with G1–G4 gate outcomes.
    """
    # Machine-stamp as_of at invocation — never carried from a previous run.
    as_of: str = datetime.now(tz=UTC).date().isoformat()

    grounded = make_grounded_model()
    refusal = make_refusal_model()

    result = QuickstartResult(load_plugin_path=_LOAD_PLUGIN_PATH)
    result.g1_rows = _run_g1(as_of, grounded)
    result.g2_rows = _run_g2(as_of, grounded, refusal)
    result.g3_passed, result.g3_notes = _run_g3(as_of)
    result.g4_passed, result.g4_notes = _run_g4(as_of, grounded)
    return result


# ---------------------------------------------------------------------------
# main() — print results
# ---------------------------------------------------------------------------


def _hr(char: str = "-", width: int = 72) -> str:
    return char * width


def main() -> None:
    """Run the quickstart demo and print all gates.  Exits non-zero on failure."""
    print(_hr("="))  # noqa: T201
    print("kairos-plugin-evidence — Offline Quickstart")  # noqa: T201
    print("Running acceptance gates (scripted model, fully offline)...")  # noqa: T201
    print(_hr("="))  # noqa: T201
    print()  # noqa: T201

    result = run()

    print(f"Plugin loaded via: {result.load_plugin_path}")  # noqa: T201
    print()  # noqa: T201

    # G1
    print(_hr("="))  # noqa: T201
    print("G1 — GENERALITY")  # noqa: T201
    print(_hr())  # noqa: T201
    header = f"{'Family':<40} {'Verdict':<12} {'Answer?':<8} {'Gate'}"
    print(header)  # noqa: T201
    print(_hr())  # noqa: T201
    for row in result.g1_rows:
        gate = "PASS" if row.passed else "FAIL"
        correct = "yes" if row.answer_correct else "NO"
        print(f"{row.family_id:<40} {row.overall_verdict:<12} {correct:<8} {gate}")  # noqa: T201
    g1_ok = all(r.passed for r in result.g1_rows)
    print(_hr())  # noqa: T201
    print(f"G1 overall: {'PASS' if g1_ok else 'FAIL'}")  # noqa: T201

    # G2
    print()  # noqa: T201
    print(_hr("="))  # noqa: T201
    print("G2 — BEFORE / AFTER DELTA")  # noqa: T201
    for row in result.g2_rows:
        print(_hr())  # noqa: T201
        print(f"Fixture: {row.family_id}")  # noqa: T201
        print(f"  BASELINE (no context): {row.baseline_answer[:100]}")  # noqa: T201
        print(f"  PIPELINE (with context): {row.pipeline_answer[:100]}")  # noqa: T201
        gate = "PASS" if row.passed else "FAIL"
        print(  # noqa: T201
            f"  baseline_refused={row.baseline_refused}  "
            f"pipeline_correct={row.pipeline_correct}  -> {gate}"
        )
    g2_ok = all(r.passed for r in result.g2_rows)
    print(_hr())  # noqa: T201
    print(f"G2 overall: {'PASS' if g2_ok else 'FAIL'}")  # noqa: T201

    # G3
    print()  # noqa: T201
    print(_hr("="))  # noqa: T201
    print("G3 — INJECTION CONTAINMENT")  # noqa: T201
    print(_hr())  # noqa: T201
    print(f"Notes: {result.g3_notes}")  # noqa: T201
    print(_hr())  # noqa: T201
    print(f"G3 overall: {'PASS' if result.g3_passed else 'FAIL'}")  # noqa: T201

    # G4
    print()  # noqa: T201
    print(_hr("="))  # noqa: T201
    print("G4 — HONEST UNCERTAINTY")  # noqa: T201
    print(_hr())  # noqa: T201
    print(f"Notes: {result.g4_notes}")  # noqa: T201
    print(_hr())  # noqa: T201
    print(f"G4 overall: {'PASS' if result.g4_passed else 'FAIL'}")  # noqa: T201

    # Final
    print()  # noqa: T201
    print(_hr("="))  # noqa: T201
    overall = "ALL PASS" if result.all_passed else "SOME GATES FAILED"
    print(f"FINAL: {overall}")  # noqa: T201
    print(_hr("="))  # noqa: T201

    if not result.all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
