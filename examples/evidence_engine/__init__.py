"""Evidence Engine concept spike package — public surface (→ C1–C4 extraction targets).

Import from this package or directly from the submodules.
"""

from __future__ import annotations

from examples.evidence_engine.answer import ScriptedModel, live_model_fn, make_answer_step
from examples.evidence_engine.belief_revision_builder import (
    CLOSING_FRAME,
    TEMPORAL_ANCHOR,
    belief_revision_builder,
    render_working_context,
)
from examples.evidence_engine.claim_extractor import claim_extractor, extract_claims
from examples.evidence_engine.content_gate import (
    REJECTION_REASONS,
    content_gate,
    gate_documents,
    registrable_domain,
)
from examples.evidence_engine.contracts import (
    BUILDER_OUTPUT,
    CLAIM_RECORD,
    EVIDENCE_PACKET,
    EXTRACTOR_INPUT,
    EXTRACTOR_OUTPUT,
    GATE_INPUT,
    GATE_OUTPUT,
    PACKET_VERSION,
    SOURCE_RECORD,
    derive_confidence,
    derive_overall_verdict,
    derive_support_level,
    derive_verdict,
    make_claim_record,
    make_packet,
    make_source_record,
)
from examples.evidence_engine.evidence_evaluator import (
    TrustPolicy,
    assign_independence_groups,
    classify_freshness,
    classify_tier,
    detect_conflicts,
    extract_values,
    make_evidence_evaluator,
)
from examples.evidence_engine.fixtures import FIXTURE_FAMILIES, INJECTION_SENTINEL, load_fixture
from examples.evidence_engine.harness import HarnessReport, run_acceptance
from examples.evidence_engine.pipeline import build_baseline, build_pipeline
from examples.evidence_engine.untrusted_text import (
    FLAG_IMPERATIVE,
    FLAG_ROLE_MARKER,
    FLAG_TEMPLATE_TOKEN,
    FLAG_TOOL_CALL,
    SanitizedText,
    is_predominantly_instructional,
    neutralize,
    normalize,
    sanitize_untrusted_text,
    scrub_credentials,
)

__all__ = [
    # untrusted_text
    "FLAG_IMPERATIVE",
    "FLAG_ROLE_MARKER",
    "FLAG_TEMPLATE_TOKEN",
    "FLAG_TOOL_CALL",
    "SanitizedText",
    "is_predominantly_instructional",
    "neutralize",
    "normalize",
    "sanitize_untrusted_text",
    "scrub_credentials",
    # contracts
    "BUILDER_OUTPUT",
    "CLAIM_RECORD",
    "EVIDENCE_PACKET",
    "EXTRACTOR_INPUT",
    "EXTRACTOR_OUTPUT",
    "GATE_INPUT",
    "GATE_OUTPUT",
    "PACKET_VERSION",
    "SOURCE_RECORD",
    "derive_confidence",
    "derive_overall_verdict",
    "derive_support_level",
    "derive_verdict",
    "make_claim_record",
    "make_packet",
    "make_source_record",
    # content_gate
    "REJECTION_REASONS",
    "content_gate",
    "gate_documents",
    "registrable_domain",
    # claim_extractor
    "claim_extractor",
    "extract_claims",
    # evidence_evaluator
    "TrustPolicy",
    "assign_independence_groups",
    "classify_freshness",
    "classify_tier",
    "detect_conflicts",
    "extract_values",
    "make_evidence_evaluator",
    # belief_revision_builder
    "CLOSING_FRAME",
    "TEMPORAL_ANCHOR",
    "belief_revision_builder",
    "render_working_context",
    # answer
    "ScriptedModel",
    "live_model_fn",
    "make_answer_step",
    # pipeline
    "build_baseline",
    "build_pipeline",
    # fixtures
    "FIXTURE_FAMILIES",
    "INJECTION_SENTINEL",
    "load_fixture",
    # harness
    "HarnessReport",
    "run_acceptance",
]
