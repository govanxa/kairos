"""MCP tool logic — the security-critical core of the MCP server (D3).

Pure tool implementations, decoupled from the ``mcp`` SDK so this module (and
its test suite) never requires the optional dependency to be installed.

The critical invariant lives in ``build_evidence_response``: the MCP response
goes straight into the calling model's context, so it must be assembled ONLY
from gated/derived data — ``working_context_bundle`` (produced by the C4
belief-revision builder, itself built only from the C2-gated, C3-evaluated
``EvidencePacket``) plus a handful of structural ``EvidencePacket`` fields and
two integer counts. It NEVER reads ``raw_documents``, the ``rejected``
records, ``gate_warnings``, or raw source ``excerpt``/``title`` text (EE-1,
EE-2, carried from C2).

Every failure path — input-limit violations, retriever exceptions, pipeline
errors — returns the disjoint ``{"error": {"type": str, "message": str}}``
shape instead of raising, and every message crossing this boundary is either
a fixed structural ``InputLimitError`` string or has passed through
``kairos.security.sanitize_exception`` (T6: no raw content, no credentials,
no file paths, no stack traces ever cross the wire).

Every tool call emits exactly one structural INFO log line (Decision 5):
tool name, document/claim counts, query LENGTH (never text), verdict,
confidence, and elapsed time. Logging never includes query text, document
text, excerpts, or unsanitized error message bodies.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from kairos.enums import StepStatus, WorkflowStatus
from kairos.security import sanitize_exception

from kairos_ai_evidence import build_reference_workflow
from kairos_ai_evidence.mcp.limits import (
    InputLimitError,
    clamp_max_results,
    stamp_now,
    stamp_today,
    validate_as_of,
    validate_claims,
    validate_documents,
    validate_query,
    validate_total_size,
)
from kairos_ai_evidence.mcp.retriever import Retriever, normalize_retrieved_documents

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RetrieverNotConfiguredError(RuntimeError):
    """Raised internally when ``verified_answer`` is called with no retriever.

    Caught within ``verified_answer_impl`` and converted into the structured
    ``{"error": {"type": "RetrieverNotConfigured", ...}}`` response — never
    propagates out of the impl functions.
    """


# ---------------------------------------------------------------------------
# Response assembly — the security-critical function
# ---------------------------------------------------------------------------


def _error_response(error_type: str, message: str) -> dict[str, Any]:
    """Build the disjoint error response shape.

    Args:
        error_type: A short, safe error-type label (never a raw class name
            derived from untrusted input; for sanitized exceptions this is
            the exception class name via ``sanitize_exception``).
        message: A safe, already-sanitized or fixed-structural message.

    Returns:
        ``{"error": {"type": error_type, "message": message}}`` — no other
        keys, so callers can detect failure by the presence of ``"error"``.
    """
    return {"error": {"type": error_type, "message": message}}


def _log_tool_call(
    tool_name: str,
    *,
    document_count: int,
    claim_count: int,
    query_length: int,
    elapsed_ms: float,
    response: dict[str, Any],
) -> None:
    """Emit exactly one structural INFO log line per MCP tool call.

    Logs ONLY counts, the query's LENGTH, verdict/confidence enum values (when
    present), an error-type label (when the call failed), and timing. NEVER
    logs query text, document text, excerpts, or unsanitized error message
    bodies — the response's ``error.message`` (already sanitized/structural)
    is deliberately omitted too, so logs stay a strict subset of what is
    already safe to put on the wire.

    Args:
        tool_name: ``"evaluate_evidence"`` or ``"verified_answer"``.
        document_count: Number of documents actually passed to the pipeline
            (post-validation/normalization/capping).
        claim_count: Number of claims actually passed to the pipeline.
        query_length: ``len(query)`` — never the query text itself.
        elapsed_ms: Wall-clock duration of the tool call in milliseconds.
        response: The response dict about to be returned to the caller.
    """
    error_block = response.get("error")
    error_type = error_block.get("type") if isinstance(error_block, dict) else None
    verdict = response.get("overall_verdict")
    confidence = response.get("confidence")
    logger.info(
        "mcp_tool_call tool=%s documents=%d claims=%d query_len=%d "
        "verdict=%s confidence=%s error=%s elapsed_ms=%.1f",
        tool_name,
        document_count,
        claim_count,
        query_length,
        verdict,
        confidence,
        error_type,
        elapsed_ms,
    )


def build_evidence_response(
    working_context_bundle: dict[str, Any],
    packet: dict[str, Any],
    *,
    sources_considered: int,
    sources_rejected: int,
) -> dict[str, Any]:
    """Assemble the EvidenceResponse from ONLY gated/derived fields.

    Reads exclusively:
    - ``working_context_bundle``: ``working_context``, ``superseded_assumptions``,
      ``unresolved_conflicts``, ``citations``, ``packet_id``.
    - ``packet`` (structural ``EvidencePacket`` fields): ``as_of``,
      ``overall_verdict``, ``confidence``, ``warnings``, ``packet_version``,
      ``assist_used``, and per-claim ``{claim_id, claim_text, verdict}``.
    - The two caller-supplied COUNTS.

    NEVER reads ``raw_documents``, the ``rejected`` records, ``gate_warnings``,
    or raw source ``excerpt``/``title`` text (EE-1, EE-2, carried C2). This
    function is total and defensive — malformed/missing fields degrade to
    safe defaults rather than raising, so a corrupted upstream state can never
    crash the MCP server mid-response.

    Args:
        working_context_bundle: The C4 ``belief_revision_builder`` output
            (``BUILDER_OUTPUT`` shape).
        packet: The C3 ``EvidencePacket`` dict.
        sources_considered: ``len(sources)`` from the gate — a COUNT only.
        sources_rejected: ``len(rejected)`` from the gate — a COUNT only,
            never the rejected records themselves (EE-2).

    Returns:
        The ``EvidenceResponse`` dict (see Decision 2 schema).
    """
    bundle = working_context_bundle if isinstance(working_context_bundle, dict) else {}
    pkt = packet if isinstance(packet, dict) else {}

    superseded_raw = bundle.get("superseded_assumptions")
    superseded_assumptions = (
        [str(item) for item in superseded_raw] if isinstance(superseded_raw, list) else []
    )

    conflicts_raw = bundle.get("unresolved_conflicts")
    unresolved_conflicts = (
        [str(item) for item in conflicts_raw] if isinstance(conflicts_raw, list) else []
    )

    citations_raw = bundle.get("citations")
    citations: list[dict[str, str]] = []
    if isinstance(citations_raw, list):
        for entry in citations_raw:
            if isinstance(entry, dict):
                citations.append(
                    {
                        "source_id": str(entry.get("source_id", "")),
                        "domain": str(entry.get("domain", "")),
                        "url": str(entry.get("url", "")),
                    }
                )

    warnings_raw = pkt.get("warnings")
    warnings = [str(item) for item in warnings_raw] if isinstance(warnings_raw, list) else []

    claims_raw = pkt.get("claims")
    claims: list[dict[str, str]] = []
    if isinstance(claims_raw, list):
        for claim in claims_raw:
            if isinstance(claim, dict):
                claims.append(
                    {
                        "claim_id": str(claim.get("claim_id", "")),
                        "claim_text": str(claim.get("claim_text", "")),
                        "verdict": str(claim.get("verdict", "")),
                    }
                )

    packet_id = str(bundle.get("packet_id") or pkt.get("packet_id") or "")

    return {
        "as_of": str(pkt.get("as_of", "")),
        "overall_verdict": str(pkt.get("overall_verdict", "insufficient")),
        "confidence": str(pkt.get("confidence", "low")),
        "working_context": str(bundle.get("working_context", "")),
        "superseded_assumptions": superseded_assumptions,
        "unresolved_conflicts": unresolved_conflicts,
        "citations": citations,
        "warnings": warnings,
        "claims": claims,
        "sources_considered": sources_considered,
        "sources_rejected": sources_rejected,
        "packet_id": packet_id,
        "packet_version": str(pkt.get("packet_version", "")),
        "assist_used": bool(pkt.get("assist_used", False)),
    }


# ---------------------------------------------------------------------------
# Pipeline runner — builds a FRESH workflow per call
# ---------------------------------------------------------------------------


def _run_pipeline(
    *,
    raw_documents: list[dict[str, Any]],
    claims: list[str],
    query: str,
    as_of: str,
    trust_policy: dict[str, Any] | None,
    noise_phrases: list[str] | None,
    today: date | None,
) -> dict[str, Any]:
    """Build a fresh workflow, run it once, and assemble the response.

    Reads only ``final_state['working_context_bundle']`` /
    ``['evidence_packet']`` and the ``len()`` of ``['sources']`` /
    ``['rejected']``. Every failure — workflow construction, ``run()``
    raising, or the workflow completing with a failed step — is converted
    into the structured error shape; no raw content, exception message, or
    traceback ever crosses the wire unsanitized (T6).

    Args:
        raw_documents: Gate-ready document dicts (still UNTRUSTED content).
        claims: Validated, non-empty list of claim strings.
        query: Validated query string.
        as_of: Validated or machine-stamped ISO date string.
        trust_policy: Constructor-only trust policy config (EE-5).
        noise_phrases: Constructor-only noise-phrase config.
        today: Constructor-only clock override (for deterministic tests).

    Returns:
        The ``EvidenceResponse`` dict, or a structured
        ``{"error": {...}}`` dict on any failure.
    """
    try:
        workflow = build_reference_workflow(
            trust_policy=trust_policy,
            noise_phrases=noise_phrases,
            today=today,
        )
        result = workflow.run(
            {
                "raw_documents": raw_documents,
                "claims": claims,
                "query": query,
                "as_of": as_of,
            }
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: sanitize everything
        error_type, message = sanitize_exception(exc)
        return _error_response(error_type, message)

    if result.status != WorkflowStatus.COMPLETE:
        error_type = "PipelineExecutionError"
        error_message = "the evidence pipeline did not complete successfully"
        for step_result in result.step_results.values():
            if step_result.status == StepStatus.FAILED_FINAL and step_result.attempts:
                last_attempt = step_result.attempts[-1]
                if last_attempt.error_type:
                    error_type = last_attempt.error_type
                    error_message = last_attempt.error_message or error_message
                break
        return _error_response(error_type, error_message)

    final_state = result.final_state
    bundle_obj = final_state.get("working_context_bundle")
    packet_obj = final_state.get("evidence_packet")
    sources_obj = final_state.get("sources")
    rejected_obj = final_state.get("rejected")

    bundle = bundle_obj if isinstance(bundle_obj, dict) else {}
    packet = packet_obj if isinstance(packet_obj, dict) else {}
    sources_considered = len(sources_obj) if isinstance(sources_obj, list) else 0
    sources_rejected = len(rejected_obj) if isinstance(rejected_obj, list) else 0

    return build_evidence_response(
        bundle,
        packet,
        sources_considered=sources_considered,
        sources_rejected=sources_rejected,
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def evaluate_evidence_impl(
    documents: object,
    claims: object,
    query: object,
    as_of: object = None,
    *,
    trust_policy: dict[str, Any] | None = None,
    noise_phrases: list[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Retrieval-agnostic tool: gate and evaluate caller-supplied documents.

    Works with no retriever configured — this is the default that ships to
    everyone (the plugin's retrieval-agnostic posture). Validates all inputs
    at the boundary before any pipeline work runs (Decision 4).

    Args:
        documents: Caller-supplied document dicts (untrusted; validated then
            passed through the content_gate — the sole sanitizer).
        claims: Caller-supplied claim strings to evaluate against the
            documents.
        query: The caller's question (used by the evaluator's temporal/value
            extraction, not model-facing beyond the deterministic response).
        as_of: Optional ISO date the documents reflect. Strictly validated;
            omitted defaults to a machine stamp (documents may be historical,
            so this is NOT forced to "today" the way ``verified_answer`` is).
        trust_policy: Constructor-only config — never a tool argument on the
            wire (EE-5); threaded through from ``create_server``.
        noise_phrases: Constructor-only config, as above.
        today: Constructor-only clock override for deterministic tests. When
            provided, it is used to derive the default ``as_of``/``fetched_at``
            stamps too (instead of the real clock) so tests are fully
            deterministic; production callers (``today=None``) are unaffected.

    Returns:
        The ``EvidenceResponse`` dict, or a structured error dict.
    """
    start = time.perf_counter()
    document_count = len(documents) if isinstance(documents, list) else 0
    claim_count = len(claims) if isinstance(claims, list) else 0
    query_length = len(query) if isinstance(query, str) else 0

    try:
        validated_documents = validate_documents(documents)
        validated_claims = validate_claims(claims)
        validated_query = validate_query(query)
        validated_as_of = validate_as_of(as_of)
        # SEV-001 Advisory A1: defense-in-depth total-size cap, checked before
        # any fetched_at stamping or pipeline construction.
        validate_total_size(validated_documents)
    except InputLimitError as exc:
        response = _error_response("InputLimitError", str(exc))
    else:
        # Full ISO-8601 UTC datetime (unified with normalize_retrieved_documents'
        # fetched_at shape — Low #2); respects the injected `today` clock (Low #3).
        fetched_at_stamp = stamp_now(today=today)
        stamped_documents: list[dict[str, Any]] = []
        for doc in validated_documents:
            fetched_at = doc.get("fetched_at")
            if isinstance(fetched_at, str) and fetched_at:
                stamped_documents.append(doc)
            else:
                # DN-5: the gate rejects documents missing fetched_at. Caller-supplied
                # docs may omit it; stamp ingest time so honest docs are not
                # spuriously rejected. Adds a machine field only — never alters
                # caller content.
                stamped_documents.append({**doc, "fetched_at": fetched_at_stamp})

        resolved_as_of = (
            validated_as_of if validated_as_of is not None else stamp_today(today=today)
        )

        response = _run_pipeline(
            raw_documents=stamped_documents,
            claims=validated_claims,
            query=validated_query,
            as_of=resolved_as_of,
            trust_policy=trust_policy,
            noise_phrases=noise_phrases,
            today=today,
        )
        document_count = len(stamped_documents)
        claim_count = len(validated_claims)
        query_length = len(validated_query)

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _log_tool_call(
        "evaluate_evidence",
        document_count=document_count,
        claim_count=claim_count,
        query_length=query_length,
        elapsed_ms=elapsed_ms,
        response=response,
    )
    return response


