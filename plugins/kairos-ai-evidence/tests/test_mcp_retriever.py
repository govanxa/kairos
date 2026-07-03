"""Tests for kairos_ai_evidence.mcp.retriever (D3).

Test-after per the Evidence Engine exception (CLAUDE.md). No `mcp` SDK import
required — this module is pure stdlib normalization logic + a Protocol.

Groups:
    TestFailurePaths       — malformed/unrecognizable payloads normalize to []
    TestBoundaryConditions — empty inputs, single documents
    TestBasicBehavior      — wire-shape normalization (web_search, fetch_url)
    TestSecurity           — the answer box is never ingested (T3); no
                              env-var / import-by-string machinery exists (S16)
"""

from __future__ import annotations

import inspect
import time
from datetime import date

from kairos_ai_evidence.mcp import retriever as retriever_module
from kairos_ai_evidence.mcp.limits import MAX_DOCUMENTS
from kairos_ai_evidence.mcp.retriever import (
    Retriever,
    normalize_retrieved_documents,
)

_FETCHED_AT = "2026-07-01T12:00:00Z"

# ---------------------------------------------------------------------------
# TestFailurePaths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_non_dict_non_list_payload_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents("not a payload", fetched_at=_FETCHED_AT) == []

    def test_int_payload_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents(42, fetched_at=_FETCHED_AT) == []  # type: ignore[arg-type]

    def test_none_payload_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents(None, fetched_at=_FETCHED_AT) == []  # type: ignore[arg-type]

    def test_dict_without_results_or_doc_keys_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents({"foo": "bar"}, fetched_at=_FETCHED_AT) == []

    def test_list_of_junk_items_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents([1, "x", None, []], fetched_at=_FETCHED_AT) == []

    def test_results_containing_non_dict_items_are_skipped(self) -> None:
        payload = {"results": [{"url": "https://example.org/a", "content": "body"}, "junk", 5]}
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert len(out) == 1
        assert out[0]["url"] == "https://example.org/a"


