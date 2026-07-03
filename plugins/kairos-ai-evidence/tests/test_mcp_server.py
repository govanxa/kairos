"""Tests for kairos_ai_evidence.mcp.server (D3).

Requires the optional `mcp` SDK — the whole module is skipped via
`pytest.importorskip("mcp")` when it is not installed (local devs without the
`[mcp]` extra still get a green run; the plugin CI lane installs `.[mcp]` so
this file is exercised and counted toward coverage).

Groups:
    TestServerWiring   — create_server(), exactly-two-tool registration,
                         delegation to tools.*_impl, RetrieverNotConfigured path
    TestLoggingHygiene — stderr-only logging, no stdout handler
    TestMainSmoke      — console entry point with app.run patched
    TestCleanCoreImport — import kairos_ai_evidence without mcp available
    TestModuleHygiene  — server.py imports only mcp + stdlib + siblings
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
import sys
from typing import Any

import pytest

pytest.importorskip("mcp")

from kairos_ai_evidence.mcp import server as server_module  # noqa: E402
from kairos_ai_evidence.mcp import tools  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_tool(app: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Call a registered tool synchronously and return its structured output."""
    content, structured = asyncio.run(app.call_tool(name, arguments))
    return structured if structured is not None else content


# ---------------------------------------------------------------------------
# TestServerWiring
# ---------------------------------------------------------------------------


class TestServerWiring:
    def test_create_server_builds_fastmcp_app(self) -> None:
        from mcp.server.fastmcp import FastMCP

        app = server_module.create_server()
        assert isinstance(app, FastMCP)

    def test_exactly_two_tools_registered(self) -> None:
        app = server_module.create_server()
        registered = asyncio.run(app.list_tools())
        names = sorted(t.name for t in registered)
        assert names == ["evaluate_evidence", "verified_answer"]

    def test_evaluate_evidence_delegates_to_impl(self, monkeypatch: Any) -> None:
        sentinel = {"sentinel": "evaluate-evidence-delegation-marker"}
        called_with: dict[str, Any] = {}

        def fake_impl(documents: Any, claims: Any, query: Any, as_of: Any, **kwargs: Any) -> Any:
            called_with.update(
                documents=documents, claims=claims, query=query, as_of=as_of, **kwargs
            )
            return sentinel

        monkeypatch.setattr(tools, "evaluate_evidence_impl", fake_impl)
        app = server_module.create_server()

        result = _call_tool(
            app,
            "evaluate_evidence",
            {"documents": [], "claims": ["c"], "query": "q", "as_of": None},
        )
        assert result == sentinel
        assert called_with["query"] == "q"
        assert called_with["claims"] == ["c"]

    def test_verified_answer_delegates_to_impl(self, monkeypatch: Any) -> None:
        sentinel = {"sentinel": "verified-answer-delegation-marker"}
        called_with: dict[str, Any] = {}

        def fake_impl(query: Any, retriever: Any, **kwargs: Any) -> Any:
            called_with["query"] = query
            called_with["retriever"] = retriever
            called_with.update(kwargs)
            return sentinel

        monkeypatch.setattr(tools, "verified_answer_impl", fake_impl)
        app = server_module.create_server(retriever=lambda q, *, max_results: [])

        result = _call_tool(app, "verified_answer", {"query": "q"})
        assert result == sentinel
        assert called_with["query"] == "q"
        assert called_with["retriever"] is not None

    def test_create_server_no_retriever_verified_answer_not_configured(self) -> None:
        app = server_module.create_server(retriever=None)
        result = _call_tool(app, "verified_answer", {"query": "q"})
        assert result["error"]["type"] == "RetrieverNotConfigured"

    def test_create_server_no_retriever_evaluate_evidence_works(self) -> None:
        app = server_module.create_server(retriever=None)
        result = _call_tool(
            app, "evaluate_evidence", {"documents": [], "claims": ["a claim"], "query": "q"}
        )
        assert "error" not in result

    def test_create_server_custom_name(self) -> None:
        app = server_module.create_server(name="my-custom-evidence-server")
        assert app.name == "my-custom-evidence-server"

    def test_tool_signatures_have_no_trust_policy_argument(self) -> None:
        """EE-5/T5 — the wire-facing tool signatures accept no trust_policy arg."""
        app = server_module.create_server()
        registered = asyncio.run(app.list_tools())
        for tool in registered:
            properties = tool.inputSchema.get("properties", {})
            assert "trust_policy" not in properties
            assert "noise_phrases" not in properties


# ---------------------------------------------------------------------------
# TestLoggingHygiene
# ---------------------------------------------------------------------------