def verified_answer_impl(
    query: object,
    retriever: Retriever | None,
    *,
    claims: object = None,
    max_results: object = None,
    trust_policy: dict[str, Any] | None = None,
    noise_phrases: list[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """The stronger firewall: retrieval + gate + evaluate in one call.

    Retrieval happens server-side, behind the gate — the calling model
    cannot substitute its own (possibly hostile or stale) documents; it must
    consume the gated ``working_context``. ``as_of`` is ALWAYS machine-stamped
    on every call (Case 3: never cached, never user-typed).

    Args:
        query: The caller's question.
        retriever: The configured ``Retriever`` callable, or ``None`` if the
            server was started without one.
        claims: Optional caller-supplied claim strings. Defaults to
            ``[query]`` when omitted (deterministic; no LLM decomposition —
            richer claim decomposition is a future D1 feature).
        max_results: Optional result-count hint, clamped to
            ``[1, MAX_RESULTS_CAP]``.
        trust_policy: Constructor-only config (EE-5) — never a tool argument.
        noise_phrases: Constructor-only config, as above.
        today: Constructor-only clock override for deterministic tests. When
            provided, ``as_of`` is derived from it instead of the real clock
            (still re-derived on every call — Case 3 — just deterministically
            in tests); production callers (``today=None``) are unaffected.

    Returns:
        The ``EvidenceResponse`` dict, or a structured error dict
        (including ``{"error": {"type": "RetrieverNotConfigured", ...}}``
        when no retriever is configured).
    """
    start = time.perf_counter()
    query_length = len(query) if isinstance(query, str) else 0
    claim_count = 0
    document_count = 0

    validated_query = ""
    validated_claims: list[str] = []
    raw_documents: list[dict[str, Any]] = []
    as_of_stamp = ""
    response: dict[str, Any] | None = None

    try:
        validated_query = validate_query(query)
        query_length = len(validated_query)
    except InputLimitError as exc:
        response = _error_response("InputLimitError", str(exc))

    if response is None:
        try:
            validated_claims = [validated_query] if claims is None else validate_claims(claims)
            claim_count = len(validated_claims)
        except InputLimitError as exc:
            response = _error_response("InputLimitError", str(exc))

    if response is None:
        try:
            if retriever is None:
                raise RetrieverNotConfiguredError(
                    "No retriever is configured for this server. Use evaluate_evidence "
                    "with caller-supplied documents, or launch the server with "
                    "create_server(retriever=...)."
                )

            clamped_max_results = clamp_max_results(max_results)
            # Case 3: re-stamped on every call, never cached, never trusted from the wire.
            # `today` (test-only) lets this be deterministic without ever caching a stamp.
            as_of_stamp = stamp_today(today=today)

            raw_payload = retriever(validated_query, max_results=clamped_max_results)
            # SEV-001: normalize_retrieved_documents caps the accepted document count
            # at MAX_DOCUMENTS by default — an untrusted retriever cannot flood the
            # content gate or the pipeline's state store.
            raw_documents = normalize_retrieved_documents(raw_payload, today=today)
            # SEV-001 Advisory A1: defense-in-depth total-size cap, checked before
            # the pipeline runs.
            validate_total_size(raw_documents)
            document_count = len(raw_documents)
        except RetrieverNotConfiguredError as exc:
            response = _error_response("RetrieverNotConfigured", str(exc))
        except InputLimitError as exc:
            response = _error_response("InputLimitError", str(exc))
        except Exception as exc:  # noqa: BLE001 - retriever is an arbitrary third-party callable
            error_type, message = sanitize_exception(exc)
            response = _error_response(error_type, message)

    if response is None:
        response = _run_pipeline(
            raw_documents=raw_documents,
            claims=validated_claims,
            query=validated_query,
            as_of=as_of_stamp,
            trust_policy=trust_policy,
            noise_phrases=noise_phrases,
            today=today,
        )

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _log_tool_call(
        "verified_answer",
        document_count=document_count,
        claim_count=claim_count,
        query_length=query_length,
        elapsed_ms=elapsed_ms,
        response=response,
    )
    return response
