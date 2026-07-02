"""Evidence Engine fixtures — INJECTION_SENTINEL, loader, MCP ingestion, family index (D3/D4).

MCP wire shapes (Vanxa MCP output):
  fetch_url   → {url, title, text}
  web_search  → {title, url, snippet}

Fixture JSONs store documents in raw MCP wire shape. Call ingest_mcp_documents()
to map them to gate-ready format before passing to the pipeline as raw_documents.
fetched_at is stamped at ingestion time (per contract 03 §2 — not required on wire).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# D4: single source of truth for the injection sentinel constant.
# Mirrored literally inside poisoned_injection.json.
# Distinctive and non-dictionary so it can't appear in benign content.
INJECTION_SENTINEL: str = "KAIROS_INJECT_SENTINEL_7Q2X"

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Known fixture family ids → JSON filename stems.
FIXTURE_FAMILIES: dict[str, str] = {
    "event_outcome_agreement": "event_outcome_agreement",
    "breaking_news_mixed_provenance": "breaking_news_mixed_provenance",
    "numeric_value_comparison": "numeric_value_comparison",
    "poisoned_injection": "poisoned_injection",
    "conflicting_sources": "conflicting_sources",
}


def ingest_mcp_documents(
    mcp_docs: list[dict[str, Any]],
    *,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """Map raw MCP wire-shape documents to gate-ready format.

    Handles both Vanxa MCP output shapes:
    - fetch_url:   {url, title, text}     → maps text   → content
    - web_search:  {title, url, snippet}  → maps snippet → content

    fetched_at is stamped at ingestion time (per 03 §2 contract — not required
    from the MCP wire). published_at is passed through if present (some MCP
    responses include it), otherwise omitted (gate treats it as None).

    Args:
        mcp_docs: List of raw MCP wire-shape dicts.
        fetched_at: ISO 8601 UTC timestamp to stamp. Defaults to now().

    Returns:
        List of gate-ready dicts with: url, title, content, fetched_at,
        published_at (optional).
    """
    stamp = fetched_at or datetime.now(tz=UTC).isoformat()
    result: list[dict[str, Any]] = []
    for doc in mcp_docs:
        if not isinstance(doc, dict):
            continue
        # Prefer 'text' (fetch_url), then 'snippet' (web_search), then 'content' (legacy).
        content: str = doc.get("text") or doc.get("snippet") or doc.get("content") or ""
        ingested: dict[str, Any] = {
            "url": doc.get("url", ""),
            "title": doc.get("title"),
            "content": content,
            "fetched_at": stamp,
        }
        # published_at is optional — pass through if present on the wire.
        if "published_at" in doc:
            ingested["published_at"] = doc["published_at"]
        result.append(ingested)
    return result


def load_fixture(family_id: str) -> dict[str, Any]:
    """Load a fixture family by id.

    Args:
        family_id: One of the keys in FIXTURE_FAMILIES.

    Returns:
        Parsed fixture dict (documents, claims, query, as_of, expected).

    Raises:
        KeyError: Unknown family_id.
        FileNotFoundError: Fixture file missing.
        ValueError: Fixture JSON is invalid.
    """
    if family_id not in FIXTURE_FAMILIES:
        raise KeyError(f"Unknown fixture family: {family_id!r}. Known: {list(FIXTURE_FAMILIES)}")

    stem = FIXTURE_FAMILIES[family_id]
    path = _FIXTURES_DIR / f"{stem}.json"

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Fixture file not found: {path}") from exc

    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Fixture {family_id!r} is not valid JSON: {exc}") from exc

    return data
