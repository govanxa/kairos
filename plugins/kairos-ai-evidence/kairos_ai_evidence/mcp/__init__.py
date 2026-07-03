"""kairos_ai_evidence.mcp — MCP server for the Evidence Engine (D3).

Exposes ``create_server``, ``main``, and the ``Retriever`` protocol. Requires
the optional ``mcp`` SDK — install via ``pip install "kairos-ai-evidence[mcp]"``.

This subpackage is NEVER imported by the top-level ``kairos_ai_evidence``
package (clean-core invariant, 06 §3): ``import kairos_ai_evidence`` stays
dependency-free even when ``mcp`` is not installed. Only importing
``kairos_ai_evidence.mcp`` itself (or its ``server`` submodule) requires the
``mcp`` SDK; ``limits``, ``retriever``, and ``tools`` remain stdlib-only and
importable on their own.
"""

from __future__ import annotations

from kairos_ai_evidence.mcp.retriever import Retriever
from kairos_ai_evidence.mcp.server import create_server, main

__all__ = ["Retriever", "create_server", "main"]
