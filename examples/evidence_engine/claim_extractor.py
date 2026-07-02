"""Evidence Engine claim_extractor — deterministic claim structuring (→ C3).

V1 deterministic pass-through: assigns claim_ids and infers claim_kind /
time_sensitivity from cheap heuristics. No LLM (Phase D1 adds decomposition).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from examples.evidence_engine.contracts import make_claim_record
from kairos.exceptions import ValidationError

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Heuristic patterns (pre-compiled — T9)
# ---------------------------------------------------------------------------

_NUMBER_RE: re.Pattern[str] = re.compile(r"\d")
_DATE_TOKENS_RE: re.Pattern[str] = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"q[1-4]\s+\d{4}|h[12]\s+\d{4})\b",
    re.IGNORECASE,
)


def _infer_claim_kind(claim_text: str) -> str:
    """Heuristic claim_kind from text tokens.

    digit/number present AND looks numeric → 'numeric'.
    date token present → 'temporal'.
    Otherwise → 'other' (conservative default per 02 §3.2).

    Args:
        claim_text: The raw claim string.

    Returns:
        One of 'numeric', 'temporal', 'other'.
    """
    if _DATE_TOKENS_RE.search(claim_text):
        return "temporal"
    if _NUMBER_RE.search(claim_text):
        return "numeric"
    return "other"


# ---------------------------------------------------------------------------
# Core extraction logic (pure)
# ---------------------------------------------------------------------------


def extract_claims(claims: list[str]) -> list[dict[str, Any]]:
    """Convert raw claim strings into ClaimRecord skeletons.

    Assigns C1..Cn, infers claim_kind and time_sensitivity. All evidence
    fields (supporting_source_ids, verdict, etc.) are left at safe defaults
    for the evaluator to populate.

    Args:
        claims: Non-empty list of claim strings supplied by the caller.

    Returns:
        List of ClaimRecord dicts (JSON-native).

    Raises:
        ValidationError: If claims is empty or contains only whitespace-only strings.
    """
    # Filter whitespace-only strings before checking emptiness.
    filtered: list[str] = [c for c in claims if c and c.strip()]
    if not filtered:
        raise ValidationError(
            "claims list must not be empty — at least one non-whitespace claim is required"
        )
    records: list[dict[str, Any]] = []
    for i, claim_text in enumerate(filtered, start=1):
        kind = _infer_claim_kind(claim_text)
        record = make_claim_record(
            claim_id=f"C{i}",
            claim_text=claim_text,
            claim_kind=kind,
            # Default 'volatile' — conservative: demands fresher sources (02 §3.2)
            time_sensitivity="volatile",
            supporting_source_ids=[],
            conflicting_source_ids=[],
            support_level="none",
            verdict="unverifiable",
            extracted_values=[],
            notes="",
        )
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Step action
# ---------------------------------------------------------------------------


def claim_extractor(ctx: StepContext) -> dict[str, Any]:
    """Claim extractor step action.

    Reads 'claims' from state. Writes 'claim_records'. Returns
    {'claim_records': [...]} for EXTRACTOR_OUTPUT validation.

    Args:
        ctx: StepContext with scoped state proxy.

    Returns:
        {'claim_records': [...]}
    """
    claims_obj = ctx.state.get("claims")
    if not isinstance(claims_obj, list):
        raise ValidationError("'claims' state key must be a non-empty list of strings")
    claims: list[str] = [str(c) for c in claims_obj if c]

    claim_records = extract_claims(claims)

    ctx.state.set("claim_records", claim_records)
    return {"claim_records": claim_records}
