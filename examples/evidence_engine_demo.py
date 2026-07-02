"""Evidence Engine concept-spike demo — runs the G1–G4 acceptance harness.

Usage:
    python examples/evidence_engine_demo.py

Prints:
  G1 generality table (query type, verdict, confidence, answer-correct)
  G2 before/after transcript (baseline vs pipeline side-by-side)
  G3 injection-containment output
  G4 honest-uncertainty transcript

All gates use the ScriptedModel (fully offline). No real API calls are made.
"""

from __future__ import annotations

from examples.evidence_engine.harness import run_acceptance


def _hr(char: str = "-", width: int = 72) -> str:
    return char * width


def print_g1(report: object) -> None:
    from examples.evidence_engine.harness import HarnessReport

    assert isinstance(report, HarnessReport)
    print(_hr("="))
    print("G1 — GENERALITY")
    print(_hr())
    header = f"{'Family':<35} {'Verdict':<12} {'Conf':<10} {'Answer?':<8} {'Gate'}"
    print(header)
    print(_hr("-"))
    for row in report.g1_rows:
        gate = "PASS" if row.passed else "FAIL"
        correct = "yes" if row.answer_correct else "NO"
        cells = f"{row.overall_verdict:<12} {row.confidence:<10} {correct:<8} {gate}"
        print(f"{row.family_id:<35} {cells}")
    all_g1 = all(r.passed for r in report.g1_rows)
    print(_hr())
    print(f"G1 overall: {'PASS' if all_g1 else 'FAIL'}")


def print_g2(report: object) -> None:
    from examples.evidence_engine.harness import HarnessReport

    assert isinstance(report, HarnessReport)
    print()
    print(_hr("="))
    print("G2 — BEFORE / AFTER DELTA")
    for row in report.g2_rows:
        print(_hr())
        print(f"Fixture: {row.family_id}")
        print(f"  BASELINE (no firewall): {row.baseline_answer[:120]}")
        print(f"  PIPELINE (with context): {row.pipeline_answer[:120]}")
        gate = "PASS" if row.passed else "FAIL"
        print(
            f"  baseline_refused={row.baseline_refused}  "
            f"pipeline_correct={row.pipeline_correct}  -> {gate}"
        )
    all_g2 = all(r.passed for r in report.g2_rows)
    print(_hr())
    print(f"G2 overall: {'PASS' if all_g2 else 'FAIL'}")


def print_g3(report: object) -> None:
    from examples.evidence_engine.harness import HarnessReport

    assert isinstance(report, HarnessReport)
    print()
    print(_hr("="))
    print("G3 — INJECTION CONTAINMENT")
    print(_hr())
    print(f"Notes: {report.g3_notes}")
    print(_hr())
    print(f"G3 overall: {'PASS' if report.g3_passed else 'FAIL'}")


def print_g4(report: object) -> None:
    from examples.evidence_engine.harness import HarnessReport

    assert isinstance(report, HarnessReport)
    print()
    print(_hr("="))
    print("G4 — HONEST UNCERTAINTY")
    print(_hr())
    print(f"Notes: {report.g4_notes}")
    print(_hr())
    print(f"G4 overall: {'PASS' if report.g4_passed else 'FAIL'}")


def main() -> None:
    print("Evidence Engine — A1 Concept Spike")
    print("Running acceptance harness (scripted model, fully offline)...")
    print()

    report = run_acceptance()

    print_g1(report)
    print_g2(report)
    print_g3(report)
    print_g4(report)

    print()
    print(_hr("="))
    overall = "ALL PASS" if report.all_passed else "SOME GATES FAILED"
    print(f"FINAL: {overall}")
    print(_hr("="))

    if not report.all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
