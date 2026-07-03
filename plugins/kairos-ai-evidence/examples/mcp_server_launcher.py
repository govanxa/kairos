"""Launcher pattern for ``kairos-evidence-mcp`` with a configured retriever.

Configuration is programmatic (Decision 3, D3): the owner writes a small
launcher like this one that imports a real retrieval function as a plain
Python callable and passes it to ``create_server(retriever=...)``. There is
no environment-variable / import-by-string retriever resolution — that path
is a code-execution vector and is deferred (S16).

Runs as-is with a tiny offline stub retriever (no network calls), so it is
demoable and CI-smokeable without a live search backend. Swap
``offline_stub_retriever`` for your own retrieval function — see the
commented block below for the real wiring pattern.

Run (after installing the ``[mcp]`` extra)::

    pip install "kairos-ai-evidence[mcp]"
    python examples/mcp_server_launcher.py
"""

from __future__ import annotations

from typing import Any

from kairos_ai_evidence.mcp.server import create_server

# A web_search-shaped payload (matches the Retriever return-shape contract:
# a list of docs, OR {query, answer, results: [...]}, OR fetch_url dicts).
_STUB_RESULTS: dict[str, Any] = {
    "query": "stub",
    "results": [
        {
            "url": "https://example.org/stub-result",
            "title": "Offline Stub Result",
            "snippet": (
                "This is a placeholder result from the offline stub retriever "
                "shipped with the launcher example — replace it with a real "
                "search backend for production use."
            ),
        }
    ],
}


def offline_stub_retriever(query: str, *, max_results: int) -> dict[str, Any]:
    """A deterministic, network-free retriever for demos and CI smoke tests.

    Args:
        query: The search query (ignored — this stub always returns the
            same placeholder payload).
        max_results: Requested result count (ignored by this stub).

    Returns:
        A ``web_search``-shaped payload with one placeholder document.
    """
    return _STUB_RESULTS


# ---------------------------------------------------------------------------
# The real wiring pattern (commented — NOT imported here). Replace the stub
# above with your own retrieval function, e.g. a Vanxa MCP web_search tool
# imported as a plain Python callable:
#
#     from vanxa_mcp.tools import _web_search  # your own MCP tool module
#
#     def create_production_server():
#         return create_server(retriever=_web_search)
#
# The human authorship of this import IS the trust boundary (S16): there is
# no string-based / environment-driven retriever resolution to audit.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    server = create_server(retriever=offline_stub_retriever)
    server.run()