class TestLoggingHygiene:
    def test_configure_logging_targets_stderr_only(self) -> None:
        server_module._configure_logging("INFO")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.handlers[0].stream is sys.stderr

    def test_stdout_has_no_logging_handler(self) -> None:
        server_module._configure_logging("WARNING")
        root = logging.getLogger()
        for handler in root.handlers:
            stream = getattr(handler, "stream", None)
            assert stream is not sys.stdout

    def test_retriever_provenance_no_retriever(self) -> None:
        assert server_module._retriever_provenance(None) == "no retriever configured"

    def test_retriever_provenance_named_function(self) -> None:
        def my_retriever(query: str, *, max_results: int) -> list[dict[str, str]]:
            return []

        provenance = server_module._retriever_provenance(my_retriever)
        assert "my_retriever" in provenance
        assert __name__ not in provenance or "test_mcp_server" in provenance


# ---------------------------------------------------------------------------
# TestMainSmoke
# ---------------------------------------------------------------------------


class TestMainSmoke:
    def test_main_configures_server_and_runs_stdio(self, monkeypatch: Any) -> None:
        ran: dict[str, bool] = {"called": False}

        class _FakeApp:
            def run(self) -> None:
                ran["called"] = True

        captured_kwargs: dict[str, Any] = {}

        def fake_create_server(**kwargs: Any) -> _FakeApp:
            captured_kwargs.update(kwargs)
            return _FakeApp()

        monkeypatch.setattr(server_module, "create_server", fake_create_server)

        server_module.main(["--log-level", "WARNING"])

        assert ran["called"] is True
        assert captured_kwargs["retriever"] is None

    def test_main_logs_provenance_line_to_stderr(self, monkeypatch: Any, capsys: Any) -> None:
        class _FakeApp:
            def run(self) -> None:
                pass

        monkeypatch.setattr(server_module, "create_server", lambda **kwargs: _FakeApp())

        server_module.main(["--log-level", "INFO"])

        captured = capsys.readouterr()
        assert "no retriever configured" in captured.err
        assert captured.out == ""

    def test_main_rejects_invalid_log_level(self, monkeypatch: Any) -> None:
        class _FakeApp:
            def run(self) -> None:
                pass

        monkeypatch.setattr(server_module, "create_server", lambda **kwargs: _FakeApp())

        with pytest.raises(SystemExit):
            server_module.main(["--log-level", "NOT-A-LEVEL"])

    def test_main_default_argv_uses_sys_argv(self, monkeypatch: Any) -> None:
        class _FakeApp:
            def run(self) -> None:
                pass

        monkeypatch.setattr(server_module, "create_server", lambda **kwargs: _FakeApp())
        monkeypatch.setattr(sys, "argv", ["kairos-evidence-mcp", "--log-level", "ERROR"])

        server_module.main()  # argv=None -> falls back to sys.argv[1:]


# ---------------------------------------------------------------------------
# TestCleanCoreImport
# ---------------------------------------------------------------------------


class TestCleanCoreImport:
    def test_import_kairos_ai_evidence_without_mcp_available(self) -> None:
        """Clean-core invariant (06 §3): `import kairos_ai_evidence` must never
        require `mcp`, even in an environment where `mcp` happens to be
        installed (this dev/CI [mcp] lane). Verified by blocking `mcp` imports
        in a subprocess and confirming the top-level import still succeeds.
        """
        script = (
            "import builtins\n"
            "_real_import = builtins.__import__\n"
            "def _blocking_import(name, *args, **kwargs):\n"
            "    if name == 'mcp' or name.startswith('mcp.'):\n"
            "        raise ImportError('mcp intentionally blocked for this test')\n"
            "    return _real_import(name, *args, **kwargs)\n"
            "builtins.__import__ = _blocking_import\n"
            "import kairos_ai_evidence\n"
            "print('CLEAN_CORE_IMPORT_OK')\n"
        )
        result = subprocess.run(  # noqa: S603 - fixed literal script + sys.executable, no untrusted input
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "CLEAN_CORE_IMPORT_OK" in result.stdout

    def test_mcp_subpackage_init_imports_server(self) -> None:
        """kairos_ai_evidence.mcp itself DOES require mcp — only the top-level
        package stays dependency-free."""
        import kairos_ai_evidence.mcp as mcp_pkg

        assert hasattr(mcp_pkg, "create_server")
        assert hasattr(mcp_pkg, "main")
        assert hasattr(mcp_pkg, "Retriever")


# ---------------------------------------------------------------------------
# TestModuleHygiene
# ---------------------------------------------------------------------------


class TestModuleHygiene:
    def test_server_module_has_no_model_or_network_imports(self) -> None:
        """server.py legitimately imports `mcp` (it's the only module that may) —
        but must never import a model adapter or a raw network client library."""
        source = inspect.getsource(server_module)
        forbidden = (
            "import requests",
            "import httpx",
            "urllib.request",
            "import anthropic",
            "import openai",
            "kairos.adapters",
        )
        for token in forbidden:
            assert token not in source

    def test_server_module_only_third_party_import_is_mcp(self) -> None:
        """Confirms the legitimate exception: `mcp` IS imported here (that's the
        whole point of this module) — this is the one place it is allowed."""
        source = inspect.getsource(server_module)
        assert "from mcp.server.fastmcp import FastMCP" in source
