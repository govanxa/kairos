"""Tests for kairos_ai_evidence.mcp.limits (D3).

Test-after per the Evidence Engine exception (CLAUDE.md). No `mcp` SDK import
required — this module is pure stdlib validation logic.

Groups:
    TestFailurePaths       — boundary-limit violations, malformed as_of
    TestBoundaryConditions — exactly-at-limit values, clamping edges
    TestBasicBehavior      — happy-path validation passthrough
    TestSecurity           — offending content never echoed in error messages
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from kairos_ai_evidence.mcp.limits import (
    MAX_CLAIM_LEN,
    MAX_CLAIMS,
    MAX_DOCUMENTS,
    MAX_QUERY_LEN,
    MAX_RESULTS_CAP,
    MAX_TOTAL_INPUT_BYTES,
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

# ---------------------------------------------------------------------------
# TestFailurePaths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_too_many_documents_rejected(self) -> None:
        docs = [{"url": f"https://example.org/{i}"} for i in range(MAX_DOCUMENTS + 1)]
        with pytest.raises(InputLimitError):
            validate_documents(docs)

    def test_non_dict_document_item_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_documents([{"url": "https://example.org/1"}, "not-a-dict"])

    def test_documents_not_a_list_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_documents({"url": "https://example.org/1"})

    def test_too_many_claims_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_claims([f"claim {i}" for i in range(MAX_CLAIMS + 1)])

    def test_claims_not_a_list_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_claims("a single claim string")

    def test_oversized_claim_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_claims(["x" * (MAX_CLAIM_LEN + 1)])

    def test_non_str_claim_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_claims(["a valid claim", 42])

    def test_empty_claim_string_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_claims(["   "])

    def test_empty_string_claim_in_list_rejected(self) -> None:
        """A bare empty string (not just whitespace) mixed into an otherwise
        valid list must be rejected — the empty-string branch of the per-item
        check, distinct from the whitespace case above."""
        with pytest.raises(InputLimitError):
            validate_claims(["a valid claim", ""])

    def test_empty_claims_list_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_claims([])

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_query("   ")

    def test_non_str_query_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_query(12345)

    def test_none_query_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_query(None)

    def test_oversized_query_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_query("x" * (MAX_QUERY_LEN + 1))

    def test_as_of_calendar_invalid_rejected(self) -> None:
        """Month 13, day 99 — syntactically shaped but not a real date."""
        with pytest.raises(InputLimitError):
            validate_as_of("2026-13-99")

    def test_as_of_tail_bytes_rejected(self) -> None:
        """re.match would accept a valid prefix with trailing garbage; fullmatch must not."""
        with pytest.raises(InputLimitError):
            validate_as_of("2026-07-01<inj>")

    def test_as_of_non_str_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_as_of(20260701)

    def test_as_of_wrong_format_rejected(self) -> None:
        with pytest.raises(InputLimitError):
            validate_as_of("07/01/2026")

    def test_total_size_over_cap_rejected(self) -> None:
        """SEV-001 Advisory A1 — combined content size over MAX_TOTAL_INPUT_BYTES."""
        big_chunk = "x" * (MAX_TOTAL_INPUT_BYTES // 2 + 1024)
        docs = [{"content": big_chunk}, {"content": big_chunk}]
        with pytest.raises(InputLimitError):
            validate_total_size(docs)

    def test_total_size_many_medium_documents_rejected(self) -> None:
        """Many documents that each pass MAX_DOCUMENTS individually but sum over cap."""
        chunk = "x" * 200_000  # 200KB each
        docs = [{"content": chunk} for _ in range(MAX_DOCUMENTS)]  # 50 * 200KB = ~10MB
        with pytest.raises(InputLimitError):
            validate_total_size(docs)


# ---------------------------------------------------------------------------
# TestBoundaryConditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_exactly_max_documents_accepted(self) -> None:
        docs = [{"url": f"https://example.org/{i}"} for i in range(MAX_DOCUMENTS)]
        result = validate_documents(docs)
        assert len(result) == MAX_DOCUMENTS

    def test_zero_documents_accepted(self) -> None:
        assert validate_documents([]) == []

    def test_exactly_max_claims_accepted(self) -> None:
        claims = [f"claim {i}" for i in range(MAX_CLAIMS)]
        result = validate_claims(claims)
        assert len(result) == MAX_CLAIMS

    def test_exactly_max_claim_len_accepted(self) -> None:
        claim = "x" * MAX_CLAIM_LEN
        result = validate_claims([claim])
        assert result == [claim]

    def test_exactly_max_query_len_accepted(self) -> None:
        query = "x" * MAX_QUERY_LEN
        assert validate_query(query) == query

    def test_single_char_query_accepted(self) -> None:
        assert validate_query("q") == "q"

    def test_unicode_emoji_query_within_limit_accepted(self) -> None:
        """Multi-byte characters (accented, CJK, emoji) count as single Python
        str characters against MAX_QUERY_LEN and pass through unmodified."""
        query = "Did the café ☕ 日本語 event conclude?"
        assert validate_query(query) == query

    def test_unicode_emoji_claim_within_limit_accepted(self) -> None:
        claim = "El café ☕ abrió el 日本語 día"
        assert validate_claims([claim]) == [claim]

    def test_total_size_counts_utf8_bytes_not_char_count(self) -> None:
        """A 4-byte-per-char emoji payload must be measured in UTF-8 BYTES, not
        character count — the byte cap is the real memory/CPU backstop (SEV-001
        Advisory A1). A string of emoji whose byte length exceeds the cap is
        rejected even though its character count is a quarter of that."""
        # Each "☕" is 3 bytes in UTF-8; build a payload over the byte cap.
        emoji_chunk = "☕" * (MAX_TOTAL_INPUT_BYTES // 3 + 1024)
        with pytest.raises(InputLimitError):
            validate_total_size([{"content": emoji_chunk}])

    def test_as_of_none_returns_none(self) -> None:
        assert validate_as_of(None) is None

    def test_as_of_valid_date_returns_verbatim(self) -> None:
        assert validate_as_of("2026-07-01") == "2026-07-01"

    def test_clamp_max_results_zero_becomes_one(self) -> None:
        assert clamp_max_results(0) == 1

    def test_clamp_max_results_large_value_capped(self) -> None:
        assert clamp_max_results(999) == MAX_RESULTS_CAP

    def test_clamp_max_results_none_uses_default(self) -> None:
        result = clamp_max_results(None)
        assert 1 <= result <= MAX_RESULTS_CAP

    def test_clamp_max_results_negative_becomes_one(self) -> None:
        assert clamp_max_results(-5) == 1

    def test_clamp_max_results_bool_uses_default(self) -> None:
        """bool is an int subclass in Python — must not silently pass through as 0/1."""
        result = clamp_max_results(True)
        assert 1 <= result <= MAX_RESULTS_CAP

    def test_clamp_max_results_non_int_uses_default(self) -> None:
        result = clamp_max_results("5")
        assert 1 <= result <= MAX_RESULTS_CAP

    def test_total_size_exactly_at_cap_accepted(self) -> None:
        # Comfortably below the cap after accounting for dict/key overhead —
        # the important property is "does not raise", not an exact byte count.
        chunk = "x" * (MAX_TOTAL_INPUT_BYTES - 1024)
        validate_total_size([{"content": chunk}])  # must not raise

    def test_total_size_empty_documents_accepted(self) -> None:
        validate_total_size([])  # must not raise

    def test_stamp_today_with_injected_today_returns_verbatim(self) -> None:
        assert stamp_today(today=date(2020, 5, 5)) == "2020-05-05"

    def test_stamp_now_with_injected_today_is_midnight_utc(self) -> None:
        stamp = stamp_now(today=date(2020, 5, 5))
        assert stamp.startswith("2020-05-05T00:00:00")


# ---------------------------------------------------------------------------
# TestBasicBehavior
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    def test_validate_query_returns_unmodified_string(self) -> None:
        assert validate_query("What happened in June 2026?") == "What happened in June 2026?"

    def test_validate_claims_returns_list_of_strings(self) -> None:
        result = validate_claims(["claim one", "claim two"])
        assert result == ["claim one", "claim two"]

    def test_validate_documents_returns_list_of_dicts(self) -> None:
        docs = [{"url": "https://example.org/a"}, {"url": "https://example.org/b"}]
        assert validate_documents(docs) == docs

    def test_stamp_today_matches_iso_date_shape(self) -> None:
        stamp = stamp_today()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp)

    def test_stamp_today_is_a_valid_calendar_date(self) -> None:
        # validate_as_of applies the same fullmatch + fromisoformat check used
        # elsewhere; a self-produced stamp must always pass its own validator.
        assert validate_as_of(stamp_today()) == stamp_today()

    def test_stamp_today_without_today_uses_real_clock(self) -> None:
        """Production behavior (today=None) is unaffected by the new parameter."""
        assert stamp_today() == stamp_today(today=None)

    def test_stamp_now_matches_full_iso_datetime_shape(self) -> None:
        stamp = stamp_now()
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", stamp)

    def test_stamp_now_without_today_uses_real_clock(self) -> None:
        # Both calls happen within the same test — dates must match (times may
        # differ by microseconds, so only compare the date portion).
        assert stamp_now()[:10] == stamp_now(today=None)[:10]


# ---------------------------------------------------------------------------
# TestSecurity
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_oversized_claim_message_never_echoes_content(self) -> None:
        sentinel = "SENTINEL_CLAIM_PAYLOAD_XYZ"
        claim = sentinel + ("x" * MAX_CLAIM_LEN)
        with pytest.raises(InputLimitError) as excinfo:
            validate_claims([claim])
        assert sentinel not in str(excinfo.value)

    def test_too_many_documents_message_never_echoes_content(self) -> None:
        sentinel = "SENTINEL_DOC_PAYLOAD_XYZ"
        docs = [{"url": f"https://example.org/{i}", "marker": sentinel} for i in range(60)]
        with pytest.raises(InputLimitError) as excinfo:
            validate_documents(docs)
        assert sentinel not in str(excinfo.value)

    def test_oversized_query_message_never_echoes_content(self) -> None:
        sentinel = "SENTINEL_QUERY_PAYLOAD_XYZ"
        query = sentinel + ("x" * MAX_QUERY_LEN)
        with pytest.raises(InputLimitError) as excinfo:
            validate_query(query)
        assert sentinel not in str(excinfo.value)

    def test_malformed_as_of_message_never_echoes_content(self) -> None:
        sentinel = "2026-99-99<script>alert(1)</script>"
        with pytest.raises(InputLimitError) as excinfo:
            validate_as_of(sentinel)
        assert sentinel not in str(excinfo.value)

    def test_input_limit_error_is_a_value_error(self) -> None:
        """InputLimitError must be catchable alongside plain ValueError."""
        assert issubclass(InputLimitError, ValueError)

    def test_total_size_message_never_echoes_content(self) -> None:
        sentinel = "SENTINEL_BIG_CONTENT_PAYLOAD_XYZ"
        big_chunk = sentinel + ("x" * MAX_TOTAL_INPUT_BYTES)
        with pytest.raises(InputLimitError) as excinfo:
            validate_total_size([{"content": big_chunk}])
        assert sentinel not in str(excinfo.value)
