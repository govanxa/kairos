"""Kairos-plugin-evidence claim_extractor — deterministic claim structuring (C3 slice 1).

V1 deterministic pass-through: assigns claim_ids (C1..Cn), infers claim_kind /
time_sensitivity from cheap heuristics. No LLM (Phase D1 adds decomposition).

Key divergence from the A1 spike (MUST-fix #3):
``_infer_claim_kind`` checks the score pattern → ``event_outcome`` FIRST, before
the date / digit heuristics.  This prevents score-pair claims (e.g. "Team A beat
Team B N-M at the tournament") from being misclassified as ``numeric`` and having
bare digits extracted as claim values instead of the atomic score pair.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from kairos.exceptions import ValidationError
from kairos.plugins.registry import step_plugin

from kairos_ai_evidence.contracts import EXTRACTOR_OUTPUT, make_claim_record

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Heuristic patterns (pre-compiled at import — T9, bounded quantifiers, no nesting)
# ---------------------------------------------------------------------------

# Score pattern: "3-2", "3 – 2", "10—0" etc.
# `(?<![-\d])`: excludes digits preceded by a hyphen (ISO date components like
# "06" in "2025-06-15") while still matching scores preceded by spaces or
# punctuation.  `(?!\d)`: prevents matching inside longer digit runs.
_SCORE_RE: re.Pattern[str] = re.compile(r"(?<![-\d])\d{1,3}\s*[-–—]\s*\d{1,3}(?!\d)")

# Date-token pattern: month names, ISO dates, quarter/half labels.
# Used to detect temporal claims in _infer_claim_kind.
_DATE_TOKENS_RE: re.Pattern[str] = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"q[1-4]\s+\d{4}|h[12]\s+\d{4})\b",
    re.IGNORECASE,
)

# Any digit — final heuristic step after score and date checks.
_NUMBER_RE: re.Pattern[str] = re.compile(r"\d")

# Score-cue word ("score"/"scoreline"/"scoreboard"/"scores") — Case 4.
# Bounded alternation, pre-compiled at import (T9: no ReDoS risk).
_SCORE_CUE_RE: re.Pattern[str] = re.compile(r"\bscore(?:line|board|s)?\b", re.IGNORECASE)

# Matchup token ("vs"/"vs."/"versus"/"against") — Case 4.
# Bounded alternation, pre-compiled at import (T9: no ReDoS risk).
_MATCHUP_RE: re.Pattern[str] = re.compile(r"\b(?:vs\.?|versus|against)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Heuristic claim-kind classifier (pure)
# ---------------------------------------------------------------------------


def _infer_claim_kind(claim_text: str) -> str:
    """Infer claim_kind from claim text using strictly ordered heuristics.

    Order (MUST-fix #3 vs the A1 spike — score checked FIRST):

    1. Score pattern present (e.g. "3-2", "10–0") → ``event_outcome``
    1b. Score-cue word ("score") AND matchup token ("vs") → ``event_outcome``
        (Case 4). A question like "What was the score of X vs Y?" carries a
        typed SCORE target even though the claim text holds no score itself;
        classifying it ``event_outcome`` routes ``extract_values`` to
        ``_SCORE_RE`` against the sources. Requiring BOTH cue and matchup
        avoids reclassifying non-sport uses ("credit score reached 720"
        stays ``numeric``).
    2. Date token present (month name / ISO date / quarter) → ``temporal``
    3. Any digit present → ``numeric``
    4. Otherwise → ``other``

    Args:
        claim_text: The raw claim string.

    Returns:
        One of ``"event_outcome"``, ``"temporal"``, ``"numeric"``, ``"other"``.
    """
    if _SCORE_RE.search(claim_text):
        return "event_outcome"
    if _SCORE_CUE_RE.search(claim_text) and _MATCHUP_RE.search(claim_text):
        return "event_outcome"
    if _DATE_TOKENS_RE.search(claim_text):
        return "temporal"
    if _NUMBER_RE.search(claim_text):
        return "numeric"
    return "other"


# ---------------------------------------------------------------------------
# Core extraction logic (pure)
# ---------------------------------------------------------------------------


# Maximum claim text length stored in a ClaimRecord (SEV-002).
# Truncation at ingest prevents the O(W²·L) n-gram search in extract_values
# from becoming a denial-of-service vector via adversarially long claims.
_MAX_CLAIM_TEXT_LEN: int = 2000


def extract_claims(claims: list[str]) -> list[dict[str, Any]]:
    """Convert raw claim strings into ClaimRecord skeletons.

    Assigns sequential C1..Cn IDs, infers ``claim_kind`` and sets
    ``time_sensitivity`` to the conservative default ``"volatile"``.  All
    evidence fields (``supporting_source_ids``, ``verdict``, etc.) are left at
    safe defaults for the evaluator to populate.

    Claim strings longer than ``_MAX_CLAIM_TEXT_LEN`` (2000 chars) are
    **silently truncated** before kind inference and storage (SEV-002).

    Args:
        claims: Non-empty list of claim strings from the caller.  Non-string
            items are silently skipped; whitespace-only strings are discarded.

    Returns:
        List of ClaimRecord dicts (JSON-native) matching ``CLAIM_RECORD`` schema.

    Raises:
        ValidationError: If no non-whitespace claim strings remain after
            filtering.
    """
    filtered: list[str] = [c for c in claims if isinstance(c, str) and c.strip()]
    if not filtered:
        raise ValidationError(
            "claims list must not be empty — at least one non-whitespace claim is required"
        )
    records: list[dict[str, Any]] = []
    for i, claim_text in enumerate(filtered, start=1):
        claim_text = claim_text[:_MAX_CLAIM_TEXT_LEN]  # SEV-002: cap before n-gram search
        kind = _infer_claim_kind(claim_text)
        record = make_claim_record(
            claim_id=f"C{i}",
            claim_text=claim_text,
            claim_kind=kind,
            # "volatile" is the conservative default — it demands fresher sources
            # and upgrades automatically if the caller specifies otherwise (02 §3.2).
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
# Step action (thin @step_plugin adapter over extract_claims)
# ---------------------------------------------------------------------------


@step_plugin(
    name="claim_extractor",
    description="Structure caller-supplied claims into ClaimRecord skeletons.",
    output_contract=EXTRACTOR_OUTPUT,
    # input_contract intentionally omitted (DN-1): claim_extractor is an
    # entry-ish step that reads 'claims' from scoped state.  ctx.inputs == {}
    # at runtime, so a fixed EXTRACTOR_INPUT schema keyed by dependency-step
    # names cannot match — wiring it would fail every run.
)
def claim_extractor(ctx: StepContext) -> dict[str, Any]:
    """Claim extractor step action.

    Reads ``claims`` from scoped state (read_keys wall enforced by the executor).
    Requires a ``list`` value; raises ``ValidationError`` when the state key is
    absent or not a list.  Non-string items inside the list are coerced to str;
    ``None`` items are dropped; whitespace-only items are discarded by
    ``extract_claims``.  Writes ``claim_records`` and returns
    ``{"claim_records": [...]}`` for executor ``output_contract`` validation
    (``EXTRACTOR_OUTPUT``).

    Args:
        ctx: StepContext with a scoped state proxy configured with
            ``read_keys=["claims"]`` and ``write_keys=["claim_records"]``.

    Returns:
        ``{"claim_records": [...]}``.

    Raises:
        ValidationError: If ``claims`` is not a list, or if no valid (non-
            whitespace) claim strings remain after filtering.
    """
    claims_obj: Any = ctx.state.get("claims")
    if not isinstance(claims_obj, list):
        raise ValidationError(
            f"'claims' state key must be a list of strings, "
            f"got {type(claims_obj).__name__ if claims_obj is not None else 'None'}"
        )

    # Coerce non-str truthy items to str; drop None.
    claims: list[str] = []
    for item in claims_obj:
        if isinstance(item, str):
            claims.append(item)
        elif item is not None:
            claims.append(str(item))

    claim_records = extract_claims(claims)

    ctx.state.set("claim_records", claim_records)
    return {"claim_records": claim_records}
