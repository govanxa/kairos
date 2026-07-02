"""Evidence Engine — untrusted text primitives (→ B1).

Pure stdlib gate primitives: normalize, neutralize, scrub_credentials,
sanitize_untrusted_text, is_predominantly_instructional.

Security stance: neutralize, not detect. All patterns pre-compiled at import
(T9 — ReDoS). No Kairos imports except for the common exception types.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Canonical flag names — injected into SanitizedText.flags; NEVER contain
# raw matched text (03 §2 injection_flags rule).
# ---------------------------------------------------------------------------

FLAG_ROLE_MARKER = "role_marker"
FLAG_TEMPLATE_TOKEN = "template_token"  # noqa: S105
FLAG_IMPERATIVE = "imperative_override"
FLAG_TOOL_CALL = "tool_call_syntax"

_ALL_FLAGS: frozenset[str] = frozenset(
    {FLAG_ROLE_MARKER, FLAG_TEMPLATE_TOKEN, FLAG_IMPERATIVE, FLAG_TOOL_CALL}
)

# Replacement token — visible, inert, never a valid instruction keyword.
_NEUTRALIZED = "[NEUTRALIZED]"

# ---------------------------------------------------------------------------
# Pre-compiled patterns (module-level — T9, never re-compiled per call)
# ---------------------------------------------------------------------------

# Zero-width and invisible Unicode characters
_ZERO_WIDTH_RE: re.Pattern[str] = re.compile(r"[​‌‍⁠﻿­]")

# Role markers: "system:", "assistant:", "user:", "human:", "ai:" at line start
# or as bracketed tags; case-insensitive.
_ROLE_MARKER_RES: list[re.Pattern[str]] = [
    re.compile(r"^\s*(system|assistant|user|human|ai)\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\[(system|assistant|user|human|ai)\]", re.IGNORECASE),
]

# Chat template tokens — bounded quantifiers (T9 — prevents catastrophic backtracking)
_TEMPLATE_TOKEN_RES: list[re.Pattern[str]] = [
    re.compile(r"<\|im_start\|>|<\|im_end\|>", re.IGNORECASE),
    re.compile(r"<\|[^|\n]{0,30}\|>"),  # bounded: max 30 chars between pipes
    re.compile(r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>", re.IGNORECASE),
    re.compile(r"<s>|</s>", re.IGNORECASE),  # LLaMA sentence boundaries
]

# Imperative injection phrases — common prompt injection patterns.
# Use non-greedy, bounded quantifiers throughout (T9).
_IMPERATIVE_RES: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(?:your|the|all|any)\s+\S{0,30}", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|another)\b", re.IGNORECASE),
    re.compile(r"forget\s+(?:everything|what)\s+you\s+(?:were|have)", re.IGNORECASE),
    re.compile(r"your\s+new\s+(?:instructions?|role|persona|task)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:if|an?\s+\S{1,30}|though)", re.IGNORECASE),
    re.compile(
        r"(?:reveal|print|show|output)\s+(?:your|the)\s+(?:system|instructions?|prompt)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:pretend|imagine)\s+you\s+(?:are|were|have)", re.IGNORECASE),
    re.compile(r"jailbreak|DAN\s+mode|developer\s+mode", re.IGNORECASE),
]

# Tool-call syntax shapes — JSON function-call patterns (bounded)
_TOOL_CALL_RES: list[re.Pattern[str]] = [
    re.compile(r'"function"\s*:\s*\{[^}]{0,200}\}', re.IGNORECASE),
    re.compile(r'"name"\s*:\s*"[^"]{0,60}"\s*,\s*"arguments"\s*:', re.IGNORECASE),
    re.compile(r"<tool_call>|</tool_call>|<tool_result>|</tool_result>", re.IGNORECASE),
    re.compile(r"\btools?\b\s*:\s*\[", re.IGNORECASE),
]

# Credential patterns — value-level redaction over free text (T7).
# Tuples of (compiled pattern, replacement). Ordered: more specific first.
_CREDENTIAL_RES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"key-[a-zA-Z0-9_-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/\-]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"token=[^\s&,;\"']{3,}"), "token=[REDACTED]"),
    (re.compile(r"password=[^\s&,;\"']{3,}"), "password=[REDACTED]"),
    (re.compile(r"api_key=[^\s&,;\"']{3,}"), "api_key=[REDACTED]"),
    (re.compile(r"apikey=[^\s&,;\"']{3,}"), "apikey=[REDACTED]"),
    (re.compile(r"secret=[^\s&,;\"']{3,}"), "secret=[REDACTED]"),
]

# Instruction-critical homoglyph map: Cyrillic/Greek lookalikes → ASCII.
# Applied AFTER NFKC (which handles many standard compatibility mappings) to
# catch residual confusables used to obfuscate keywords.
_HOMOGLYPH_MAP: dict[str, str] = {
    # Cyrillic
    "а": "a",  # а
    "е": "e",  # е
    "о": "o",  # о
    "р": "r",  # р
    "с": "s",  # с
    "у": "u",  # у
    "х": "x",  # х
    "і": "i",  # і (Ukrainian/Bulgarian)
    "А": "A",  # А (Cyrillic capital)
    "Е": "E",  # Е
    "О": "O",  # О
    "Р": "R",  # Р
    "С": "S",  # С
    "Т": "T",  # Т
    "Х": "X",  # Х
    # Greek
    "α": "a",  # α
    "ε": "e",  # ε
    "ο": "o",  # ο (omicron)
    "ν": "v",  # ν (nu — looks like v)
    "τ": "t",  # τ (tau)
    "υ": "u",  # υ (upsilon)
    "Α": "A",  # Α (Greek capital alpha)
    "Ε": "E",  # Ε
    "Ο": "O",  # Ο
}

# Translation table for fast str.translate()
_HOMOGLYPH_TABLE: dict[int, str] = {ord(k): v for k, v in _HOMOGLYPH_MAP.items()}

# Minimum non-neutralized content (chars) to consider a document salvageable.
_MIN_SALVAGEABLE_CHARS = 30
# Neutralization count threshold for predominantly-instructional rejection.
_PREDOMINANTLY_INSTRUCTIONAL_THRESHOLD = 3


# ---------------------------------------------------------------------------
# SanitizedText — immutable output of sanitize_untrusted_text
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SanitizedText:
    """Result of sanitizing an untrusted string.

    Attributes:
        text: The cleaned, neutralized, credential-scrubbed text. Safe to
            store in state (never raw web content).
        flags: Sorted, de-duped canonical flag names that were triggered.
            NEVER contains raw matched text — only names from _ALL_FLAGS.
        truncated: True if the text was capped to max_len.
    """

    text: str
    flags: list[str]
    truncated: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC normalize → strip zero-width chars → homoglyph-fold.

    Deterministic. Applied before pattern matching so obfuscated markers
    are caught by the neutralize step.

    Args:
        text: Raw untrusted string.

    Returns:
        Normalized string safe to apply regex patterns to.
    """
    # NFKC handles many compatibility mappings (fullwidth, ligatures, etc.)
    text = unicodedata.normalize("NFKC", text)
    # Strip zero-width and invisible formatting characters
    text = _ZERO_WIDTH_RE.sub("", text)
    # Fold instruction-critical homoglyphs to ASCII
    text = text.translate(_HOMOGLYPH_TABLE)
    return text


