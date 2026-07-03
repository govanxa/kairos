"""MCP server retriever interface + payload normalization (D3).

Defines the ``Retriever`` Protocol that ``verified_answer`` calls, and
``normalize_retrieved_documents`` — a vetted promotion of the
``examples/_fixtures.py`` ``_flatten`` logic (private examples code; promoted
here because the server module needs it).

The normalizer performs NO sanitization — it only reshapes a raw retrieval
payload into the gate-ready document shape. The content_gate (C2) remains the
sole trust boundary; raw retriever output is UNTRUSTED until it passes through
the gate (04 §1).

SEV-001 hardening: retriever output is untrusted and unbounded — a retriever
could return an arbitrarily large payload (e.g. tens of thousands of items).
``_flatten`` stops collecting once ``max_documents`` (default
``limits.MAX_DOCUMENTS``) accepted documents have been gathered, so a flood
never reaches the content gate or the pipeline's state store.

Import-by-string retriever resolution (``KAIROS_EVIDENCE_RETRIEVER`` env var)
is DEFERRED from v1 per owner decision — it is a code-execution vector (S16).
This module intentionally contains no environment-variable reads and no
import machinery of any kind. The only supported configuration path is the
programmatic ``create_server(retriever=...)`` factory.

No third-party imports — stdlib only. ``kairos_ai_evidence.mcp.limits`` is a
sibling stdlib-only module (not a third-party dependency), so importing the
``MAX_DOCUMENTS`` cap and the ``stamp_now`` helper from it does not require
the optional ``mcp`` SDK — this module remains importable and fully
unit-testable without it.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable

from kairos_ai_evidence.mcp.limits import MAX_DOCUMENTS, stamp_now

RetrieverResult = list[dict[str, Any]] | dict[str, Any]

# Wire-shape keys that mark a dict as an actual document (as opposed to a
# wrapper object like {query, answer, results: [...]}).
_DOCUMENT_KEYS: tuple[str, ...] = ("url", "snippet", "text", "content")


@runtime_checkable
class Retriever(Protocol):
    """Callable contract for a configured retriever.

    A retriever is a plain Python callable — synchronous, no async contract
    in v1. The owner configures one by writing a ~10-line launcher that
    imports their own search function and passes it to
    ``create_server(retriever=...)`` (Decision 3). There is no server-to-server
    MCP client and no import-by-string resolution.
    """

    def __call__(self, query: str, *, max_results: int) -> RetrieverResult:
        """Retrieve raw documents for a query.

        Args:
            query: The search query string.
            max_results: The (already-clamped) maximum number of results
                requested.

        Returns:
            A raw retrieval payload: a list of document dicts, a
            ``web_search``-style wrapper (``{query, answer, results: [...]}``),
            or a list of ``fetch_url``-style dicts (``{url, title, text}``).
        """
        ...  # pragma: no cover - Protocol method body, never executed directly


def _flatten(
    node: object,
    acc: list[dict[str, Any]],
    *,
    max_documents: int,
) -> list[dict[str, Any]]:
    """Recursively flatten a raw retrieval payload into a list of doc dicts.

    Tolerates a bare list of docs, a ``web_search`` wrapper with a nested
    ``results`` list, or a list of such wrappers. Non-list/non-dict nodes
    (and dicts that are neither a ``results`` wrapper nor a recognizable
    document) contribute nothing — this function never raises.

    SEV-001: collection stops as soon as ``len(acc) >= max_documents`` at any
    recursion level, so an oversized or adversarially-nested payload (e.g.
    hundreds of thousands of documents) is never fully walked — the cost is
    bounded by ``max_documents``, not by the payload size.

    Args:
        node: Any value encountered while walking the payload.
        acc: Accumulator list, mutated in place and also returned.
        max_documents: Stop collecting once this many documents are gathered.

    Returns:
        The accumulator list of raw document dicts (not yet reshaped),
        containing at most ``max_documents`` items.
    """
    if len(acc) >= max_documents:
        return acc
    if isinstance(node, list):
        for item in node:
            if len(acc) >= max_documents:
                break
            _flatten(item, acc, max_documents=max_documents)
    elif isinstance(node, dict):
        if isinstance(node.get("results"), list):
            # A web_search-style wrapper: {query, answer, results: [...]}.
            # Only "results" is descended into — "answer" (T3, an
            # attacker-influenceable summary box) is never ingested.
            _flatten(node["results"], acc, max_documents=max_documents)
        elif any(key in node for key in _DOCUMENT_KEYS):
            acc.append(node)
    return acc


def normalize_retrieved_documents(
    payload: RetrieverResult,
    *,
    fetched_at: str | None = None,
    max_documents: int = MAX_DOCUMENTS,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Flatten and reshape a raw retrieval payload into gate-ready documents.

    Tolerates the same wire shapes as the private ``examples/_fixtures.py``
    ``ingest_mcp_documents`` helper: a bare list of doc dicts, a
    ``web_search`` wrapper (``{query, answer, results: [...]}``), or
    ``fetch_url`` dicts (``{url, title, text}``). Maps
    ``text | snippet | content`` (first non-empty, in that priority order) to
    the canonical ``content`` field, drops the ``answer`` box (T3), and
    stamps ``fetched_at``.

    SEV-001: the accepted document count is capped at ``max_documents`` — the
    same cap ``evaluate_evidence`` enforces via ``validate_documents`` — so
    an untrusted retriever cannot force the content gate or the pipeline's
    state store to process an unbounded flood of documents.

    This function performs NO sanitization of document content — it only
    reshapes. The content_gate (C2) is the sole trust boundary and runs
    immediately after this step in the pipeline (04 §1).

    Args:
        payload: The raw value returned by a configured ``Retriever``.
        fetched_at: ISO 8601 UTC timestamp to stamp on every document.
            Defaults to ``stamp_now(today=today)``.
        max_documents: Hard cap on the number of documents returned (SEV-001).
            Defaults to ``limits.MAX_DOCUMENTS``.
        today: Optional deterministic date override threaded to the default
            ``fetched_at`` stamp (tests only); ignored when ``fetched_at`` is
            explicitly provided.

    Returns:
        A list of at most ``max_documents`` gate-ready dicts:
        ``{url, title, content, fetched_at}``, plus ``published_at`` when
        present on the source document. Malformed or unrecognizable payloads
        normalize to an empty list — never raises.
    """
    stamp = fetched_at or stamp_now(today=today)

    flat = _flatten(payload, [], max_documents=max_documents)

    out: list[dict[str, Any]] = []
    for doc in flat:
        if not isinstance(doc, dict):  # pragma: no cover - _flatten only ever appends dicts
            continue
        content: str = doc.get("text") or doc.get("snippet") or doc.get("content") or ""
        item: dict[str, Any] = {
            "url": doc.get("url", ""),
            "title": doc.get("title"),
            "content": content,
            "fetched_at": stamp,
        }
        if "published_at" in doc:
            item["published_at"] = doc["published_at"]
        out.append(item)
    return out
