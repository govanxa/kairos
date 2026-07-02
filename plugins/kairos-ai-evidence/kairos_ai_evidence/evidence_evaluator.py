"""Kairos-plugin-evidence evidence_evaluator — corroboration engine (C3).

Slice 2 (complete module): adds TrustPolicy, classify_tier, classify_freshness,
assign_independence_groups, detect_conflicts, compose_warnings, resolve_as_of,
make_evidence_evaluator factory, and the @step_plugin-decorated default-policy
evidence_evaluator step action.

All six MUST-fixes from the blueprint are implemented in the extraction core:
#1 Score-pair atomic ("3-2" one value, never bare "3"; dash variants accepted).
#2 Bare-year masking ("2026" masked → never extracted as a numeric value).
#3 Title+excerpt assembled — scores/values in titles are found.
#4 No-value ≠ conflict (zero survivors → [], never a fabricated value).
#5 Adjacency filter — values must have a claim anchor within ±60 chars.
#6 Noise masking — relative timestamps, round labels, bare years masked.

Security:
- EE-5: TrustPolicy built only at make_evidence_evaluator() time; no policy
  ever read from ctx.state.
- T6: unexpected failures wrapped in sanitized ExecutionError from None.
- T9: all patterns pre-compiled, bounded quantifiers, no nested ambiguity.
- Determinism: no LLM, no network, no randomness. One documented clock dep
  (resolve_as_of fallback, injectable for tests via `today`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from kairos.exceptions import ConfigError, ExecutionError
from kairos.plugins.registry import step_plugin
from kairos.security import sanitize_exception

from kairos_ai_evidence.content_gate import registrable_domain
from kairos_ai_evidence.contracts import (
    EVALUATOR_OUTPUT,
    Freshness,
    ProvenanceTier,
    derive_confidence,
    derive_overall_verdict,
    derive_support_level,
    derive_verdict,
    make_packet,
)

if TYPE_CHECKING:
    from kairos.step import StepContext

# ---------------------------------------------------------------------------
# Pattern constants — ALL pre-compiled at import time (T9).
# Bounded quantifiers, constant-width lookarounds, no nested ambiguous
# repetition. Input text is gate-sanitized and length-bounded
# (title ≤200 + excerpt ≤2000) → total ≤ ~2200 chars per source (linear-time).
# ---------------------------------------------------------------------------

# Score pairs: "3-2", "3 – 2", "3—2", "10–0".
# `(?<![-\d])`: 1-char constant-width lookbehind — excludes digits that are
# preceded by a hyphen (ISO date components like "06" in "2025-06-15"), while
# still matching standalone scores preceded by spaces or punctuation.
# `(?!\d)`: 1-char constant-width lookahead prevents matching inside longer runs.
_SCORE_RE: re.Pattern[str] = re.compile(r"(?<![-\d])\d{1,3}\s*[-–—]\s*\d{1,3}(?!\d)")

# Numeric values: integers ≤999, thousands-separated, decimals, percentages.
# `(?<![a-zA-Z\d.,])`: 1-char lookbehind — excludes digits inside alphanumeric
# tokens ("CO2": "O" precedes "2" → no match; "H2O": "H" precedes "2" → no match).
# `\d{1,3}`: bounded leading digits. `(?:,\d{3})*`: each group is exactly 4 chars.
_NUMBER_RE: re.Pattern[str] = re.compile(r"(?<![a-zA-Z\d.,])\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?%?")

# Date spans: named-month dates ("Jul 1, 2026", "1 July 2026"), ISO dates.
# Month abbreviation + optional extension (0–6 extra chars, bounded).
# Optional day-of-month and year; all quantifiers bounded.
_DATE_SPAN_RE: re.Pattern[str] = re.compile(
    r"\b(?:"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6}\.?\s+\d{1,2}(?:,?\s+\d{4})?"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6}\.?(?:,?\s+\d{4})?"
    r"|\d{4}-\d{1,2}-\d{1,2}"
    r")\b",
    re.IGNORECASE,
)

# Named constant for the bare-year noise pattern.  Referenced by identity in
# _masked_spans to skip it for temporal claims (L1: named constant prevents
# silent breakage if _DEFAULT_NOISE_RE is ever reordered).
_BARE_YEAR_RE: re.Pattern[str] = re.compile(r"\b(?:19|20)\d{2}\b")

# Noise patterns (tuple — each applied separately; all quantifiers bounded).
_DEFAULT_NOISE_RE: tuple[re.Pattern[str], ...] = (
    # Relative timestamps: "6 hours ago", "2 days ago", "3 weeks ago"
    re.compile(r"\b\d{1,3}\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b", re.I),
    # "Published …" trailers (up to 40 non-newline chars; bounded)
    re.compile(r"\bpublished\b[^.\n]{0,40}", re.I),
    # Round labels: "last 16", "last-16", "round of 32" (MUST-fix #6)
    re.compile(r"\b(?:last[\s-]\d{1,3}|round\s+of\s+\d{1,3})\b", re.I),
    # Bare 4-digit years: "2026", "1995" (MUST-fix #2, #6).
    # Use _BARE_YEAR_RE so _masked_spans can skip it by identity for temporal
    # claims — reordering this tuple cannot silently break temporal extraction.
    _BARE_YEAR_RE,
)

# N-gram search limits for entity_fact / other claims (SEV-002).
# Caps prevent O(W²·L) degenerate performance on adversarially long claim text.
_MAX_NGRAM_TOKENS: int = 12  # maximum n-gram length (tokens)
_MAX_CLAIM_WORDS: int = 100  # maximum claim words scanned in n-gram search

# Stopwords used by _significant_tokens to filter adjacency anchors.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # Articles / prepositions / conjunctions
        "the",
        "and",
        "for",
        "from",
        "but",
        "not",
        "with",
        "into",
        "over",
        "after",
        "also",
        "each",
        "about",
        "more",
        "some",
        "there",
        "then",
        "only",
        "very",
        "just",
        "even",
        "most",
        "both",
        "all",
        "any",
        # Common verbs (short / high-frequency)
        "was",
        "were",
        "are",
        "has",
        "had",
        "have",
        "been",
        "can",
        "did",
        "does",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        # Pronouns
        "that",
        "this",
        "they",
        "them",
        "their",
        "what",
        "who",
        "how",
        "when",
        "where",
        "which",
        "than",
        "his",
        "her",
        "its",
        "our",
        # Very short filler words (len<3 handled separately, but belt+braces)
        "at",
        "by",
        "in",
        "of",
        "to",
        "is",
        "it",
        "as",
        "an",
        "or",
        "be",
        "do",
        "if",
        "so",
        "up",
        "on",
        "no",
    }
)

# Adjacency window: characters around a candidate span searched for anchors.
_ADJACENCY_WINDOW: int = 60

# Title/excerpt separator sentinel (U+241E INFORMATION SEPARATOR TWO).
# A value cannot straddle this boundary during string slicing.
_SEP: str = " ␞ "

# Pre-compiled pure-numeric detector (module-level for efficiency in tight loops).
_PURE_NUMERIC_RE: re.Pattern[str] = re.compile(r"^\d+$")

# Freshness day-delta thresholds per time_sensitivity (current_days, recent_days).
# Source is current if delta ≤ current_days, recent if ≤ recent_days, else stale.
_FRESHNESS_THRESHOLDS: dict[str, tuple[int, int]] = {
    "volatile": (1, 7),  # sports scores, market prices — very tight windows
    "slow_changing": (7, 90),  # statistics, surveys — moderate windows
    "static": (90, 365),  # historical facts, constants — wide windows
}

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _significant_tokens(claim_text: str) -> list[str]:
    """Extract adjacency anchors from a claim string.

    Anchors are the claim's content words — lowercased tokens of length ≥ 3
    that are not stopwords, not pure-numeric, and not date or score tokens
    (MUST-fix #5).  These are used both for the relevance bail-out (step 2)
    and for the adjacency check (step 5) in ``extract_values``.

    Args:
        claim_text: The claim string.

    Returns:
        List of lowercase significant token strings (may be empty).
    """
    result: list[str] = []
    for raw in claim_text.lower().split():
        # Strip common edge-punctuation so "world." → "world" (keeps "guido's" intact).
        t = raw.strip(".,;:!?\"'()''")  # noqa: B005 — intended: strip any of these edge chars
        if len(t) < 3:
            continue
        if t in _STOPWORDS:
            continue
        if _PURE_NUMERIC_RE.match(t):
            continue
        if _SCORE_RE.match(t):  # score token like "3-2" → not an anchor
            continue
        result.append(t)
    return result


def _masked_spans(
    text: str,
    extra_noise: tuple[str, ...],
    *,
    skip_date_spans: bool = False,
) -> list[tuple[int, int]]:
    """Compute character spans that must be excluded from candidate extraction.

    Covers: ``_DATE_SPAN_RE`` matches (unless ``skip_date_spans``), each
    ``_DEFAULT_NOISE_RE`` match, and each literal ``extra_noise`` substring
    occurrence in *text* (MUST-fixes #2, #6).  Candidates whose spans overlap
    any masked span are discarded in step 5 of ``extract_values``.

    ``skip_date_spans`` is ``True`` for temporal claims where the date IS the
    target value — applying date masking there would eliminate valid candidates.

    ``extra_noise`` items are plain substrings, never compiled regexes, so no
    user-supplied pattern is ever compiled (ReDoS-safe, T9).

    Args:
        text: Assembled title + separator + excerpt text.
        extra_noise: Literal substrings to mask beyond the built-in list.
        skip_date_spans: When True, ``_DATE_SPAN_RE`` is NOT applied.

    Returns:
        List of ``(start, end)`` int pairs covering masked character spans.
    """
    spans: list[tuple[int, int]] = []

    if not skip_date_spans:
        for m in _DATE_SPAN_RE.finditer(text):
            spans.append((m.start(), m.end()))

    for noise_re in _DEFAULT_NOISE_RE:
        # For temporal claims (skip_date_spans=True) also skip the bare-year
        # pattern (_BARE_YEAR_RE).  A bare year like "2025" appears inside valid
        # date candidates ("March 15, 2025") and must not block them.
        # Identity check on _BARE_YEAR_RE (not a positional index) so reordering
        # _DEFAULT_NOISE_RE can never silently break temporal extraction (L1).
        if skip_date_spans and noise_re is _BARE_YEAR_RE:
            continue
        for m in noise_re.finditer(text):
            spans.append((m.start(), m.end()))

    # Literal extra_noise: str.find is linear, no regex compiled (T9).
    lower_text = text.lower()
    for phrase in extra_noise:
        if not phrase:
            continue
        phrase_lower = phrase.lower()
        start = 0
        while True:
            idx = lower_text.find(phrase_lower, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(phrase_lower)))
            start = idx + 1

    return spans


def _overlaps_masked(cand_start: int, cand_end: int, masked: list[tuple[int, int]]) -> bool:
    """Return True if [cand_start, cand_end) overlaps any masked span."""
    return any(s < cand_end and e > cand_start for s, e in masked)


# ---------------------------------------------------------------------------
# Public pure functions — extraction core
# ---------------------------------------------------------------------------


def normalize_value(v: str) -> str:
    """Normalize an extracted value for comparison and storage.

    Folds en-dash (–, U+2013) and em-dash (—, U+2014) to hyphen-minus (-),
    collapses whitespace around hyphens, normalizes internal whitespace, strips
    leading/trailing whitespace, and caps the result at 40 characters.

    "3 - 2" and "3-2" and "3–2" all normalize to the same string.

    Args:
        v: Raw extracted value string.

    Returns:
        Normalized string, max 40 characters.
    """
    v = v.replace("–", "-").replace("—", "-")
    v = re.sub(r"\s*-\s*", "-", v)
    v = " ".join(v.split())
    v = v.strip()
    return v[:40]


def extract_values(
    claim: dict[str, Any],
    source: dict[str, Any],
    *,
    extra_noise: tuple[str, ...] = (),
) -> list[str]:
    """Extract the claim's supporting value from a sanitized source record.

    Implements the 6-step value-extraction algorithm with all six MUST-fixes.
    Total function — returns ``[]`` on any malformed/missing input, never raises.
    Returns 0 or 1 items.

    Algorithm:

    1. **Assemble text** (MUST-fix #3): ``title + _SEP + excerpt`` — title first
       (highest factoid density); separator sentinel prevents value from
       straddling the boundary.  Bail to ``[]`` if blank.

    2. **Relevance + anchors** (MUST-fix #5): compute
       ``anchors = _significant_tokens(claim_text)``.  If no anchor substring
       appears in lowercased text → unrelated source → ``[]``.

    3. **Mask noise / date regions** (MUST-fixes #2, #6): ``_masked_spans()``
       covering ``_DATE_SPAN_RE``, ``_DEFAULT_NOISE_RE``, and ``extra_noise``
       literals.  Exception for temporal claims: ``_DATE_SPAN_RE`` is NOT
       applied (the date IS the target value).

    4. **Kind-driven candidate search**:
       - ``event_outcome`` → ``_SCORE_RE`` only (atomic pairs — MUST-fix #1).
       - ``numeric`` → ``_NUMBER_RE``.
       - ``temporal`` → ``_DATE_SPAN_RE``.
       - ``entity_fact`` / ``other`` → longest anchor n-gram in text.

    5. **Filter**: discard candidates overlapping masked spans; require at
       least one anchor within ±``_ADJACENCY_WINDOW`` (60 chars) of the
       candidate (MUST-fix #5 — kills stray noise numbers).

    6. **Return** the first surviving candidate ``normalize_value``-d and
       capped at 40 chars, as a one-item list.  Zero survivors → ``[]``
       (MUST-fix #4 — absence of value never manufactures a conflict).

    Args:
        claim: ClaimRecord dict with ``claim_text`` and ``claim_kind``.
        source: SourceRecord dict with optional ``title`` and ``excerpt``.
        extra_noise: Literal substrings to mask beyond the built-in list
            (MUST-fix #6, configurable via ``make_evidence_evaluator``).

    Returns:
        ``[normalized_value]`` (one item) or ``[]``.

    Note:
        Bare 4-digit values in the range 1900–2099 (e.g. years) are masked by
        ``_BARE_YEAR_RE`` and are therefore **not extractable** for
        ``numeric`` claims.  This is an accepted tradeoff of MUST-fix #2 (date-
        token exclusion prevents year-as-date false conflicts); callers needing
        year extraction should use ``temporal`` claim_kind instead.
    """
    # --- Step 1: Assemble search text (MUST-fix #3) ---
    title: str = source.get("title") or ""
    excerpt: str = source.get("excerpt") or ""
    if not title and not excerpt:
        return []

    text: str = (title + _SEP + excerpt) if title else excerpt
    lower: str = text.lower()

    claim_text: str = str(claim.get("claim_text") or "")
    claim_kind: str = str(claim.get("claim_kind") or "other")

    # --- Step 2: Relevance bail-out + anchor set ---
    anchors: list[str] = _significant_tokens(claim_text)
    if not anchors:
        return []
    if not any(anchor in lower for anchor in anchors):
        return []

    # --- Step 3: Mask noise / date regions ---
    masked = _masked_spans(
        text,
        extra_noise,
        skip_date_spans=(claim_kind == "temporal"),
    )

    # --- Step 4: Kind-driven candidate search ---
    candidates: list[tuple[int, int]] = []

    match claim_kind:
        case "event_outcome":
            for m in _SCORE_RE.finditer(text):
                candidates.append((m.start(), m.end()))

        case "numeric":
            for m in _NUMBER_RE.finditer(text):
                candidates.append((m.start(), m.end()))

        case "temporal":
            for m in _DATE_SPAN_RE.finditer(text):
                candidates.append((m.start(), m.end()))

        case "entity_fact" | "other":
            # SEV-002: cap to _MAX_CLAIM_WORDS tokens and _MAX_NGRAM_TOKENS
            # n-gram length to bound complexity to O(W·N·L) instead of O(W²·L).
            words = claim_text.lower().split()[:_MAX_CLAIM_WORDS]
            found_span: tuple[int, int] | None = None
            for n in range(min(len(words), _MAX_NGRAM_TOKENS), 0, -1):
                if found_span is not None:
                    break
                for i in range(len(words) - n + 1):
                    phrase_words = words[i : i + n]
                    phrase = " ".join(phrase_words)
                    cleaned = [w.strip(".,;:!?\"'") for w in phrase_words]
                    has_anchor = any(
                        w not in _STOPWORDS and len(w) >= 3 and not _PURE_NUMERIC_RE.match(w)
                        for w in cleaned
                    )
                    if not has_anchor:
                        continue
                    idx = lower.find(phrase)
                    if idx >= 0:
                        found_span = (idx, idx + len(phrase))
                        break
            if found_span is not None:
                candidates.append(found_span)

        case _:
            return []

    # --- Step 5: Filter — masked overlap + adjacency (MUST-fixes #5, #6) ---
    for cand_start, cand_end in candidates:
        if _overlaps_masked(cand_start, cand_end, masked):
            continue
        win_start = max(0, cand_start - _ADJACENCY_WINDOW)
        win_end = min(len(lower), cand_end + _ADJACENCY_WINDOW)
        context = lower[win_start:win_end]
        if not any(anchor in context for anchor in anchors):
            continue
        # --- Step 6: First survivor, normalized and capped ---
        raw_value = text[cand_start:cand_end]
        normalized = normalize_value(raw_value)
        if normalized:
            return [normalized]

    return []


# ---------------------------------------------------------------------------
# Trust Policy — config-time value object (EE-5: never read from state)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustPolicy:
    """Immutable trust policy controlling source tier promotion and exclusion.

    Built exclusively at ``make_evidence_evaluator()`` construction time.
    No code path reads policy from ``ctx.state`` (EE-5).

    Attributes:
        pin: Domains always promoted to tier ``official``.
        deny: Domains excluded from the active set before extraction.
        tier_overrides: Explicit domain → ProvenanceTier value mappings.
    """

    pin: frozenset[str] = field(default_factory=frozenset)
    deny: frozenset[str] = field(default_factory=frozenset)
    tier_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> TrustPolicy:
        """Validate and build a TrustPolicy from a config dict.

        Accepts canonical keys ``pin``/``deny``/``tier_overrides`` and spike
        aliases ``pins``/``denies``.

        Args:
            cfg: Config dict or None (returns permissive default policy).

        Returns:
            A validated, frozen TrustPolicy.

        Raises:
            ConfigError: When cfg is not dict|None; pin/deny not list-of-str;
                tier_overrides not dict; any tier_overrides value is not a
                ProvenanceTier vocabulary member.
        """
        if cfg is None:
            return cls()
        if not isinstance(cfg, dict):
            raise ConfigError(f"trust_policy must be a dict or None; got {type(cfg).__name__}")

        # 'pin' (canonical) or 'pins' (alias)
        pin_raw = cfg.get("pin", cfg.get("pins"))
        if pin_raw is not None:
            if not isinstance(pin_raw, list) or not all(isinstance(x, str) for x in pin_raw):
                raise ConfigError("trust_policy 'pin'/'pins' must be a list of strings")
            pin: frozenset[str] = frozenset(pin_raw)
        else:
            pin = frozenset()

        # 'deny' (canonical) or 'denies' (alias)
        deny_raw = cfg.get("deny", cfg.get("denies"))
        if deny_raw is not None:
            if not isinstance(deny_raw, list) or not all(isinstance(x, str) for x in deny_raw):
                raise ConfigError("trust_policy 'deny'/'denies' must be a list of strings")
            deny: frozenset[str] = frozenset(deny_raw)
        else:
            deny = frozenset()

        # 'tier_overrides' — validated against ProvenanceTier vocabulary
        tier_overrides_raw = cfg.get("tier_overrides")
        if tier_overrides_raw is not None:
            if not isinstance(tier_overrides_raw, dict):
                raise ConfigError("trust_policy 'tier_overrides' must be a dict")
            valid_tiers = {t.value for t in ProvenanceTier}
            for _domain_key, tier_val in tier_overrides_raw.items():
                if tier_val not in valid_tiers:
                    raise ConfigError(
                        f"tier_overrides value {tier_val!r} is not a valid ProvenanceTier; "
                        f"valid values: {sorted(valid_tiers)}"
                    )
            tier_overrides: dict[str, str] = dict(tier_overrides_raw)
        else:
            tier_overrides = {}

        return cls(pin=pin, deny=deny, tier_overrides=tier_overrides)


# ---------------------------------------------------------------------------
# Classification helpers (pure)
# ---------------------------------------------------------------------------


def _tier_from_tld(domain: str) -> str:
    """Derive provenance tier from TLD heuristic (no tldextract — stdlib only).

    gov/mil/int → official; edu/ac/org → established_media; else → aggregator.

    Args:
        domain: Registrable domain string (e.g. ``"espn.com"``).

    Returns:
        A ProvenanceTier enum value string.
    """
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else domain.lower()
    if tld in {"gov", "mil", "int"}:
        return ProvenanceTier.OFFICIAL.value
    if tld in {"edu", "ac", "org"}:
        return ProvenanceTier.ESTABLISHED_MEDIA.value
    return ProvenanceTier.AGGREGATOR.value


def classify_tier(source: dict[str, Any], policy: TrustPolicy) -> str:
    """Classify a source's provenance tier using the policy + TLD heuristic.

    Resolution order (EE-5: deny overrides pin so a source cannot
    self-promote via a malicious pin + deny combination):
    deny → pin → tier_override → TLD heuristic.

    Args:
        source: SourceRecord dict with a ``domain`` field.
        policy: The active TrustPolicy.

    Returns:
        A ProvenanceTier enum value string.
    """
    domain: str = source.get("domain", "")
    # EE-5: deny beats pin — excluded source keeps its heuristic tier
    if domain in policy.deny:
        return _tier_from_tld(domain)
    if domain in policy.pin:
        return ProvenanceTier.OFFICIAL.value
    if domain in policy.tier_overrides:
        return policy.tier_overrides[domain]
    return _tier_from_tld(domain)


def classify_freshness(
    source: dict[str, Any],
    time_sensitivity: str,
    as_of: str,
) -> str:
    """Classify a source's freshness relative to the as_of date.

    Day-delta = as_of − published_at. Thresholds come from
    ``_FRESHNESS_THRESHOLDS[time_sensitivity]``.  Missing or unparseable
    ``published_at`` → ``undated``.

    Args:
        source: SourceRecord dict with an optional ``published_at`` field.
        time_sensitivity: One of ``"volatile"``, ``"slow_changing"``, ``"static"``.
        as_of: Resolved ISO date string (``YYYY-MM-DD``).

    Returns:
        A Freshness enum value string.
    """
    published_at = source.get("published_at")
    if not published_at or not isinstance(published_at, str):
        return Freshness.UNDATED.value

    try:
        pub_date = date.fromisoformat(published_at[:10])
    except (ValueError, AttributeError):
        return Freshness.UNDATED.value

    try:
        as_of_date = date.fromisoformat(as_of[:10])
    except (ValueError, AttributeError):
        return Freshness.UNDATED.value

    delta = (as_of_date - pub_date).days
    if delta < 0:
        # Future-dated source: treat as current (most conservative honest choice).
        return Freshness.CURRENT.value

    thresholds = _FRESHNESS_THRESHOLDS.get(time_sensitivity, _FRESHNESS_THRESHOLDS["volatile"])
    current_threshold, recent_threshold = thresholds
    if delta <= current_threshold:
        return Freshness.CURRENT.value
    if delta <= recent_threshold:
        return Freshness.RECENT.value
    return Freshness.STALE.value


def assign_independence_groups(sources: list[dict[str, Any]]) -> None:
    """Re-derive independence_group = registrable_domain(url) for each source.

    Overrides the gate placeholder (anti-churnalism; 02 §3.3 step 2).
    Mutates the source dicts in place; callers must pass shallow copies.

    Args:
        sources: List of SourceRecord dicts to update in place.
    """
    for source in sources:
        url: str = source.get("url", "")
        source["independence_group"] = registrable_domain(url)


def detect_conflicts(claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect conflicting extracted values for a single claim.

    Operates ONLY on the claim's ``extracted_values`` field (MUST-fix #4):
    a source with no extracted value is non-supporting, not conflicting.
    Conflict = two or more sources yielded different normalized values.

    Args:
        claim: ClaimRecord dict with ``extracted_values`` populated.

    Returns:
        A list of conflict descriptor dicts (empty if no conflict).
        Each descriptor: ``{"claim_id": str, "description": str, "source_ids": [str]}``.
    """
    extracted: list[Any] = claim.get("extracted_values", [])
    if not extracted:
        return []

    values_by_source: list[tuple[str, str]] = []
    for ev in extracted:
        if isinstance(ev, dict):
            sid = str(ev.get("source_id", ""))
            val = normalize_value(str(ev.get("value", "")))
            if val:
                values_by_source.append((sid, val))

    if not values_by_source:
        return []

    unique_values = {v for _, v in values_by_source}
    if len(unique_values) <= 1:
        return []

    # Conflict: structural description from short normalized values only (T6).
    claim_id: str = str(claim.get("claim_id", ""))
    source_ids: list[str] = sorted({sid for sid, _ in values_by_source})
    values_str = ", ".join(f'"{v}"' for v in sorted(unique_values))

    return [
        {
            "claim_id": claim_id,
            "description": f"Sources report differing values: {values_str}",
            "source_ids": source_ids,
        }
    ]


def compose_warnings(
    sources: list[dict[str, Any]],
    *,
    as_of_stamped: bool,
    as_of: str,
) -> list[str]:
    """Compose structural packet-level warnings.

    Four warning conditions (all structural strings — never raw web content, T6):
    1. All sources share one independence group (churnalism risk).
    2. Any source carries injection flags (confidence capped at low).
    3. No source has a published_at date (freshness is undated).
    4. as_of was machine-stamped (DN-3 caveat).

    Args:
        sources: All SourceRecord dicts in the packet (active + denied, for audit).
        as_of_stamped: True iff ``resolve_as_of`` fell back to the system clock.
        as_of: Resolved ISO date string.

    Returns:
        List of structural warning strings.
    """
    warnings: list[str] = []

    # Warning 1: single independence group (T4)
    if sources:
        groups = {s.get("independence_group", "") for s in sources}
        if len(groups) == 1:
            warnings.append("All sources share one independence group — corroboration is limited.")

    # Warning 2: injection-flagged sources (EE-3 / T1)
    flagged_count = sum(1 for s in sources if s.get("injection_flags"))
    if flagged_count:
        warnings.append(
            f"{flagged_count} source(s) carried injection flags; confidence capped at low."
        )

    # Warning 3: no source has published_at (03 §6 decision 4)
    if sources and all(not s.get("published_at") for s in sources):
        warnings.append("No source carries a publication date; freshness is undated.")

    # Warning 4: machine-stamped as_of (DN-3)
    if as_of_stamped:
        warnings.append(f"as_of not supplied by caller; stamped from system clock ({as_of}).")

    return warnings


def resolve_as_of(
    state_as_of: str | None,
    *,
    today: date | None = None,
) -> tuple[str, bool]:
    """Resolve the as_of date string.

    DN-3 — the single documented clock dependency in C3.

    1. If *state_as_of* is present and matches ``^\\d{4}-\\d{2}-\\d{2}`` →
       use verbatim, ``machine_stamped=False``.
    2. Otherwise → machine-stamp from ``today`` (or ``datetime.now(UTC).date()``),
       ``machine_stamped=True``.

    Args:
        state_as_of: Raw as_of from scoped state (may be None or malformed).
        today: Injectable date for deterministic tests (default: system clock).

    Returns:
        Tuple ``(as_of_str, machine_stamped)``.
    """
    # SEV-001: re.fullmatch ensures NO tail bytes survive (e.g. "2026-07-01<inj>"
    # would pass re.match but fails fullmatch).  date.fromisoformat then rejects
    # calendar-invalid dates like "2026-13-99".  Both must succeed; any failure
    # falls through to machine-stamp so the as_of is always a clean ISO-date.
    if isinstance(state_as_of, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", state_as_of):
        try:
            date.fromisoformat(state_as_of)
            return state_as_of, False
        except ValueError:
            pass  # calendar-invalid date → machine-stamp below
    fallback = today if today is not None else datetime.now(tz=UTC).date()
    return fallback.isoformat(), True


# ---------------------------------------------------------------------------
# Factory — make_evidence_evaluator
# ---------------------------------------------------------------------------

# Time-sensitivity priority (lower int = stricter = more volatile).
_TS_PRIORITY: dict[str, int] = {"volatile": 0, "slow_changing": 1, "static": 2}


def make_evidence_evaluator(
    *,
    trust_policy: dict[str, Any] | None = None,
    noise_phrases: list[str] | None = None,
    today: date | None = None,
) -> Callable[[StepContext], dict[str, Any]]:
    """Build an evidence evaluator closure with a validated trust policy.

    Validates all configuration at construction time (ConfigError on malformed
    input).  The returned closure is safe to call as a Kairos step action.

    Args:
        trust_policy: Config dict for TrustPolicy.from_config (or None for
            the permissive default).  EE-5: never read from ctx.state.
        noise_phrases: Additional literal substrings to mask during extraction
            (MUST-fix #6).  Must be list[str] or None.
        today: Injectable date for deterministic freshness + as_of fallback
            (default: system clock, DN-3).

    Returns:
        A callable ``(ctx: StepContext) -> dict[str, Any]`` that runs the
        full packet-assembly pipeline and writes ``evidence_packet`` to state.

    Raises:
        ConfigError: On malformed ``trust_policy`` or ``noise_phrases``.
    """
    # Validate at construction (EE-5: fail fast, not mid-run).
    policy: TrustPolicy = TrustPolicy.from_config(trust_policy)

    if noise_phrases is not None:
        if not isinstance(noise_phrases, list) or not all(
            isinstance(p, str) for p in noise_phrases
        ):
            raise ConfigError("noise_phrases must be a list of strings or None")
        noise_extra: tuple[str, ...] = tuple(noise_phrases)
    else:
        noise_extra = ()

    def _evaluator(ctx: StepContext) -> dict[str, Any]:
        """Run the full corroboration pipeline and write evidence_packet to state."""
        try:
            # ---- Read scoped state (read_keys wall enforced by executor) ----
            raw_claim_records = ctx.state.get("claim_records")
            raw_sources = ctx.state.get("sources")
            query_raw = ctx.state.get("query")
            state_as_of = ctx.state.get("as_of")

            # Defensive coercion (DN-1: no input_contract wired on the step)
            claim_records: list[dict[str, Any]] = (
                list(raw_claim_records) if isinstance(raw_claim_records, list) else []
            )
            raw_sources_list: list[dict[str, Any]] = (
                list(raw_sources) if isinstance(raw_sources, list) else []
            )
            query: str = str(query_raw) if query_raw is not None else ""

            # ---- Resolve as_of (DN-3) ----
            as_of, machine_stamped = resolve_as_of(
                state_as_of if isinstance(state_as_of, str) else None,
                today=today,
            )

            # ---- Step 1: shallow copy + enrich sources ----
            sources: list[dict[str, Any]] = [dict(s) for s in raw_sources_list]
            assign_independence_groups(sources)

            # Strictest time_sensitivity across claims (volatile is tightest).
            strict_ts: str = min(
                (
                    cr.get("time_sensitivity", "volatile")
                    for cr in claim_records
                    if isinstance(cr, dict)
                ),
                key=lambda ts: _TS_PRIORITY.get(str(ts), 0),
                default="volatile",
            )

            for s in sources:
                s["provenance_tier"] = classify_tier(s, policy)
                s["freshness"] = classify_freshness(s, strict_ts, as_of)

            sources_by_id: dict[str, dict[str, Any]] = {
                s["source_id"]: s for s in sources if "source_id" in s
            }

            # ---- Step 3: active source IDs (exclude denied by domain + group) ----
            active_ids: set[str] = {
                sid
                for sid, s in sources_by_id.items()
                if s.get("domain", "") not in policy.deny
                and s.get("independence_group", "") not in policy.deny
            }

            # ---- Step 4: per-claim enrichment ----
            processed_claims: list[dict[str, Any]] = []
            all_conflicts_list: list[dict[str, Any]] = []

            for cr in claim_records:
                if not isinstance(cr, dict):
                    continue
                claim = dict(cr)

                # Extract values from every active source
                extracted: list[dict[str, str]] = []
                for sid in sorted(active_ids):  # sorted for deterministic order
                    src = sources_by_id.get(sid)
                    if src is None:
                        continue
                    v = extract_values(claim, src, extra_noise=noise_extra)
                    if v:
                        extracted.append({"source_id": sid, "value": v[0]})

                claim["extracted_values"] = extracted

                conflicts = detect_conflicts(claim)
                if conflicts:
                    all_extracted_ids = sorted({ev["source_id"] for ev in extracted})
                    claim["conflicting_source_ids"] = all_extracted_ids
                    claim["supporting_source_ids"] = []
                    all_conflicts_list.extend(conflicts)
                else:
                    claim["supporting_source_ids"] = [ev["source_id"] for ev in extracted]
                    claim["conflicting_source_ids"] = []

                # Groups map for derive_support_level
                groups_map: dict[str, str] = {
                    sid: sources_by_id[sid]["independence_group"]
                    for sid in active_ids
                    if sid in sources_by_id
                }
                supporting = claim["supporting_source_ids"]
                claim["support_level"] = derive_support_level(supporting, groups_map)
                claim["verdict"] = derive_verdict(claim, sources_by_id)

                processed_claims.append(claim)

            # ---- Step 5: packet-level derivation ----
            overall = derive_overall_verdict(processed_claims)
            confidence = derive_confidence(processed_claims, sources_by_id)

            # ---- Step 6: compose warnings ----
            warnings = compose_warnings(sources, as_of_stamped=machine_stamped, as_of=as_of)

            # ---- Step 7: assemble packet via C1 make_packet ----
            packet = make_packet(
                query=query,
                as_of=as_of,
                claims=processed_claims,
                sources=sources,
                overall_verdict=overall,
                confidence=confidence,
                conflicts=all_conflicts_list,
                warnings=warnings,
                assist_used=False,
            )

            # ---- Step 8: write to state + return ----
            ctx.state.set("evidence_packet", packet)
            return packet

        except ExecutionError:
            raise
        except Exception as exc:
            error_type, error_msg = sanitize_exception(exc)
            raise ExecutionError(f"evidence_evaluator failed: {error_type}: {error_msg}") from None

    return _evaluator


# ---------------------------------------------------------------------------
# Default-policy step action (thin adapter over make_evidence_evaluator())
# ---------------------------------------------------------------------------

_default_evaluator: Callable[[StepContext], dict[str, Any]] = make_evidence_evaluator()


@step_plugin(
    name="evidence_evaluator",
    description="Corroborate claims across sanitized sources; derive verdicts.",
    output_contract=EVALUATOR_OUTPUT,
    # input_contract intentionally omitted (DN-1): inputs keyed by dependency
    # step names, not by EVALUATOR_INPUT field names. See blueprint §Architecture.
)
def evidence_evaluator(ctx: StepContext) -> dict[str, Any]:
    """Step action: evaluate claims against sanitized sources and derive verdicts.

    Thin adapter over the default-policy evaluator closure.  For a custom trust
    policy, callers use ``make_evidence_evaluator(trust_policy=...)`` and wire
    the result as a ``Step.action`` directly (see blueprint Dependency Map).

    Reads ``claim_records``, ``sources``, ``query``, and ``as_of`` from scoped
    state (``read_keys`` wall enforced by the executor — EE-1).  Writes
    ``evidence_packet`` to state.

    Args:
        ctx: StepContext with scoped state proxy.

    Returns:
        EvidencePacket dict — validated against EVALUATOR_OUTPUT by the executor.

    Raises:
        ExecutionError: On unexpected internal failure; message is sanitized
            via ``sanitize_exception()``; ``__cause__`` suppressed (T6).
    """
    return _default_evaluator(ctx)