def neutralize(text: str) -> tuple[str, list[str]]:
    """Defang role markers, template tokens, imperatives, and tool-call syntax.

    Applies all pattern groups in order. Matched regions are replaced with
    _NEUTRALIZED. Returns the defanged text and the set of canonical flag
    names that fired. NEVER returns matched raw fragments in the flags list.

    Args:
        text: Pre-normalized untrusted text.

    Returns:
        (defanged_text, sorted_unique_flag_names)
    """
    flags: set[str] = set()

    for pat in _ROLE_MARKER_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_ROLE_MARKER)
            text = new_text

    for pat in _TEMPLATE_TOKEN_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_TEMPLATE_TOKEN)
            text = new_text

    for pat in _IMPERATIVE_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_IMPERATIVE)
            text = new_text

    for pat in _TOOL_CALL_RES:
        new_text = pat.sub(_NEUTRALIZED, text)
        if new_text != text:
            flags.add(FLAG_TOOL_CALL)
            text = new_text

    return text, sorted(flags)


def scrub_credentials(text: str) -> str:
    """Redact credential patterns from free text (T7).

    Applied after neutralization so credential patterns embedded inside
    injection phrases are still caught.

    Args:
        text: Text that may contain API keys, tokens, passwords.

    Returns:
        Text with credential values replaced by [REDACTED_KEY] or similar.
    """
    for pattern, replacement in _CREDENTIAL_RES:
        text = pattern.sub(replacement, text)
    return text


def sanitize_untrusted_text(text: str, *, max_len: int = 2000) -> SanitizedText:
    """Full sanitization pipeline for a single untrusted string field.

    Pipeline: normalize → neutralize → scrub_credentials → cap to max_len.
    The single entry point content_gate calls per string field.

    Args:
        text: Raw untrusted string (from web page content, title, URL path, etc.).
        max_len: Maximum output length in characters. Default 2000.

    Returns:
        SanitizedText with cleaned text, triggered flags, and truncation flag.
    """
    if not text:
        return SanitizedText(text="", flags=[], truncated=False)

    normed = normalize(text)
    neutralized, flags = neutralize(normed)
    scrubbed = scrub_credentials(neutralized)

    truncated = len(scrubbed) > max_len
    final_text = scrubbed[:max_len] if truncated else scrubbed

    return SanitizedText(text=final_text, flags=flags, truncated=truncated)


def is_predominantly_instructional(sanitized: SanitizedText, *, raw_len: int) -> bool:
    """Structural rejection signal: is the content mostly injection payloads?

    Returns True when the document should be rejected as unsalvageable:
    - Empty (or whitespace-only) after cleaning.
    - So small after neutralization relative to original that real content is gone.
    - Contains enough neutralization markers to indicate the page is primarily
      an injection payload.

    Args:
        sanitized: Output of sanitize_untrusted_text.
        raw_len: Character length of the original (pre-sanitization) text.

    Returns:
        True if the document is predominantly instructional.
    """
    stripped = sanitized.text.strip()

    # Empty after cleaning
    if not stripped:
        return True

    # Count neutralization markers
    neutralized_count = sanitized.text.count(_NEUTRALIZED)
    if neutralized_count >= _PREDOMINANTLY_INSTRUCTIONAL_THRESHOLD:
        return True

    # Very little real content remains relative to original
    return raw_len > 0 and len(stripped) < _MIN_SALVAGEABLE_CHARS
