"""kairos-plugin-evidence workflows — reference workflow factory (C4).

Provides ``build_reference_workflow``: wires the four plugin steps with the
02 §2 scoped-state walls (+ F2 extension for query/as_of). The answer/model
step is deliberately NOT included — that step is the user's (02 §2) — so the
plugin carries zero model coupling and remains fully deterministic offline.

Scoped-state walls enforce EE-1: the user's answer step (which should be
added by the caller with read_keys=["working_context_bundle", "query"]) cannot
structurally reach raw_documents, sources, rejected, or gate_warnings.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from kairos import Step, Workflow

from kairos_plugin_evidence.belief_revision import belief_revision_builder
from kairos_plugin_evidence.claim_extractor import claim_extractor
from kairos_plugin_evidence.content_gate import content_gate
from kairos_plugin_evidence.contracts import (
    BUILDER_OUTPUT,
    EVALUATOR_OUTPUT,
    EXTRACTOR_OUTPUT,
    GATE_OUTPUT,
)
from kairos_plugin_evidence.evidence_evaluator import make_evidence_evaluator

# Default step names used in the reference workflow.
_STEP_CONTENT_GATE: str = "content_gate"
_STEP_CLAIM_EXTRACTOR: str = "claim_extractor"
_STEP_EVIDENCE_EVALUATOR: str = "evidence_evaluator"
_STEP_BELIEF_REVISION: str = "belief_revision_builder"


def build_reference_workflow(
    *,
    trust_policy: dict[str, Any] | None = None,
    noise_phrases: list[str] | None = None,
    today: date | None = None,
    name: str = "evidence-reference",
    max_llm_calls: int = 10,
) -> Workflow:
    """Wire the 4 plugin steps with the 02 §2 scoped-state walls (+ F2 extension).

    The returned workflow expects these initial inputs at ``run()``:
    ``{raw_documents, claims, query, as_of}``.

    Scoped-state map (02 §2 + F2):

    * content_gate: reads [raw_documents], writes [sources, rejected, gate_warnings]
    * claim_extractor: reads [claims], writes [claim_records]
    * evidence_evaluator: reads [claim_records, sources, query, as_of],
      writes [evidence_packet]
    * belief_revision_builder: reads [evidence_packet], writes [working_context_bundle]

    The user's answer step should be added **after** this workflow with::

        Step("answer", answer_fn,
             depends_on=["belief_revision_builder"],
             read_keys=["working_context_bundle", "query"],
             write_keys=["answer"])

    Args:
        trust_policy: TrustPolicy config dict threaded into the evaluator factory.
        noise_phrases: Custom noise phrases to suppress during value extraction.
        today: Override for the evaluator's ``resolve_as_of`` clock (for testing).
        name: Workflow name (must match ``[a-zA-Z0-9_-]+``).
        max_llm_calls: LLM call circuit-breaker limit (default 10; the reference
            workflow makes zero LLM calls itself — this cap applies to user answer
            steps added downstream).

    Returns:
        A ``Workflow`` with four plugin steps and correctly configured scoped walls.

    Raises:
        ConfigError: If ``trust_policy`` or ``noise_phrases`` are malformed
            (raised by the evaluator factory at construction time — EE-5).
    """
    evaluator_fn = make_evidence_evaluator(
        trust_policy=trust_policy,
        noise_phrases=noise_phrases,
        today=today,
    )

    return Workflow(
        name=name,
        steps=[
            Step(
                name=_STEP_CONTENT_GATE,
                action=content_gate,
                read_keys=["raw_documents"],
                write_keys=["sources", "rejected", "gate_warnings"],
                output_contract=GATE_OUTPUT,
            ),
            Step(
                name=_STEP_CLAIM_EXTRACTOR,
                action=claim_extractor,
                read_keys=["claims"],
                write_keys=["claim_records"],
                output_contract=EXTRACTOR_OUTPUT,
            ),
            Step(
                name=_STEP_EVIDENCE_EVALUATOR,
                action=evaluator_fn,
                depends_on=[_STEP_CONTENT_GATE, _STEP_CLAIM_EXTRACTOR],
                # F2: evaluator reads query + as_of beyond the 02 §2 scoped map.
                read_keys=["claim_records", "sources", "query", "as_of"],
                write_keys=["evidence_packet"],
                output_contract=EVALUATOR_OUTPUT,
            ),
            Step(
                name=_STEP_BELIEF_REVISION,
                action=belief_revision_builder,
                depends_on=[_STEP_EVIDENCE_EVALUATOR],
                read_keys=["evidence_packet"],
                write_keys=["working_context_bundle"],
                output_contract=BUILDER_OUTPUT,
            ),
        ],
        max_llm_calls=max_llm_calls,
    )