# ---------------------------------------------------------------------------
# TestBoundaryConditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_empty_list_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents([], fetched_at=_FETCHED_AT) == []

    def test_empty_results_wrapper_normalizes_to_empty(self) -> None:
        assert normalize_retrieved_documents({"results": []}, fetched_at=_FETCHED_AT) == []

    def test_single_bare_document_normalizes(self) -> None:
        payload = [{"url": "https://example.org/a", "content": "body text"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert len(out) == 1
        assert out[0]["content"] == "body text"

    def test_default_fetched_at_is_stamped_when_omitted(self) -> None:
        payload = [{"url": "https://example.org/a", "content": "body"}]
        out = normalize_retrieved_documents(payload)
        assert isinstance(out[0]["fetched_at"], str) and out[0]["fetched_at"]

    def test_default_fetched_at_is_full_iso_datetime_not_date_only(self) -> None:
        """Low #2 — unified with evaluate_evidence's fetched_at shape: a full
        ISO-8601 datetime, not a bare YYYY-MM-DD date."""
        payload = [{"url": "https://example.org/a", "content": "body"}]
        out = normalize_retrieved_documents(payload)
        assert "T" in out[0]["fetched_at"]

    def test_default_fetched_at_respects_injected_today(self) -> None:
        payload = [{"url": "https://example.org/a", "content": "body"}]
        out = normalize_retrieved_documents(payload, today=date(2020, 5, 5))
        assert out[0]["fetched_at"].startswith("2020-05-05T00:00:00")

    def test_exactly_max_documents_all_accepted(self) -> None:
        payload = [
            {"url": f"https://example.org/{i}", "content": "body"} for i in range(MAX_DOCUMENTS)
        ]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert len(out) == MAX_DOCUMENTS

    def test_custom_max_documents_override(self) -> None:
        payload = [{"url": f"https://example.org/{i}", "content": "body"} for i in range(20)]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT, max_documents=5)
        assert len(out) == 5

    def test_max_documents_zero_returns_no_documents(self) -> None:
        payload = [{"url": "https://example.org/a", "content": "body"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT, max_documents=0)
        assert out == []


# ---------------------------------------------------------------------------
# TestBasicBehavior — wire-shape normalization
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    def test_web_search_wrapper_flattened(self) -> None:
        payload = {
            "query": "test query",
            "answer": "some summary",
            "results": [
                {"url": "https://a.example.org", "title": "A", "snippet": "snippet A"},
                {"url": "https://b.example.org", "title": "B", "snippet": "snippet B"},
            ],
        }
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert len(out) == 2
        assert out[0]["content"] == "snippet A"
        assert out[1]["content"] == "snippet B"
        assert out[0]["fetched_at"] == _FETCHED_AT

    def test_fetch_url_shape_maps_text_to_content(self) -> None:
        payload = [{"url": "https://example.org/page", "title": "Page", "text": "full text body"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["content"] == "full text body"

    def test_content_key_passthrough(self) -> None:
        payload = [{"url": "https://example.org/page", "content": "already content-shaped"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["content"] == "already content-shaped"

    def test_unicode_emoji_content_preserved_intact(self) -> None:
        """The normalizer reshapes only — multi-byte content (accents, CJK,
        emoji) must survive byte-for-byte; no encoding/decoding mangling."""
        body = "Le café ☕ a ouvert — 日本語 テスト 🎉"
        payload = [{"url": "https://example.org/page", "content": body}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["content"] == body

    def test_unicode_title_preserved_intact(self) -> None:
        title = "日本語のタイトル ☕"
        payload = [{"url": "https://example.org/page", "title": title, "content": "body"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["title"] == title

    def test_published_at_passed_through_when_present(self) -> None:
        payload = [
            {
                "url": "https://example.org/page",
                "content": "body",
                "published_at": "2026-06-30T00:00:00Z",
            }
        ]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["published_at"] == "2026-06-30T00:00:00Z"

    def test_published_at_absent_when_not_present(self) -> None:
        payload = [{"url": "https://example.org/page", "content": "body"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert "published_at" not in out[0]

    def test_title_passed_through_as_is(self) -> None:
        payload = [{"url": "https://example.org/page", "title": "My Title", "content": "body"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["title"] == "My Title"

    def test_title_absent_becomes_none(self) -> None:
        payload = [{"url": "https://example.org/page", "content": "body"}]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out[0]["title"] is None

    def test_nested_list_of_wrappers_flattened(self) -> None:
        payload = [
            {"results": [{"url": "https://a.example.org", "content": "a"}]},
            {"results": [{"url": "https://b.example.org", "content": "b"}]},
        ]
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert {d["url"] for d in out} == {"https://a.example.org", "https://b.example.org"}

    def test_retriever_protocol_runtime_checkable(self) -> None:
        def stub(query: str, *, max_results: int) -> list[dict[str, str]]:
            return [{"url": "https://example.org", "content": "stub"}]

        assert isinstance(stub, Retriever)


# ---------------------------------------------------------------------------
# TestSecurity
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_answer_box_never_ingested(self) -> None:
        """T3 — the web_search 'answer' summary box must never become a document."""
        payload = {
            "query": "test query",
            "answer": "ATTACKER-CONTROLLED ANSWER BOX PAYLOAD",
            "results": [{"url": "https://example.org", "content": "benign body"}],
        }
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert len(out) == 1
        serialized = str(out)
        assert "ATTACKER-CONTROLLED ANSWER BOX PAYLOAD" not in serialized
        assert "answer" not in out[0]

    def test_query_key_never_ingested_as_a_document(self) -> None:
        payload = {"query": "the original query text", "results": []}
        out = normalize_retrieved_documents(payload, fetched_at=_FETCHED_AT)
        assert out == []

    def test_no_environment_variable_read_anywhere_in_module(self) -> None:
        """S16 — the import-by-string retriever path is deferred; assert it was
        never wired in: no os.environ / os.getenv reference in the module source.

        The env var name may appear in prose (explaining the deferral) but must
        never appear as an actual lookup (``os.environ["KAIROS_EVIDENCE_RETRIEVER"]``
        or ``os.environ.get(...)``/``os.getenv(...)`` called with that name).
        """
        source = inspect.getsource(retriever_module)
        assert "os.environ" not in source
        assert "os.getenv" not in source
        assert 'environ["KAIROS_EVIDENCE_RETRIEVER"' not in source
        assert 'environ.get("KAIROS_EVIDENCE_RETRIEVER"' not in source
        assert 'getenv("KAIROS_EVIDENCE_RETRIEVER"' not in source

    def test_no_import_by_string_machinery_in_module(self) -> None:
        """S16 — no importlib / import_module / __import__ / eval / exec anywhere."""
        source = inspect.getsource(retriever_module)
        for forbidden in ("importlib", "import_module", "__import__(", "eval(", "exec("):
            assert forbidden not in source

    def test_resolve_retriever_from_env_does_not_exist(self) -> None:
        """The deferred (Open Q1) env-import resolver must not be shipped in v1."""
        assert not hasattr(retriever_module, "resolve_retriever_from_env")

    def test_module_imports_no_third_party_package(self) -> None:
        """retriever.py must remain stdlib-only so it is testable without `mcp`."""
        module_file = retriever_module.__file__
        assert module_file is not None
        with open(module_file, encoding="utf-8") as fh:
            source = fh.read()
        assert "import mcp" not in source
        assert "from mcp" not in source

    def test_flood_payload_capped_at_max_documents(self) -> None:
        """SEV-001 — an untrusted retriever returning far more than MAX_DOCUMENTS
        documents must never produce more than MAX_DOCUMENTS normalized documents."""
        payload = {
            "results": [
                {"url": f"https://flood.example.org/{i}", "content": "filler"}
                for i in range(50_000)
            ]
        }
        start = time.perf_counter()
        out = normalize_retrieved_documents(payload)
        elapsed = time.perf_counter() - start

        assert len(out) <= MAX_DOCUMENTS
        assert elapsed < 1.0, f"flood normalization took {elapsed:.2f}s — cap not effective"

    def test_flood_nested_wrappers_capped_at_max_documents(self) -> None:
        """The cap holds even when the flood is spread across many nested wrappers."""
        payload = [
            {"results": [{"url": f"https://flood.example.org/{i}", "content": "x"}]}
            for i in range(50_000)
        ]
        out = normalize_retrieved_documents(payload)
        assert len(out) <= MAX_DOCUMENTS
