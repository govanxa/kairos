"""MCP server wiring — the ONLY module in this package that imports ``mcp`` (D3).

Registers the two MCP tools (``evaluate_evidence``, ``verified_answer``) as
thin one-line wrappers delegating to ``kairos_ai_evidence.mcp.tools``. Carries
zero pipeline logic itself — every behavioral decision, validation rule, and
security boundary lives in ``tools.py``/``limits.py``/``retriever.py``, which
import no third-party package and are testable without the ``mcp`` SDK.

stdio transport only (matches the Vanxa MCP ``app.run()`` pattern). Logging
is configured to stderr exclusively — stdout is reserved for the MCP protocol
stream; a stray log line on stdout would corrupt MCP framing.

Trust policy / noise phrases / clock overrides are ``create_server`` keyword
arguments only (EE-5, T5) — the registered tool signatures accept no such
argument, so there is no wire path for a caller to influence the trust
configuration.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from kairos_ai_evidence.mcp import tools
from kairos_ai_evidence.mcp.retriever import Retriever

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_NAME: str = "kairos-evidence"


def create_server(
    *,
    retriever: Retriever | None = None,
    trust_policy: dict[str, Any] | None = None,
    noise_phrases: list[str] | None = None,
    today: date | None = None,
    name: str = _DEFAULT_SERVER_NAME,
) -> FastMCP:
    """Build a configured FastMCP app with the two Evidence Engine tools.

    ``trust_policy``, ``noise_phrases``, and ``today`` are captured by the
    closures below and threaded into every tool call as constructor config —
    they are never exposed as tool arguments on the wire (EE-5).

    Args:
        retriever: A configured ``Retriever`` callable, or ``None`` (default)
            to ship the retrieval-agnostic server where only
            ``evaluate_evidence`` is functional.
        trust_policy: Optional trust-policy config threaded into the
            evaluator factory.
        noise_phrases: Optional custom noise phrases for value extraction.
        today: Optional clock override for the evaluator (testing only).
        name: The MCP server name advertised to clients.

    Returns:
        A ``FastMCP`` app with exactly two tools registered:
        ``evaluate_evidence`` and ``verified_answer``.
    """
    app = FastMCP(name)

    @app.tool()
    def evaluate_evidence(
        documents: list[dict[str, Any]],
        claims: list[str],
        query: str,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Gate and evaluate caller-supplied documents against claims.

        Retrieval-agnostic: works with no retriever configured. Documents are
        untrusted input — they are sanitized by the content gate before any
        claim is evaluated. Returns a deterministic evidence bundle; never
        the calling model's job (no LLM runs inside this tool).

        Args:
            documents: Document dicts, each with at least a ``url`` and
                body text (``content``/``text``/``snippet``).
            claims: Claim strings to evaluate against the documents.
            query: The original question driving evaluation.
            as_of: Optional ISO ``YYYY-MM-DD`` date the documents reflect;
                omit to use a machine-stamped date.

        Returns:
            The EvidenceResponse dict, or a structured error dict.
        """
        return tools.evaluate_evidence_impl(
            documents,
            claims,
            query,
            as_of,
            trust_policy=trust_policy,
            noise_phrases=noise_phrases,
            today=today,
        )

    @app.tool()
    def verified_answer(
        query: str,
        claims: list[str] | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Retrieve, gate, and evaluate evidence for a query in one call.

        The stronger firewall: retrieval happens server-side, behind the
        content gate, so the calling model cannot substitute stale priors
        for evidence. Requires a retriever configured via
        ``create_server(retriever=...)`` — without one, returns a structured
        ``RetrieverNotConfigured`` error directing the caller to
        ``evaluate_evidence``. ``as_of`` is always machine-stamped fresh on
        every call.

        Args:
            query: The question to answer.
            claims: Optional claim strings; defaults to ``[query]``.
            max_results: Optional result-count hint (clamped to a safe range).

        Returns:
            The EvidenceResponse dict, or a structured error dict.
        """
        return tools.verified_answer_impl(
            query,
            retriever,
            claims=claims,
            max_results=max_results,
            trust_policy=trust_policy,
            noise_phrases=noise_phrases,
            today=today,
        )

    return app


def _configure_logging(level: str) -> None:
    """Configure root logging to stderr only, replacing any existing handlers.

    stdout is reserved for the MCP protocol stream — a logging handler
    targeting stdout would corrupt the stdio transport framing.

    Args:
        level: A standard logging level name (e.g. ``"INFO"``, ``"WARNING"``).
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def _retriever_provenance(retriever: Retriever | None) -> str:
    """Build the S16 startup provenance line — structural only, never content.

    Args:
        retriever: The configured retriever callable, or ``None``.

    Returns:
        ``"no retriever configured"``, or ``"<module>.<qualname>"`` for the
        configured callable.
    """
    if retriever is None:
        return "no retriever configured"
    module = getattr(retriever, "__module__", "unknown")
    qualname = getattr(retriever, "__qualname__", getattr(retriever, "__name__", repr(retriever)))
    return f"{module}.{qualname}"


def main(
    argv: list[str] | None = None,
    *,
    retriever: Retriever | None = None,
    trust_policy: dict[str, Any] | None = None,
    noise_phrases: list[str] | None = None,
) -> None:
    """Console entry point for ``kairos-evidence-mcp``.

    Ships with the retrieval-agnostic default: the console script starts
    with no retriever configured (``evaluate_evidence`` fully functional,
    ``verified_answer`` returns ``RetrieverNotConfigured``). To run a server
    with a retriever configured, write a launcher script that calls
    ``create_server(retriever=...).run()`` directly (see
    ``examples/mcp_server_launcher.py``) — import-by-string retriever
    resolution from the environment is deferred (S16: code-execution vector).

    Args:
        argv: Command-line arguments (excluding argv[0]); defaults to
            ``sys.argv[1:]`` when ``None``. Exposed as a parameter for
            testability.
        retriever: Optional retriever, for programmatic embedding by callers
            that construct their own entry point around this function.
        trust_policy: Optional trust-policy config, as above.
        noise_phrases: Optional noise-phrase config, as above.
    """
    parser = argparse.ArgumentParser(prog="kairos-evidence-mcp")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (logs go to stderr only; default: INFO).",
    )
    args = parser.parse_args(argv)

    _configure_logging(args.log_level)
    logger.info(
        "kairos-evidence-mcp starting; retriever=%s",
        _retriever_provenance(retriever),
    )

    app = create_server(
        retriever=retriever,
        trust_policy=trust_policy,
        noise_phrases=noise_phrases,
    )
    app.run()


if __name__ == "__main__":  # pragma: no cover - exercised via console script
    main()
