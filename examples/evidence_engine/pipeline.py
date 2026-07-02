"""Evidence Engine pipeline — Workflow wiring (→ C4).

build_pipeline: full evidence-engine pipeline (gate → extractor → evaluator
→ builder → answer). Scoped-state wall enforces EE-1 (no raw documents
downstream of the gate).

build_baseline: single answer step, no gate/evaluator, reads only 'query'.
The G2 no-firewall baseline — structurally CANNOT reach web-derived state keys.

F2 finding (blueprint §8): evaluator read_keys extended to include 'query'
and 'as_of' (required by EvidencePacket — 03 §6) beyond the 02 §2 scoped map.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from examples.evidence_engine.answer import make_answer_step
from examples.evidence_engine.belief_revision_builder import belief_revision_builder
from examples.evidence_engine.claim_extractor import claim_extractor
from examples.evidence_engine.content_gate import content_gate
from examples.evidence_engine.contracts import (
    BUILDER_OUTPUT,
    EVIDENCE_PACKET,
    EXTRACTOR_OUTPUT,
    GATE_OUTPUT,
)
from examples.evidence_engine.evidence_evaluator import make_evidence_evaluator
from kairos import Step, Workflow


def build_pipeline(
    *,
    model_fn: Callable[[str], str],
    trust_policy: dict[str, Any] | None = None,
) -> Workflow:
    """Build the full evidence-engine Workflow.

    Scoped-state map (02 §2 + F2 extension):
    - content_gate:           read raw_documents, as_of → write sources, rejected, gate_warnings
    - claim_extractor:        read claims           → write claim_records
    - evidence_evaluator:     read claim_records, sources, query, as_of → write evidence_packet
    - belief_revision_builder: read evidence_packet  → write working_context_bundle
    - answer:                 read working_context_bundle, query → write answer

    Args:
        model_fn: Callable[[str], str] for the answer step (scripted or live).
        trust_policy: Optional TrustPolicy config dict passed to the evaluator.

    Returns:
        A Workflow ready to run with initial inputs:
        {raw_documents, claims, query, as_of}.
    """
    return Workflow(
        name="evidence-engine-spike",
        steps=[
            Step(
                "content_gate",
                content_gate,
                read_keys=["raw_documents", "as_of"],
                write_keys=["sources", "rejected", "gate_warnings"],
                output_contract=GATE_OUTPUT,
            ),
            Step(
                "claim_extractor",
                claim_extractor,
                read_keys=["claims"],
                write_keys=["claim_records"],
                output_contract=EXTRACTOR_OUTPUT,
            ),
            Step(
                "evidence_evaluator",
                make_evidence_evaluator(trust_policy),
                depends_on=["content_gate", "claim_extractor"],
                # F2: evaluator reads query + as_of in addition to 02 §2 scoped map.
                read_keys=["claim_records", "sources", "query", "as_of"],
                write_keys=["evidence_packet"],
                output_contract=EVIDENCE_PACKET,
            ),
            Step(
                "belief_revision_builder",
                belief_revision_builder,
                depends_on=["evidence_evaluator"],
                read_keys=["evidence_packet"],
                write_keys=["working_context_bundle"],
                output_contract=BUILDER_OUTPUT,
            ),
            Step(
                "answer",
                make_answer_step(model_fn, with_context=True),
                depends_on=["belief_revision_builder"],
                read_keys=["working_context_bundle", "query"],
                write_keys=["answer"],
            ),
        ],
        max_llm_calls=10,
    )


def build_baseline(*, model_fn: Callable[[str], str]) -> Workflow:
    """Build the no-firewall baseline Workflow for the G2 delta comparison.

    A single answer step. read_keys=['query'] means this workflow structurally
    CANNOT reach raw_documents, sources, rejected, or any gate output — the
    baseline prompt contains only the bare user question.

    Args:
        model_fn: Callable[[str], str] for the answer step (typically a
            ScriptedModel in 'refusal' mode for the G2 simulation).

    Returns:
        A Workflow ready to run with initial inputs: {query}.
    """
    return Workflow(
        name="evidence-engine-baseline",
        steps=[
            Step(
                "answer",
                make_answer_step(model_fn, with_context=False),
                read_keys=["query"],
                write_keys=["answer"],
            ),
        ],
        max_llm_calls=10,
    )
