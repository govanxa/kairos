"""kairos-plugin-evidence — Evidence Engine plugin for the Kairos SDK.

Public surface: MANIFEST (the entry-point target for kairos.plugins) plus
re-exports of the contracts and step/workflow public API for ergonomic imports.

Example::

    from kairos_plugin_evidence import EVIDENCE_PACKET, make_packet
    from kairos_plugin_evidence import gate_documents, registrable_domain
    from kairos_plugin_evidence import make_evidence_evaluator, TrustPolicy
    from kairos_plugin_evidence import render_working_context, build_reference_workflow

MANIFEST carries all four step actions (C2–C4) and the reference workflow factory.
"""

from __future__ import annotations

from kairos.plugins.registry import build_manifest

from kairos_plugin_evidence.belief_revision import (
    belief_revision_builder,
    render_working_context,
)
from kairos_plugin_evidence.claim_extractor import claim_extractor, extract_claims
from kairos_plugin_evidence.content_gate import (
    REJECTION_REASONS,
    content_gate,
    gate_documents,
    registrable_domain,
)
from kairos_plugin_evidence.contracts import (
    BUILDER_OUTPUT,
    CLAIM_RECORD,
    EVALUATOR_INPUT,
    EVALUATOR_OUTPUT,
    EVIDENCE_PACKET,
    EXTRACTOR_INPUT,
    EXTRACTOR_OUTPUT,
    GATE_INPUT,
    GATE_OUTPUT,
    PACKET_VERSION,
    SOURCE_RECORD,
    SUPPORTED_PACKET_VERSIONS,
    ClaimKind,
    Confidence,
    Freshness,
    InjectionFlag,
    OverallVerdict,
    ProvenanceTier,
    SupportLevel,
    TimeSensitivity,
    Verdict,
    derive_confidence,
    derive_overall_verdict,
    derive_support_level,
    derive_verdict,
    is_supported_packet_version,
    make_claim_record,
    make_packet,
    make_source_record,
)
from kairos_plugin_evidence.evidence_evaluator import (
    TrustPolicy,
    assign_independence_groups,
    classify_freshness,
    classify_tier,
    compose_warnings,
    detect_conflicts,
    evidence_evaluator,
    extract_values,
    make_evidence_evaluator,
    normalize_value,
    resolve_as_of,
)
from kairos_plugin_evidence.workflows import build_reference_workflow

__all__ = [
    # Manifest — entry-point target (B2 requirement)
    "MANIFEST",
    # C4 belief revision builder
    "belief_revision_builder",
    "render_working_context",
    # C4 reference workflow factory
    "build_reference_workflow",
    # C3 claim extractor
    "claim_extractor",
    "extract_claims",
    # C3 evidence evaluator — step action + factory + policy
    "evidence_evaluator",
    "make_evidence_evaluator",
    "TrustPolicy",
    # C3 extraction core
    "extract_values",
    "normalize_value",
    # C3 classification + composition
    "classify_tier",
    "classify_freshness",
    "assign_independence_groups",
    "detect_conflicts",
    "compose_warnings",
    "resolve_as_of",
    # C2 content gate
    "content_gate",
    "gate_documents",
    "registrable_domain",
    "REJECTION_REASONS",
    # Versioning
    "PACKET_VERSION",
    "SUPPORTED_PACKET_VERSIONS",
    "is_supported_packet_version",
    # Enums
    "ProvenanceTier",
    "Freshness",
    "ClaimKind",
    "TimeSensitivity",
    "SupportLevel",
    "Verdict",
    "OverallVerdict",
    "Confidence",
    "InjectionFlag",
    # Record schemas
    "SOURCE_RECORD",
    "CLAIM_RECORD",
    "EVIDENCE_PACKET",
    # Per-step I/O schemas
    "GATE_INPUT",
    "GATE_OUTPUT",
    "EXTRACTOR_INPUT",
    "EXTRACTOR_OUTPUT",
    "EVALUATOR_INPUT",
    "EVALUATOR_OUTPUT",
    "BUILDER_OUTPUT",
    # Constructors
    "make_source_record",
    "make_claim_record",
    "make_packet",
    # Derivation functions
    "derive_support_level",
    "derive_verdict",
    "derive_overall_verdict",
    "derive_confidence",
]

# MANIFEST is the entry-point target declared in pyproject.toml:
#   [project.entry-points."kairos.plugins"]
#   evidence = "kairos_plugin_evidence:MANIFEST"
#
# C2: content_gate; C3: claim_extractor + evidence_evaluator; C4: belief_revision_builder.
# MANIFEST.workflows["reference"] = build_reference_workflow (plugin-system spec §11 slot).
MANIFEST = build_manifest(
    name="evidence",
    version="0.1.0",
    description="Evidence Engine — contract-validated evidence evaluation for Kairos workflows.",
    requires_kairos=">=0.5,<0.6",
    steps=(content_gate, claim_extractor, evidence_evaluator, belief_revision_builder),
    validators=(),
    workflows={"reference": build_reference_workflow},
)
