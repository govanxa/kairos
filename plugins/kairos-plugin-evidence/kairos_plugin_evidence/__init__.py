"""kairos-plugin-evidence — Evidence Engine plugin for the Kairos SDK.

Public surface: MANIFEST (the entry-point target for kairos.plugins) plus
re-exports of the contracts and content_gate public API for ergonomic imports.

Example::

    from kairos_plugin_evidence import EVIDENCE_PACKET, make_packet
    from kairos_plugin_evidence import gate_documents, registrable_domain

MANIFEST carries content_gate at C2; C3–C4 append their step actions.
"""

from __future__ import annotations

from kairos.plugins.registry import build_manifest

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

__all__ = [
    # Manifest — entry-point target (B2 requirement)
    "MANIFEST",
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
# C2 registers content_gate; C3–C4 register their @step_plugin callables.
MANIFEST = build_manifest(
    name="evidence",
    version="0.1.0",
    description="Evidence Engine — contract-validated evidence evaluation for Kairos workflows.",
    requires_kairos=">=0.5,<0.6",
    steps=(content_gate,),
    validators=(),
)
