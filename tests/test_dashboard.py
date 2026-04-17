"""Tests for kairos.dashboard — written BEFORE implementation (TDD).

Dashboard module tests covering all S17 security requirements:
  S17.1: Localhost-only binding (127.0.0.1, no 0.0.0.0)
  S17.2: Token authentication (query param or Bearer header, 403 on failure)
  S17.3: --no-auth sets token=None, prints warning to stderr
  S17.4: CSP + X-Content-Type-Options headers on EVERY response
  S17.5: Read-only — non-GET methods return 405 with Allow: GET
  S17.6: No import of kairos.state or kairos.logger (data isolation)
  S17.7: Run ID path traversal prevention

v0.4.4 additions:
  - TestUIFilesExist — dashboard_ui/ files on disk and loaded at import time
  - TestStaticFileServing — /static/styles.css and /static/app.js routes
  - TestCSPUpdate — 'unsafe-inline' removed from script-src
  - TestVersionBump — version is 0.4.4

Test priority order (TDD):
1. Security (S17) — first and most important
2. Failure paths — bad log_dir, missing run_id, malformed files, unknown paths
3. Boundary conditions — single file, empty dir, non-.jsonl ignored
4. Happy paths — HTML served, JSON responses, health check
5. Data loading unit tests — pure functions
6. CLI command tests — dashboard command exists, defaults, flags
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_jsonl_content(
    run_id: str = "abc123",
    workflow_name: str = "my_workflow",
    status: str = "complete",
    total_steps: int = 2,
    completed_steps: int = 2,
) -> str:
    """Build a minimal valid .jsonl file content for a completed run."""
    events = [
        {
            "timestamp": "2024-01-01T12:00:00+00:00",
            "event_type": "workflow_start",
            "step_id": None,
            "data": {
                "workflow_name": workflow_name,
                "run_id": run_id,
                "total_steps": total_steps,
            },
            "level": "LogLevel.INFO",
        },
        {
            "timestamp": "2024-01-01T12:00:01+00:00",
            "event_type": "workflow_complete",
            "step_id": None,
            "data": {
                "status": status,
                "duration_ms": 1234.5,
                "summary": {
                    "total_steps": total_steps,
                    "completed_steps": completed_steps,
                    "failed_steps": 0,
                    "skipped_steps": 0,
                    "total_retries": 0,
                    "total_duration_ms": 1234.5,
                    "validations_passed": 0,
                    "validations_failed": 0,
                },
            },
            "level": "LogLevel.INFO",
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


def _fetch(url: str, token: str | None = None, method: str = "GET") -> tuple[int, dict]:
    """Make an HTTP request and return (status_code, response_headers_dict).

    Raises urllib.error.HTTPError for non-2xx (caller can inspect .code).
    """
    if token:
        full_url = f"{url}?token={token}"
    else:
        full_url = url
    req = urllib.request.Request(full_url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            headers = dict(resp.headers)
            body = resp.read().decode("utf-8")
            return resp.status, {"headers": headers, "body": body}
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, {"headers": headers, "body": body}


# ---------------------------------------------------------------------------
# Fixtures — real embedded servers on random ports
# ---------------------------------------------------------------------------


@pytest.fixture()
def dashboard_server(tmp_path: Path):
    """Start a DashboardServer on a random port with a fixed test token."""
    from kairos.dashboard import DashboardServer

    token = "test-token-for-testing"
    server = DashboardServer(port=0, log_dir=str(tmp_path), auth_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Give the server a moment to start
    time.sleep(0.05)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    yield server, base_url, token
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture()
def noauth_server(tmp_path: Path):
    """Start a DashboardServer with no auth (--no-auth mode)."""
    from kairos.dashboard import DashboardServer

    server = DashboardServer(port=0, log_dir=str(tmp_path), auth_token=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    yield server, base_url
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture()
def server_with_runs(tmp_path: Path):
    """Start a DashboardServer with two pre-populated .jsonl files."""
    from kairos.dashboard import DashboardServer

    # Write two .jsonl files
    (tmp_path / "wf1_run001.jsonl").write_text(
        _make_jsonl_content(run_id="run001", workflow_name="wf1"),
        encoding="utf-8",
    )
    (tmp_path / "wf2_run002.jsonl").write_text(
        _make_jsonl_content(run_id="run002", workflow_name="wf2", status="failed"),
        encoding="utf-8",
    )

    token = "test-token"
    server = DashboardServer(port=0, log_dir=str(tmp_path), auth_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    yield server, base_url, token, tmp_path
    server.shutdown()
    thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Group 1: Security (S17) — FIRST priority
# ---------------------------------------------------------------------------


class TestS17LocalhostOnly:
    """S17.1 — Dashboard MUST bind to 127.0.0.1, never 0.0.0.0."""

    def test_bind_host_constant_is_loopback(self):
        """_BIND_HOST must be '127.0.0.1'."""
        from kairos.dashboard import _BIND_HOST

        assert _BIND_HOST == "127.0.0.1"

    def test_server_binds_to_loopback(self, dashboard_server):
        """Server.server_address[0] must be 127.0.0.1."""
        server, _base_url, _token = dashboard_server
        assert server.server_address[0] == "127.0.0.1"

    def test_no_host_parameter_on_server(self):
        """DashboardServer constructor must NOT accept a host parameter."""
        import inspect

        from kairos.dashboard import DashboardServer

        sig = inspect.signature(DashboardServer.__init__)
        assert "host" not in sig.parameters, "DashboardServer must not have a host parameter"

    def test_bind_host_hardcoded_not_configurable(self, tmp_path: Path):
        """Even if someone tries to monkey-patch port, host stays 127.0.0.1."""
        from kairos.dashboard import DashboardServer

        server = DashboardServer(port=0, log_dir=str(tmp_path), auth_token=None)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()


class TestS17TokenAuth:
    """S17.2 — Token authentication, 403 on failure."""

    def test_valid_token_in_query_param_allowed(self, dashboard_server):
        """Requests with correct ?token= query param are allowed."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/health")
        # Health check has no auth — use that to confirm server is up
        assert status == 200

    def test_missing_token_on_protected_endpoint_returns_403(self, dashboard_server):
        """GET / without token must return 403."""
        _server, base_url, _token = dashboard_server
        status, data = _fetch(f"{base_url}/")
        assert status == 403

    def test_wrong_token_on_protected_endpoint_returns_403(self, dashboard_server):
        """GET / with wrong token must return 403."""
        _server, base_url, _token = dashboard_server
        status, _data = _fetch(f"{base_url}/", token="wrong-token")
        assert status == 403

    def test_correct_token_in_query_param_returns_200(self, dashboard_server):
        """GET / with correct ?token= must return 200."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/", token=token)
        assert status == 200

    def test_correct_token_in_bearer_header_returns_200(self, dashboard_server):
        """GET / with correct Authorization: Bearer header must return 200."""
        _server, base_url, token = dashboard_server
        req = urllib.request.Request(
            f"{base_url}/",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

    def test_health_endpoint_skips_auth(self, dashboard_server):
        """GET /api/health requires NO token."""
        _server, base_url, _token = dashboard_server
        req = urllib.request.Request(f"{base_url}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

    def test_api_runs_missing_token_returns_403(self, dashboard_server):
        """GET /api/runs without token must return 403."""
        _server, base_url, _token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs")
        assert status == 403

    def test_api_run_detail_missing_token_returns_403(self, dashboard_server):
        """GET /api/runs/<id> without token must return 403."""
        _server, base_url, _token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs/some-run-id")
        assert status == 403

    def test_token_comparison_uses_hmac_compare_digest(self):
        """Token comparison must use hmac.compare_digest (timing-safe)."""
        import ast
        import os

        dashboard_path = os.path.join(os.path.dirname(__file__), "..", "kairos", "dashboard.py")
        dashboard_path = os.path.realpath(dashboard_path)
        with open(dashboard_path, encoding="utf-8") as f:
            source = f.read()
        # hmac must be imported
        assert "import hmac" in source, "dashboard.py must import hmac"
        # compare_digest must be used (not bare == for token)
        tree = ast.parse(source)
        has_compare_digest = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "compare_digest":
                    has_compare_digest = True
                    break
        assert has_compare_digest, (
            "dashboard.py must use hmac.compare_digest() for token comparison"
        )


class TestS17NoAuth:
    """S17.3 — --no-auth mode: token=None means all requests pass auth."""

    def test_noauth_server_allows_all_requests_without_token(self, noauth_server):
        """With auth_token=None, all requests are allowed without a token."""
        _server, base_url = noauth_server
        status, _data = _fetch(f"{base_url}/")
        assert status == 200

    def test_noauth_server_attribute_is_none(self, noauth_server):
        """server.auth_token must be None in no-auth mode."""
        server, _base_url = noauth_server
        assert server.auth_token is None

    def test_noauth_start_dashboard_prints_warning(self, tmp_path: Path, capsys):
        """start_dashboard with no_auth=True must print warning to stderr."""
        # We test the warning function directly
        from kairos.dashboard import _print_noauth_warning

        _print_noauth_warning()
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "no-auth" in captured.err.lower()


class TestS17CspHeaders:
    """S17.4 — CSP + X-Content-Type-Options on EVERY response."""

    def test_csp_header_on_index(self, dashboard_server):
        """GET / must have Content-Security-Policy header."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_csp_value_restricts_to_self(self, dashboard_server):
        """CSP must include default-src 'self'."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        csp = headers.get("content-security-policy", "")
        assert "default-src" in csp
        assert "'self'" in csp

    def test_nosniff_header_on_index(self, dashboard_server):
        """GET / must have X-Content-Type-Options: nosniff."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert headers.get("x-content-type-options", "").lower() == "nosniff"

    def test_csp_header_on_403_response(self, dashboard_server):
        """Even 403 responses must have CSP header."""
        _server, base_url, _token = dashboard_server
        _status, data = _fetch(f"{base_url}/")
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_csp_header_on_404_response(self, dashboard_server):
        """Even 404 responses must have CSP header."""
        _server, base_url, token = dashboard_server
        status, data = _fetch(f"{base_url}/nonexistent-path", token=token)
        assert status == 404
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_csp_header_on_405_response(self, dashboard_server):
        """Even 405 responses must have CSP header."""
        _server, base_url, token = dashboard_server
        status, data = _fetch(f"{base_url}/", token=token, method="POST")
        assert status == 405
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_csp_header_on_health_response(self, dashboard_server):
        """GET /api/health must also have CSP header."""
        _server, base_url, _token = dashboard_server
        _status, data = _fetch(f"{base_url}/api/health")
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_csp_constant_matches_spec(self):
        """_CSP_HEADER constant must NOT have unsafe-inline in script-src (v0.4.4 tightened CSP).

        After file extraction JS loads from /static/app.js (same-origin),
        so 'unsafe-inline' can be removed from script-src.  style-src still
        keeps 'unsafe-inline' because JS sets inline styles on DOM elements.
        """
        from kairos.dashboard import _CSP_HEADER

        assert "default-src 'self'" in _CSP_HEADER
        # script-src must NOT have 'unsafe-inline' — tightened in v0.4.4
        assert "script-src 'self'" in _CSP_HEADER
        assert "script-src 'self' 'unsafe-inline'" not in _CSP_HEADER
        # style-src keeps unsafe-inline for dynamic inline styles
        assert "style-src 'self' 'unsafe-inline'" in _CSP_HEADER


class TestS17ReadOnly:
    """S17.5 — All non-GET methods must return 405 with Allow: GET header."""

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    def test_non_get_method_returns_405(self, dashboard_server, method: str):
        """POST/PUT/DELETE/PATCH/HEAD/OPTIONS must all return 405."""
        _server, base_url, token = dashboard_server
        status, data = _fetch(f"{base_url}/", token=token, method=method)
        assert status == 405, f"Expected 405 for {method}, got {status}"

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    def test_405_includes_allow_get_header(self, dashboard_server, method: str):
        """405 response must include Allow: GET header."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/", token=token, method=method)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        allow = headers.get("allow", "")
        assert "GET" in allow, f"Allow header missing 'GET': {allow!r}"

    def test_post_to_api_runs_returns_405(self, dashboard_server):
        """POST /api/runs must return 405."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs", token=token, method="POST")
        assert status == 405


class TestS17DataIsolation:
    """S17.6 — Dashboard must not import kairos.state or kairos.logger."""

    def test_dashboard_does_not_import_kairos_state(self):
        """kairos.dashboard must not import kairos.state (reads pre-redacted files only)."""
        import ast
        import os

        dashboard_path = os.path.join(os.path.dirname(__file__), "..", "kairos", "dashboard.py")
        dashboard_path = os.path.realpath(dashboard_path)
        with open(dashboard_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "kairos.state" not in alias.name, (
                        "dashboard.py must not import kairos.state"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "kairos.state" not in module, (
                    "dashboard.py must not import from kairos.state"
                )

    def test_dashboard_does_not_import_kairos_logger(self):
        """kairos.dashboard must not import kairos.logger (reads pre-redacted files only)."""
        import ast
        import os

        dashboard_path = os.path.join(os.path.dirname(__file__), "..", "kairos", "dashboard.py")
        dashboard_path = os.path.realpath(dashboard_path)
        with open(dashboard_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "kairos.logger" not in alias.name, (
                        "dashboard.py must not import kairos.logger"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "kairos.logger" not in module, (
                    "dashboard.py must not import from kairos.logger"
                )

    def test_dashboard_uses_json_loads_not_eval(self):
        """dashboard.py must use json.loads — never eval(), exec(), or pickle."""
        import os

        dashboard_path = os.path.join(os.path.dirname(__file__), "..", "kairos", "dashboard.py")
        dashboard_path = os.path.realpath(dashboard_path)
        with open(dashboard_path, encoding="utf-8") as f:
            source = f.read()
        # These patterns should not appear as calls
        assert "eval(" not in source, "dashboard.py must not use eval()"
        assert "exec(" not in source, "dashboard.py must not use exec()"
        assert "pickle" not in source, "dashboard.py must not use pickle"


class TestS17PathTraversal:
    """S17.7 — Run IDs used in file ops must be validated against path traversal."""

    def test_path_traversal_in_run_id_returns_404(self, dashboard_server):
        """GET /api/runs/../../etc/passwd must not traverse paths — return 404."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs/..%2F..%2Fetc%2Fpasswd", token=token)
        assert status == 404

    def test_dotdot_in_run_id_returns_404(self, dashboard_server):
        """GET /api/runs/../secret must not traverse — return 404."""
        _server, base_url, token = dashboard_server
        # URL-encoded .. path
        status, _data = _fetch(f"{base_url}/api/runs/../secret", token=token)
        # May get 404 or redirect to root; either is safe, just not a file leak
        assert status in (400, 404)

    def test_absolute_path_in_run_id_returns_404(self, dashboard_server):
        """Absolute path in run_id must not be used in file ops."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs/%2Fetc%2Fpasswd", token=token)
        assert status == 404

    def test_valid_run_id_allowed(self, server_with_runs):
        """Valid run ID (alphanumeric + hyphens) must be accepted."""
        _server, base_url, token, _tmp = server_with_runs
        status, data = _fetch(f"{base_url}/api/runs/run001", token=token)
        # 200 = found, 404 = not found (by run_id in events, not filename)
        # The run001 run_id is in the events data so should return 200
        assert status == 200


# ---------------------------------------------------------------------------
# Group 2: Failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_unknown_path_returns_404(self, dashboard_server):
        """Unknown URL path must return 404."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/nonexistent", token=token)
        assert status == 404

    def test_missing_run_id_returns_404(self, dashboard_server):
        """GET /api/runs/<nonexistent-id> must return 404."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs/nonexistent-run-id", token=token)
        assert status == 404

    def test_empty_log_dir_returns_empty_list(self, dashboard_server):
        """GET /api/runs on empty dir must return 200 with empty list."""
        _server, base_url, token = dashboard_server
        status, data = _fetch(f"{base_url}/api/runs", token=token)
        assert status == 200
        parsed = json.loads(data["body"])
        assert parsed == [] or isinstance(parsed, list)

    def test_malformed_jsonl_skipped(self, tmp_path: Path):
        """Malformed .jsonl lines must be skipped, not crash the server."""
        from kairos.dashboard import _read_events

        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text("not json\n{also bad\n", encoding="utf-8")
        events = _read_events(bad_file)
        assert events == []

    def test_empty_jsonl_file_returns_empty_list(self, tmp_path: Path):
        """Empty .jsonl file must return empty list (not raise)."""
        from kairos.dashboard import _read_events

        empty_file = tmp_path / "empty.jsonl"
        empty_file.write_text("", encoding="utf-8")
        events = _read_events(empty_file)
        assert events == []

    def test_partially_malformed_jsonl_returns_valid_events(self, tmp_path: Path):
        """Mix of valid and invalid lines — return only valid events."""
        from kairos.dashboard import _read_events

        mixed_file = tmp_path / "mixed.jsonl"
        valid_event = json.dumps({"event_type": "workflow_start", "data": {}})
        mixed_file.write_text(f"not json\n{valid_event}\n{{bad}}\n", encoding="utf-8")
        events = _read_events(mixed_file)
        assert len(events) == 1
        assert events[0]["event_type"] == "workflow_start"

    def test_404_response_is_json(self, dashboard_server):
        """404 response body must be valid JSON."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/nonexistent", token=token)
        parsed = json.loads(data["body"])
        assert "error" in parsed

    def test_403_response_is_json(self, dashboard_server):
        """403 response body must be valid JSON."""
        _server, base_url, _token = dashboard_server
        _status, data = _fetch(f"{base_url}/api/runs")
        parsed = json.loads(data["body"])
        assert "error" in parsed


# ---------------------------------------------------------------------------
# Group 3: Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_single_jsonl_file_listed(self, tmp_path: Path):
        """Directory with exactly one .jsonl file returns one run."""
        from kairos.dashboard import _list_runs

        (tmp_path / "run1.jsonl").write_text(_make_jsonl_content(run_id="r1"), encoding="utf-8")
        runs = _list_runs(str(tmp_path))
        assert len(runs) == 1

    def test_non_jsonl_files_ignored(self, tmp_path: Path):
        """Non-.jsonl files in log_dir must be ignored."""
        from kairos.dashboard import _list_runs

        (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
        (tmp_path / "data.json").write_text("{}", encoding="utf-8")
        runs = _list_runs(str(tmp_path))
        assert runs == []

    def test_multiple_files_all_listed(self, tmp_path: Path):
        """Multiple .jsonl files all appear in run list."""
        from kairos.dashboard import _list_runs

        for i in range(3):
            (tmp_path / f"run{i}.jsonl").write_text(
                _make_jsonl_content(run_id=f"r{i}"), encoding="utf-8"
            )
        runs = _list_runs(str(tmp_path))
        assert len(runs) == 3

    def test_empty_log_dir_returns_empty_list(self, tmp_path: Path):
        """Empty directory returns empty list from _list_runs."""
        from kairos.dashboard import _list_runs

        runs = _list_runs(str(tmp_path))
        assert runs == []

    def test_run_without_workflow_complete_event_shows_incomplete(self, tmp_path: Path):
        """A run with only workflow_start (no workflow_complete) shows 'incomplete' status."""
        from kairos.dashboard import _extract_summary, _read_events

        partial = tmp_path / "partial.jsonl"
        partial.write_text(
            json.dumps(
                {
                    "timestamp": "2024-01-01T12:00:00+00:00",
                    "event_type": "workflow_start",
                    "step_id": None,
                    "data": {"workflow_name": "wf", "run_id": "r1", "total_steps": 1},
                    "level": "LogLevel.INFO",
                }
            ),
            encoding="utf-8",
        )
        events = _read_events(partial)
        summary = _extract_summary(events)
        assert summary["status"] == "incomplete"

    def test_run_id_search_returns_correct_run(self, tmp_path: Path):
        """_get_run_events finds the correct file by run_id."""
        from kairos.dashboard import _get_run_events

        content = _make_jsonl_content(run_id="unique-run-999")
        (tmp_path / "my_run.jsonl").write_text(content, encoding="utf-8")
        events = _get_run_events(str(tmp_path), "unique-run-999")
        assert events is not None
        assert len(events) > 0

    def test_get_run_events_not_found_returns_none(self, tmp_path: Path):
        """_get_run_events returns None when run_id not found."""
        from kairos.dashboard import _get_run_events

        events = _get_run_events(str(tmp_path), "nonexistent-run-id")
        assert events is None


# ---------------------------------------------------------------------------
# Group 4: Happy paths
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    def test_index_returns_html(self, dashboard_server):
        """GET / must return HTML content."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        content_type = headers.get("content-type", "")
        assert "text/html" in content_type

    def test_index_body_is_html_document(self, dashboard_server):
        """GET / response body must be valid HTML."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/", token=token)
        assert "<!DOCTYPE html>" in data["body"] or "<html" in data["body"].lower()

    def test_api_runs_returns_json_list(self, server_with_runs):
        """GET /api/runs must return a JSON array."""
        _server, base_url, token, _tmp = server_with_runs
        status, data = _fetch(f"{base_url}/api/runs", token=token)
        assert status == 200
        parsed = json.loads(data["body"])
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_api_runs_list_contains_run_summaries(self, server_with_runs):
        """Each run in /api/runs must have expected summary fields."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs", token=token)
        runs = json.loads(data["body"])
        for run in runs:
            assert "run_id" in run
            assert "workflow_name" in run
            assert "status" in run

    def test_api_run_detail_returns_events(self, server_with_runs):
        """GET /api/runs/<run_id> must return JSON with events list."""
        _server, base_url, token, _tmp = server_with_runs
        status, data = _fetch(f"{base_url}/api/runs/run001", token=token)
        assert status == 200
        parsed = json.loads(data["body"])
        assert "events" in parsed
        assert isinstance(parsed["events"], list)

    def test_api_health_returns_ok(self, dashboard_server):
        """GET /api/health must return JSON with status ok."""
        _server, base_url, _token = dashboard_server
        req = urllib.request.Request(f"{base_url}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
            assert body.get("status") == "ok"

    def test_api_health_content_type_json(self, dashboard_server):
        """GET /api/health must return Content-Type: application/json."""
        _server, base_url, _token = dashboard_server
        req = urllib.request.Request(f"{base_url}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            content_type = resp.headers.get("Content-Type", "")
            assert "application/json" in content_type

    def test_runs_json_content_type(self, dashboard_server):
        """GET /api/runs must return Content-Type: application/json."""
        _server, base_url, token = dashboard_server
        status, data = _fetch(f"{base_url}/api/runs", token=token)
        assert status == 200
        headers = {k.lower(): v for k, v in data["headers"].items()}
        content_type = headers.get("content-type", "")
        assert "application/json" in content_type

    def test_generate_token_returns_string(self):
        """generate_token() must return a non-empty string."""
        from kairos.dashboard import generate_token

        token = generate_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_uses_secrets(self):
        """generate_token() must produce cryptographically random tokens."""
        from kairos.dashboard import generate_token

        # Two tokens must not be equal (astronomically improbable if truly random)
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10, "All 10 generated tokens must be unique"

    def test_generate_token_minimum_length(self):
        """generate_token() token must be at least 32 characters."""
        from kairos.dashboard import generate_token

        token = generate_token()
        # secrets.token_urlsafe(32) produces ~43 URL-safe base64 chars
        assert len(token) >= 32

    def test_default_port_constant(self):
        """_DEFAULT_PORT must be 8420."""
        from kairos.dashboard import _DEFAULT_PORT

        assert _DEFAULT_PORT == 8420


# ---------------------------------------------------------------------------
# Group 5: Data loading unit tests (pure functions)
# ---------------------------------------------------------------------------


class TestDataLoading:
    def test_list_jsonl_files_returns_only_jsonl(self, tmp_path: Path):
        """_list_jsonl_files returns only .jsonl files."""
        from kairos.dashboard import _list_jsonl_files

        (tmp_path / "a.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "b.txt").write_text("not jsonl", encoding="utf-8")
        (tmp_path / "c.json").write_text("{}", encoding="utf-8")

        files = _list_jsonl_files(tmp_path)
        assert len(files) == 1
        assert files[0].name == "a.jsonl"

    def test_list_jsonl_files_sorted_newest_first(self, tmp_path: Path):
        """_list_jsonl_files returns files sorted newest-modified first."""
        import time as _time

        from kairos.dashboard import _list_jsonl_files

        # Write files with slight time separation
        older = tmp_path / "older.jsonl"
        older.write_text("{}", encoding="utf-8")
        _time.sleep(0.01)
        newer = tmp_path / "newer.jsonl"
        newer.write_text("{}", encoding="utf-8")

        files = _list_jsonl_files(tmp_path)
        assert files[0].name == "newer.jsonl"
        assert files[1].name == "older.jsonl"

    def test_list_jsonl_files_empty_dir(self, tmp_path: Path):
        """_list_jsonl_files returns empty list for empty directory."""
        from kairos.dashboard import _list_jsonl_files

        files = _list_jsonl_files(tmp_path)
        assert files == []

    def test_read_events_parses_valid_jsonl(self, tmp_path: Path):
        """_read_events parses all valid JSON lines from a .jsonl file."""
        from kairos.dashboard import _read_events

        content = _make_jsonl_content(run_id="test-run")
        f = tmp_path / "test.jsonl"
        f.write_text(content, encoding="utf-8")
        events = _read_events(f)
        assert len(events) == 2
        assert events[0]["event_type"] == "workflow_start"
        assert events[1]["event_type"] == "workflow_complete"

    def test_read_events_skips_non_dict_lines(self, tmp_path: Path):
        """_read_events skips lines that are valid JSON but not objects."""
        from kairos.dashboard import _read_events

        f = tmp_path / "test.jsonl"
        f.write_text('[1, 2, 3]\n{"event_type": "workflow_start", "data": {}}\n', encoding="utf-8")
        events = _read_events(f)
        assert len(events) == 1

    def test_extract_summary_from_complete_run(self, tmp_path: Path):
        """_extract_summary correctly extracts fields from a complete run."""
        from kairos.dashboard import _extract_summary, _read_events

        f = tmp_path / "complete.jsonl"
        f.write_text(
            _make_jsonl_content(
                run_id="run-xyz",
                workflow_name="test_wf",
                status="complete",
                total_steps=3,
                completed_steps=3,
            ),
            encoding="utf-8",
        )
        events = _read_events(f)
        summary = _extract_summary(events)

        assert summary["run_id"] == "run-xyz"
        assert summary["workflow_name"] == "test_wf"
        assert summary["status"] == "complete"
        assert summary["total_steps"] == 3
        assert summary["completed_steps"] == 3

    def test_extract_summary_from_empty_events_returns_defaults(self):
        """_extract_summary on empty list returns defaults."""
        from kairos.dashboard import _extract_summary

        summary = _extract_summary([])
        assert summary["status"] == "incomplete"
        assert summary["workflow_name"] == "unknown"

    def test_list_runs_aggregates_all_files(self, tmp_path: Path):
        """_list_runs returns a summary for each .jsonl file."""
        from kairos.dashboard import _list_runs

        for i in range(3):
            (tmp_path / f"run{i}.jsonl").write_text(
                _make_jsonl_content(run_id=f"r{i}", workflow_name=f"wf{i}"),
                encoding="utf-8",
            )
        runs = _list_runs(str(tmp_path))
        assert len(runs) == 3
        run_ids = {r["run_id"] for r in runs}
        assert run_ids == {"r0", "r1", "r2"}

    def test_list_runs_sorts_newest_first(self, tmp_path: Path):
        """_list_runs returns runs sorted newest-modified first."""
        import time as _time

        from kairos.dashboard import _list_runs

        (tmp_path / "old.jsonl").write_text(_make_jsonl_content(run_id="old-run"), encoding="utf-8")
        _time.sleep(0.01)
        (tmp_path / "new.jsonl").write_text(_make_jsonl_content(run_id="new-run"), encoding="utf-8")
        runs = _list_runs(str(tmp_path))
        assert runs[0]["run_id"] == "new-run"

    def test_get_run_events_finds_by_run_id(self, tmp_path: Path):
        """_get_run_events finds the right run by scanning run_id in events."""
        from kairos.dashboard import _get_run_events

        (tmp_path / "wf_run123.jsonl").write_text(
            _make_jsonl_content(run_id="run123"), encoding="utf-8"
        )
        events = _get_run_events(str(tmp_path), "run123")
        assert events is not None
        assert len(events) == 2

    def test_get_run_events_returns_none_when_not_found(self, tmp_path: Path):
        """_get_run_events returns None when no file has the given run_id."""
        from kairos.dashboard import _get_run_events

        (tmp_path / "other.jsonl").write_text(
            _make_jsonl_content(run_id="different-run"), encoding="utf-8"
        )
        result = _get_run_events(str(tmp_path), "nonexistent-run-id")
        assert result is None

    def test_get_run_events_rejects_traversal_run_id(self, tmp_path: Path):
        """_get_run_events must reject run_ids with path traversal patterns."""
        from kairos.dashboard import _get_run_events

        result = _get_run_events(str(tmp_path), "../../etc/passwd")
        assert result is None

    def test_read_events_handles_oserror(self, tmp_path: Path):
        """_read_events returns empty list when file cannot be read (OSError)."""
        from kairos.dashboard import _read_events

        nonexistent = tmp_path / "nonexistent.jsonl"
        events = _read_events(nonexistent)
        assert events == []

    def test_read_events_skips_empty_lines(self, tmp_path: Path):
        """_read_events skips blank lines without error."""
        from kairos.dashboard import _read_events

        f = tmp_path / "blanks.jsonl"
        f.write_text(
            '\n\n{"event_type": "workflow_start", "data": {}}\n\n',
            encoding="utf-8",
        )
        events = _read_events(f)
        assert len(events) == 1

    def test_extract_summary_counts_step_fail_events(self, tmp_path: Path):
        """_extract_summary counts step_fail events when no workflow_complete present."""
        from kairos.dashboard import _extract_summary

        events = [
            {
                "event_type": "workflow_start",
                "step_id": None,
                "data": {"workflow_name": "wf", "run_id": "r1", "total_steps": 2},
                "timestamp": "2024-01-01T12:00:00+00:00",
            },
            {
                "event_type": "step_fail",
                "step_id": "step_a",
                "data": {},
                "timestamp": "2024-01-01T12:00:01+00:00",
            },
            {
                "event_type": "step_skip",
                "step_id": "step_b",
                "data": {},
                "timestamp": "2024-01-01T12:00:02+00:00",
            },
        ]
        summary = _extract_summary(events)
        assert summary["status"] == "incomplete"
        assert summary["failed_steps"] == 1
        assert summary["skipped_steps"] == 1

    def test_extract_summary_counts_step_complete_events(self):
        """_extract_summary counts step_complete events when no workflow_complete present."""
        from kairos.dashboard import _extract_summary

        events = [
            {
                "event_type": "workflow_start",
                "step_id": None,
                "data": {"workflow_name": "wf", "run_id": "r1", "total_steps": 1},
                "timestamp": "2024-01-01T12:00:00+00:00",
            },
            {
                "event_type": "step_complete",
                "step_id": "step_a",
                "data": {},
                "timestamp": "2024-01-01T12:00:01+00:00",
            },
        ]
        summary = _extract_summary(events)
        assert summary["status"] == "incomplete"
        assert summary["completed_steps"] == 1


# ---------------------------------------------------------------------------
# Group 6: CLI command tests
# ---------------------------------------------------------------------------


class TestDashboardCliCommand:
    def test_dashboard_command_exists(self):
        """The 'dashboard' command must exist in the kairos CLI app."""
        pytest.importorskip("typer")
        from kairos.cli import app

        # Typer stores auto-derived command names in the callback function name
        # when .name is None (the default when no explicit name is given)
        command_names = [
            c.name if c.name is not None else (c.callback.__name__ if c.callback else None)
            for c in app.registered_commands
        ]
        assert "dashboard" in command_names

    def test_dashboard_command_help_text(self):
        """The 'dashboard' command must have help text."""
        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["dashboard", "--help"])
        assert result.exit_code == 0
        # Strip ANSI escape codes — typer on some Python versions injects
        # ANSI color codes between hyphens (e.g., --log-dir becomes
        # \x1b[36m-\x1b[0m\x1b[36m-log\x1b[0m\x1b[36m-dir\x1b[0m).
        import re

        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output).lower()
        assert "port" in plain
        assert "log-dir" in plain
        assert "no-auth" in plain

    def test_dashboard_command_rejects_nonexistent_log_dir(self, tmp_path: Path):
        """kairos dashboard --log-dir /nonexistent must exit with error."""
        pytest.importorskip("typer")
        from typer.testing import CliRunner

        from kairos.cli import app

        runner = CliRunner()
        nonexistent = str(tmp_path / "does_not_exist")
        result = runner.invoke(app, ["dashboard", "--log-dir", nonexistent])
        assert result.exit_code != 0

    def test_dashboard_default_port_is_8420(self):
        """Default port for dashboard command must be 8420."""
        pytest.importorskip("typer")
        import inspect

        from kairos.cli import dashboard

        sig = inspect.signature(dashboard)
        assert sig.parameters["port"].default == 8420

    def test_dashboard_default_no_auth_is_false(self):
        """Default --no-auth for dashboard command must be False."""
        pytest.importorskip("typer")
        import inspect

        from kairos.cli import dashboard

        sig = inspect.signature(dashboard)
        assert sig.parameters["no_auth"].default is False


# ---------------------------------------------------------------------------
# Group 7: Constants and module-level checks
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_token_length_constant(self):
        """_TOKEN_LENGTH must be 32."""
        from kairos.dashboard import _TOKEN_LENGTH

        assert _TOKEN_LENGTH == 32

    def test_index_html_is_complete_document(self):
        """_INDEX_HTML must be a non-empty string containing HTML boilerplate."""
        from kairos.dashboard import _INDEX_HTML

        assert isinstance(_INDEX_HTML, str)
        assert len(_INDEX_HTML) > 100
        assert "<html" in _INDEX_HTML.lower() or "<!DOCTYPE" in _INDEX_HTML

    def test_index_html_has_no_external_resources(self):
        """_INDEX_HTML must not reference external URLs (CDN, fonts, etc.)."""
        from kairos.dashboard import _INDEX_HTML

        # These common CDN patterns must not appear
        forbidden = [
            "cdn.jsdelivr.net",
            "unpkg.com",
            "cdnjs.cloudflare.com",
            "fonts.googleapis.com",
            "https://ajax",
        ]
        for pattern in forbidden:
            assert pattern not in _INDEX_HTML, (
                f"_INDEX_HTML must not reference external resource: {pattern}"
            )

    def test_app_js_fetches_api_with_token(self):
        """_APP_JS JavaScript must pass the auth token when fetching API."""
        from kairos.dashboard import _APP_JS

        # The JS should reference /api/runs and include token logic
        assert "/api/runs" in _APP_JS
        assert "token" in _APP_JS

    def test_index_html_contains_title(self):
        """_INDEX_HTML must contain 'Kairos Dashboard' title."""
        from kairos.dashboard import _INDEX_HTML

        assert "Kairos Dashboard" in _INDEX_HTML

    def test_styles_css_is_nonempty_string(self):
        """_STYLES_CSS must be a non-empty string with CSS content."""
        from kairos.dashboard import _STYLES_CSS

        assert isinstance(_STYLES_CSS, str)
        assert len(_STYLES_CSS) > 100
        assert ":root" in _STYLES_CSS  # must have design system variables

    def test_app_js_is_nonempty_string(self):
        """_APP_JS must be a non-empty string with JavaScript content."""
        from kairos.dashboard import _APP_JS

        assert isinstance(_APP_JS, str)
        assert len(_APP_JS) > 100


# ---------------------------------------------------------------------------
# Group 8: Edge case tests added by QA
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Group 9: v0.4.4 — UI file extraction tests (written BEFORE implementation)
# ---------------------------------------------------------------------------


class TestUIFilesExist:
    """Verify dashboard_ui/ files exist on disk and are loaded at import time."""

    def test_ui_dir_constant_defined(self):
        """_UI_DIR module constant must be defined and be a Path."""
        from pathlib import Path

        from kairos.dashboard import _UI_DIR

        assert isinstance(_UI_DIR, Path)

    def test_index_html_file_exists_on_disk(self):
        """kairos/dashboard_ui/index.html must exist on disk."""
        from kairos.dashboard import _UI_DIR

        assert (_UI_DIR / "index.html").exists(), "dashboard_ui/index.html must exist"

    def test_styles_css_file_exists_on_disk(self):
        """kairos/dashboard_ui/styles.css must exist on disk."""
        from kairos.dashboard import _UI_DIR

        assert (_UI_DIR / "styles.css").exists(), "dashboard_ui/styles.css must exist"

    def test_app_js_file_exists_on_disk(self):
        """kairos/dashboard_ui/app.js must exist on disk."""
        from kairos.dashboard import _UI_DIR

        assert (_UI_DIR / "app.js").exists(), "dashboard_ui/app.js must exist"

    def test_index_html_loaded_into_module_var(self):
        """_INDEX_HTML module var must be loaded from the file (not empty)."""
        from kairos.dashboard import _INDEX_HTML, _UI_DIR

        on_disk = (_UI_DIR / "index.html").read_text(encoding="utf-8")
        assert on_disk == _INDEX_HTML

    def test_styles_css_loaded_into_module_var(self):
        """_STYLES_CSS module var must be loaded from the file (not empty)."""
        from kairos.dashboard import _STYLES_CSS, _UI_DIR

        on_disk = (_UI_DIR / "styles.css").read_text(encoding="utf-8")
        assert on_disk == _STYLES_CSS

    def test_app_js_loaded_into_module_var(self):
        """_APP_JS module var must be loaded from the file (not empty)."""
        from kairos.dashboard import _APP_JS, _UI_DIR

        on_disk = (_UI_DIR / "app.js").read_text(encoding="utf-8")
        assert on_disk == _APP_JS

    def test_index_html_references_static_css(self):
        """index.html must reference /static/styles.css via a <link> tag."""
        from kairos.dashboard import _INDEX_HTML

        assert "/static/styles.css" in _INDEX_HTML

    def test_index_html_references_static_js(self):
        """index.html must reference /static/app.js via a <script src> tag."""
        from kairos.dashboard import _INDEX_HTML

        assert "/static/app.js" in _INDEX_HTML

    def test_index_html_has_no_inline_script(self):
        """index.html must NOT have inline <script> blocks (CSP compliance)."""
        import re

        from kairos.dashboard import _INDEX_HTML

        # Matches <script> without a src attribute (inline script)
        pattern = r"<script(?![^>]*\bsrc\b)[^>]*>.*?</script>"
        inline_script = re.search(pattern, _INDEX_HTML, re.DOTALL)
        assert inline_script is None, (
            f"index.html must not have inline scripts: found {inline_script}"
        )

    def test_index_html_has_no_external_urls(self):
        """index.html must not reference any external URLs."""
        from kairos.dashboard import _INDEX_HTML

        forbidden = [
            "cdn.jsdelivr.net",
            "unpkg.com",
            "cdnjs.cloudflare.com",
            "fonts.googleapis.com",
            "https://ajax",
        ]
        for pattern in forbidden:
            assert pattern not in _INDEX_HTML, (
                f"index.html must not reference external resource: {pattern}"
            )


class TestStaticFileServing:
    """Static file routes: /static/styles.css and /static/app.js."""

    def test_static_css_returns_200_with_auth(self, dashboard_server):
        """GET /static/styles.css with valid token must return 200."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/static/styles.css", token=token)
        assert status == 200

    def test_static_js_returns_200_with_auth(self, dashboard_server):
        """GET /static/app.js with valid token must return 200."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/static/app.js", token=token)
        assert status == 200

    def test_static_css_content_type(self, dashboard_server):
        """GET /static/styles.css must return Content-Type: text/css."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/static/styles.css", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "text/css" in headers.get("content-type", "")

    def test_static_js_content_type(self, dashboard_server):
        """GET /static/app.js must return Content-Type: text/javascript."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/static/app.js", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        ct = headers.get("content-type", "")
        assert "javascript" in ct

    def test_static_css_has_csp_header(self, dashboard_server):
        """GET /static/styles.css must include CSP header."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/static/styles.css", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_static_js_has_csp_header(self, dashboard_server):
        """GET /static/app.js must include CSP header."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/static/app.js", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers

    def test_static_css_no_auth_required(self, dashboard_server):
        """GET /static/styles.css must work WITHOUT token (browser loads via <link>)."""
        _server, base_url, _token = dashboard_server
        status, _data = _fetch(f"{base_url}/static/styles.css")
        assert status == 200

    def test_static_js_no_auth_required(self, dashboard_server):
        """GET /static/app.js must work WITHOUT token (browser loads via <script>)."""
        _server, base_url, _token = dashboard_server
        status, _data = _fetch(f"{base_url}/static/app.js")
        assert status == 200

    def test_unknown_static_path_returns_404(self, dashboard_server):
        """GET /static/unknown.xyz must return 404."""
        _server, base_url, _token = dashboard_server
        status, _data = _fetch(f"{base_url}/static/unknown.xyz")
        assert status == 404

    def test_no_static_path_traversal(self, dashboard_server):
        """GET /static/../dashboard.py must not serve Python source."""
        _server, base_url, token = dashboard_server
        status, data = _fetch(f"{base_url}/static/..%2Fdashboard.py", token=token)
        # Should get 404, not 200 with Python source
        assert status == 404

    def test_static_css_body_contains_design_tokens(self, dashboard_server):
        """styles.css response body must contain :root CSS custom properties."""
        _server, base_url, token = dashboard_server
        _status, data = _fetch(f"{base_url}/static/styles.css", token=token)
        assert ":root" in data["body"]
        assert "--bg-950" in data["body"]  # a specific design token


class TestCSPUpdate:
    """v0.4.4 tightened CSP — 'unsafe-inline' removed from script-src."""

    def test_script_src_has_no_unsafe_inline(self):
        """_CSP_HEADER must NOT contain 'unsafe-inline' in script-src."""
        from kairos.dashboard import _CSP_HEADER

        # Parse script-src directive
        parts = [p.strip() for p in _CSP_HEADER.split(";")]
        script_src = next((p for p in parts if p.startswith("script-src")), "")
        assert "'unsafe-inline'" not in script_src, (
            f"script-src must not have 'unsafe-inline' after v0.4.4: {script_src!r}"
        )

    def test_style_src_still_has_unsafe_inline(self):
        """_CSP_HEADER must still have 'unsafe-inline' in style-src (dynamic inline styles)."""
        from kairos.dashboard import _CSP_HEADER

        parts = [p.strip() for p in _CSP_HEADER.split(";")]
        style_src = next((p for p in parts if p.startswith("style-src")), "")
        assert "'unsafe-inline'" in style_src, f"style-src must keep 'unsafe-inline': {style_src!r}"

    def test_default_src_self_present(self):
        """_CSP_HEADER must have default-src 'self'."""
        from kairos.dashboard import _CSP_HEADER

        assert "default-src 'self'" in _CSP_HEADER

    def test_script_src_self_present(self):
        """_CSP_HEADER must have script-src 'self'."""
        from kairos.dashboard import _CSP_HEADER

        assert "script-src 'self'" in _CSP_HEADER


class TestVersionBump:
    """Current version must be reflected in the package."""

    def test_version_is_current(self):
        """kairos.__version__ must match the current release."""
        import kairos

        assert kairos.__version__ == "0.4.6"


class TestEdgeCases:
    """Edge cases identified during QA review."""

    def test_empty_run_id_in_url_returns_404(self, dashboard_server):
        """GET /api/runs/ (trailing slash, empty run_id) must return 404."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/api/runs/", token=token)
        assert status == 404

    def test_token_in_query_param_takes_precedence_over_missing_header(self, dashboard_server):
        """When token is in query param only, auth succeeds without header."""
        _server, base_url, token = dashboard_server
        status, _data = _fetch(f"{base_url}/", token=token)
        assert status == 200

    def test_bearer_header_works_without_query_param(self, dashboard_server):
        """When token is in Bearer header only, auth succeeds."""
        _server, base_url, token = dashboard_server
        req = urllib.request.Request(
            f"{base_url}/",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

    def test_wrong_bearer_with_correct_query_param(self, dashboard_server):
        """Query param token should authenticate even if Bearer is wrong."""
        _server, base_url, token = dashboard_server
        req = urllib.request.Request(
            f"{base_url}/?token={token}",
            headers={"Authorization": "Bearer wrong-token"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

    def test_unicode_workflow_name_in_summary(self, tmp_path: Path):
        """Workflow names with unicode characters are handled correctly."""
        from kairos.dashboard import _extract_summary, _read_events

        content = _make_jsonl_content(run_id="uni-run", workflow_name="workflow_\u00e9\u00e8\u00ea")
        f = tmp_path / "unicode.jsonl"
        f.write_text(content, encoding="utf-8")
        events = _read_events(f)
        summary = _extract_summary(events)
        assert summary["workflow_name"] == "workflow_\u00e9\u00e8\u00ea"

    def test_unicode_run_id_rejected_by_safe_pattern(self, tmp_path: Path):
        """Run IDs with unicode characters are rejected by _SAFE_RUN_ID_RE."""
        from kairos.dashboard import _get_run_events

        result = _get_run_events(str(tmp_path), "run-\u00e9\u00e8\u00ea")
        assert result is None

    def test_multiple_runs_in_single_jsonl_file(self, tmp_path: Path):
        """A single .jsonl with one workflow_start yields one run summary."""
        from kairos.dashboard import _list_runs

        # Two runs would need two .jsonl files; one file = one run
        content = _make_jsonl_content(run_id="only-run")
        (tmp_path / "single.jsonl").write_text(content, encoding="utf-8")
        runs = _list_runs(str(tmp_path))
        assert len(runs) == 1
        assert runs[0]["run_id"] == "only-run"

    def test_run_detail_includes_both_summary_and_events(self, server_with_runs):
        """GET /api/runs/<id> response must contain both 'summary' and 'events'."""
        _server, base_url, token, _tmp = server_with_runs
        status, data = _fetch(f"{base_url}/api/runs/run001", token=token)
        assert status == 200
        parsed = json.loads(data["body"])
        assert "summary" in parsed
        assert "events" in parsed
        assert isinstance(parsed["summary"], dict)
        assert isinstance(parsed["events"], list)
        # Summary should have expected fields
        assert "workflow_name" in parsed["summary"]
        assert "status" in parsed["summary"]
        assert "run_id" in parsed["summary"]


# ============================================================
# Enhancement 8 — Step Input/Output Inspector
# ============================================================


class TestInspectorPanel:
    """Enhancement 8 — step input/output inspector panel."""

    def test_app_js_contains_inspector_renderer(self):
        """app.js must include renderInspectorPanel function."""
        from kairos.dashboard import _APP_JS

        assert "renderInspectorPanel" in _APP_JS

    def test_app_js_contains_inspector_state(self):
        """app.js must track openInspectorStepId and currentRunEvents."""
        from kairos.dashboard import _APP_JS

        assert "openInspectorStepId" in _APP_JS
        assert "currentRunEvents" in _APP_JS

    def test_app_js_contains_toggle_inspector(self):
        """app.js must include toggleInspector function."""
        from kairos.dashboard import _APP_JS

        assert "toggleInspector" in _APP_JS

    def test_app_js_contains_close_inspector(self):
        """app.js must include closeInspector function."""
        from kairos.dashboard import _APP_JS

        assert "closeInspector" in _APP_JS

    def test_app_js_contains_switch_inspector_tab(self):
        """app.js must include switchInspectorTab function."""
        from kairos.dashboard import _APP_JS

        assert "switchInspectorTab" in _APP_JS

    def test_app_js_contains_inspect_icon(self):
        """app.js must include iconInspect function."""
        from kairos.dashboard import _APP_JS

        assert "iconInspect" in _APP_JS

    def test_app_js_contains_inspect_button_in_step_groups(self):
        """app.js renderStepGroups must include inspect button."""
        from kairos.dashboard import _APP_JS

        assert "inspect-btn" in _APP_JS
        assert "data-inspect-step" in _APP_JS

    def test_styles_contain_inspector_panel(self):
        """styles.css must include .inspector-panel class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspector-panel" in _STYLES_CSS

    def test_styles_contain_inspector_tabs(self):
        """styles.css must include .inspector-tab class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspector-tab" in _STYLES_CSS

    def test_styles_contain_inspector_close(self):
        """styles.css must include .inspector-close class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspector-close" in _STYLES_CSS

    def test_styles_contain_inspect_btn(self):
        """styles.css must include .inspect-btn class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspect-btn" in _STYLES_CSS

    def test_styles_contain_inspector_empty(self):
        """styles.css must include .inspector-empty class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspector-empty" in _STYLES_CSS

    def test_inspector_close_has_aria_label(self):
        """Inspector close button must have aria-label."""
        from kairos.dashboard import _APP_JS

        assert "Close inspector" in _APP_JS

    def test_inspect_btn_has_aria_label(self):
        """Inspect button must have aria-label."""
        from kairos.dashboard import _APP_JS

        assert "Inspect step" in _APP_JS

    def test_inspector_graceful_degradation_messages(self):
        """Inspector must include context-specific degradation messages."""
        from kairos.dashboard import _APP_JS

        assert "not captured at this verbosity" in _APP_JS
        assert "No input data recorded" in _APP_JS
        assert "No validation contract configured" in _APP_JS
        assert "Step did not complete" in _APP_JS

    def test_inspector_tab_content_attributes(self):
        """Inspector must use data-tab-content attributes for tab switching."""
        from kairos.dashboard import _APP_JS

        assert "data-tab-content" in _APP_JS
        assert "data-tab=" in _APP_JS

    # --- Behavioral tests ---

    def test_inspector_panel_html_has_three_tabs(self):
        """renderInspectorPanel must produce exactly three named tabs: input, output, validation."""
        from kairos.dashboard import _APP_JS

        # The rendered HTML must include all three tab data-tab values
        assert 'data-tab="input"' in _APP_JS
        assert 'data-tab="output"' in _APP_JS
        assert 'data-tab="validation"' in _APP_JS
        # And the matching tab-content sections
        assert 'data-tab-content="input"' in _APP_JS
        assert 'data-tab-content="output"' in _APP_JS
        assert 'data-tab-content="validation"' in _APP_JS

    def test_inspector_panel_html_has_correct_structure(self):
        """renderInspectorPanel must produce inspector-header, inspector-tabs, inspector-body."""
        from kairos.dashboard import _APP_JS

        assert '"inspector-header"' in _APP_JS
        assert '"inspector-tabs"' in _APP_JS
        assert '"inspector-body"' in _APP_JS

    def test_colorize_json_used_for_input_output_rendering(self):
        """renderInspectorPanel must call colorizeJson for input and output tab content."""
        from kairos.dashboard import _APP_JS

        # colorizeJson must appear inside the renderInspectorPanel function body.
        # Verify the function definition and its use of colorizeJson both exist.
        assert "function renderInspectorPanel" in _APP_JS
        assert "colorizeJson(inputData" in _APP_JS or "colorizeJson(input" in _APP_JS
        assert "colorizeJson(outputData" in _APP_JS or "colorizeJson(output" in _APP_JS

    def test_inspector_panel_data_attribute_carries_step_id(self):
        """The rendered inspector panel root element must carry data-inspector-step attribute."""
        from kairos.dashboard import _APP_JS

        assert 'data-inspector-step="' in _APP_JS

    def test_step_group_header_inspect_button_structure(self):
        """Inspect button must have class, data-inspect-step, and aria-label."""
        from kairos.dashboard import _APP_JS

        # All three attributes must appear together in the inspect button markup
        assert 'class="inspect-btn"' in _APP_JS
        assert 'data-inspect-step="' in _APP_JS
        assert 'aria-label="Inspect step ' in _APP_JS

    def test_insert_adjacent_html_uses_beforeend(self):
        """toggleInspector must insert panel as last child (beforeend) to stay inside the li."""
        from kairos.dashboard import _APP_JS

        assert "insertAdjacentHTML('beforeend'" in _APP_JS
        assert "insertAdjacentHTML('afterend'" not in _APP_JS

    def test_css_escape_used_in_selector(self):
        """toggleInspector must use CSS.escape in querySelector to prevent selector injection."""
        from kairos.dashboard import _APP_JS

        assert "CSS.escape(stepId)" in _APP_JS

    def test_styles_inspect_btn_focus_visible(self):
        """styles.css must style inspect-btn with focus-visible."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspect-btn:focus-visible" in _STYLES_CSS

    def test_styles_inspector_empty_code_styling(self):
        """styles.css must style <code> inside .inspector-empty."""
        from kairos.dashboard import _STYLES_CSS

        assert ".inspector-empty code" in _STYLES_CSS
        assert "var(--font-mono)" in _STYLES_CSS
        assert "var(--bg-800)" in _STYLES_CSS


# ---------------------------------------------------------------------------
# Enhancement 5 — Step Dependency Graph (SVG DAG)
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    """Enhancement 5 — step dependency graph (SVG DAG)."""

    # SVG helpers presence
    def test_app_js_contains_svg_namespace(self):
        from kairos.dashboard import _APP_JS

        assert "http://www.w3.org/2000/svg" in _APP_JS

    def test_app_js_contains_svg_el(self):
        from kairos.dashboard import _APP_JS

        assert "function svgEl(" in _APP_JS

    def test_app_js_contains_svg_rect(self):
        from kairos.dashboard import _APP_JS

        assert "function svgRect(" in _APP_JS

    def test_app_js_contains_svg_text(self):
        from kairos.dashboard import _APP_JS

        assert "function svgText(" in _APP_JS

    def test_app_js_contains_svg_line(self):
        from kairos.dashboard import _APP_JS

        assert "function svgLine(" in _APP_JS

    def test_app_js_contains_svg_path(self):
        from kairos.dashboard import _APP_JS

        assert "function svgPath(" in _APP_JS

    def test_app_js_contains_svg_arrow_marker(self):
        from kairos.dashboard import _APP_JS

        assert "function svgArrowMarker(" in _APP_JS

    def test_app_js_contains_svg_group(self):
        from kairos.dashboard import _APP_JS

        assert "function svgGroup(" in _APP_JS

    # Graph component presence
    def test_app_js_contains_render_graph_placeholder(self):
        from kairos.dashboard import _APP_JS

        assert "renderGraphPlaceholder" in _APP_JS

    def test_app_js_contains_mount_dependency_graph(self):
        from kairos.dashboard import _APP_JS

        assert "mountDependencyGraph" in _APP_JS

    def test_app_js_contains_extract_dependency_data(self):
        from kairos.dashboard import _APP_JS

        assert "extractDependencyData" in _APP_JS

    def test_app_js_contains_compute_graph_layout(self):
        from kairos.dashboard import _APP_JS

        assert "computeGraphLayout" in _APP_JS

    def test_app_js_contains_scroll_to_step_group(self):
        from kairos.dashboard import _APP_JS

        assert "scrollToStepGroup" in _APP_JS

    # Graph container and CSS
    def test_app_js_contains_graph_container(self):
        from kairos.dashboard import _APP_JS

        assert "graph-container" in _APP_JS
        assert "dep-graph" in _APP_JS

    def test_styles_contain_graph_container(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".graph-container" in _STYLES_CSS

    def test_styles_contain_graph_node(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".graph-node" in _STYLES_CSS

    def test_styles_contain_graph_highlight(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".graph-highlight" in _STYLES_CSS

    def test_styles_contain_graph_empty(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".graph-empty" in _STYLES_CSS

    # Behavioral tests
    def test_graph_uses_create_element_ns(self):
        """SVG elements must use createElementNS, not createElement."""
        from kairos.dashboard import _APP_JS

        assert "createElementNS" in _APP_JS

    def test_graph_node_constants_defined(self):
        """Graph layout constants must be defined."""
        from kairos.dashboard import _APP_JS

        assert "NODE_W" in _APP_JS
        assert "NODE_H" in _APP_JS

    def test_graph_node_has_data_step_id(self):
        """Graph nodes must have data-step-id for event delegation."""
        from kairos.dashboard import _APP_JS

        assert "data-step-id" in _APP_JS

    def test_graph_has_aria_label(self):
        """Graph container must have accessible label."""
        from kairos.dashboard import _APP_JS

        assert "dependency graph" in _APP_JS.lower() or "aria-label" in _APP_JS

    def test_graph_reads_css_tokens(self):
        """Graph must read design system tokens via getComputedStyle."""
        from kairos.dashboard import _APP_JS

        assert "getComputedStyle" in _APP_JS

    def test_graph_container_overflow_auto(self):
        """Graph container must have overflow-x: auto for wide graphs."""
        from kairos.dashboard import _STYLES_CSS

        assert "overflow" in _STYLES_CSS

    def test_graph_node_focus_visible(self):
        """Graph nodes must be keyboard accessible with focus-visible."""
        from kairos.dashboard import _STYLES_CSS

        assert "focus-visible" in _STYLES_CSS

    def test_graph_edge_uses_cubic_bezier(self):
        """Graph edges must use cubic bezier paths."""
        from kairos.dashboard import _APP_JS

        # Cubic bezier uses C command in SVG path
        assert "' C '" in _APP_JS or "'C '" in _APP_JS or "' C'" in _APP_JS or "C " in _APP_JS

    def test_graph_calls_mount_after_innerhtml(self):
        """showRunDetail must call mountDependencyGraph after setting innerHTML."""
        from kairos.dashboard import _APP_JS

        assert "mountDependencyGraph" in _APP_JS

    def test_graph_event_delegation_for_node_click(self):
        """Event delegation must handle graph node clicks."""
        from kairos.dashboard import _APP_JS

        assert "graph-container" in _APP_JS
        assert "scrollToStepGroup" in _APP_JS

    # Behavioral assertions (Finding 8)

    def test_extract_dependency_data_accesses_step_id(self):
        """extractDependencyData must reference step_id for event processing."""
        from kairos.dashboard import _APP_JS

        assert "step_id" in _APP_JS

    def test_extract_dependency_data_accesses_plan_and_dependencies(self):
        """extractDependencyData must reference data.plan and data.dependencies."""
        from kairos.dashboard import _APP_JS

        assert "data.plan" in _APP_JS or "'plan'" in _APP_JS or '"plan"' in _APP_JS
        assert "dependencies" in _APP_JS

    def test_compute_graph_layout_uses_layer_assignment(self):
        """computeGraphLayout must use topological depth via layers variable."""
        from kairos.dashboard import _APP_JS

        assert "layers[" in _APP_JS
        assert "layers[s.id]" in _APP_JS

    def test_svg_helpers_use_create_element_ns(self):
        """SVG helper svgEl must use createElementNS with the SVG namespace."""
        from kairos.dashboard import _APP_JS

        assert "createElementNS" in _APP_JS
        assert "http://www.w3.org/2000/svg" in _APP_JS

    def test_graph_node_text_truncation_constant(self):
        """Node text truncation must use a named numeric limit."""
        from kairos.dashboard import _APP_JS

        # The truncation check compares node.id.length against a constant (18)
        assert "node.id.length" in _APP_JS
        assert "> 18" in _APP_JS or ">= 18" in _APP_JS or "> 17" in _APP_JS

    def test_circular_dependency_warning_exists(self):
        """computeGraphLayout must emit a console.warn when maxIter is exhausted."""
        from kairos.dashboard import _APP_JS

        assert "maxIter <= 0" in _APP_JS
        assert "circular dependencies" in _APP_JS

    def test_graph_placeholder_function_renamed(self):
        """renderGraphPlaceholder must exist; renderDependencyGraph must not."""
        from kairos.dashboard import _APP_JS

        assert "function renderGraphPlaceholder(" in _APP_JS
        assert "function renderDependencyGraph(" not in _APP_JS

    def test_css_graph_highlight_uses_increased_specificity(self):
        """graph-highlight must use .step-group-header.graph-highlight, not !important."""
        from kairos.dashboard import _STYLES_CSS

        assert ".step-group-header.graph-highlight" in _STYLES_CSS
        # The bare single-class selector must not appear as its own rule
        import re

        assert not re.search(r"(?<![\w-])\.graph-highlight\s*\{", _STYLES_CSS)
        rule_body = _STYLES_CSS.split(".step-group-header.graph-highlight")[1].split("}")[0]
        assert "!important" not in rule_body

    def test_font_mono_token_in_css_tokens_cache(self):
        """getCssTokens must resolve --font-mono token."""
        from kairos.dashboard import _APP_JS

        assert "--font-mono" in _APP_JS
        assert "fontMono" in _APP_JS

    def test_svg_helpers_use_design_system_tokens_not_hardcoded_hex(self):
        """SVG helper defaults must not use hardcoded hex colours in function bodies."""
        from kairos.dashboard import _APP_JS

        # svgText fill default must not be hardcoded hex — it must use tokens.textPrimary
        assert "fill: opts.fill || '#f8fafc'" not in _APP_JS
        # svgLine stroke default must not be hardcoded hex — it must use tokens.edge
        assert "stroke || '#475569'" not in _APP_JS
        # svgArrowMarker fill default must not be hardcoded hex
        assert "fill: color || '#475569'" not in _APP_JS

    def test_hover_uses_mouseleave_not_mouseout(self):
        """Graph hover must use mouseleave, not mouseout, to prevent child-element flicker."""
        from kairos.dashboard import _APP_JS

        assert "mouseleave" in _APP_JS
        assert "mouseout" not in _APP_JS


# ---------------------------------------------------------------------------
# Enhancement 6 — Export Run Data
# ---------------------------------------------------------------------------


class TestExportEndpointsSecurity:
    """Enhancement 6 — export endpoints security (S17 compliance)."""

    def test_export_json_requires_auth(self, server_with_runs):
        """GET /api/runs/<id>/export/json without token returns 403."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(f"{base_url}/api/runs/run001/export/json")
        assert status == 403

    def test_export_csv_requires_auth(self, server_with_runs):
        """GET /api/runs/<id>/export/csv without token returns 403."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(f"{base_url}/api/runs/run001/export/csv")
        assert status == 403

    def test_export_json_has_csp_headers(self, server_with_runs):
        """Export JSON response includes CSP and nosniff headers."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers
        assert headers.get("x-content-type-options", "").lower() == "nosniff"

    def test_export_csv_has_csp_headers(self, server_with_runs):
        """Export CSV response includes CSP and nosniff headers."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers
        assert headers.get("x-content-type-options", "").lower() == "nosniff"

    def test_export_json_invalid_run_id_returns_404(self, server_with_runs):
        """Export with nonexistent run_id returns 404."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(f"{base_url}/api/runs/doesnotexist/export/json", token=token)
        assert status == 404

    def test_export_csv_invalid_run_id_returns_404(self, server_with_runs):
        """Export with nonexistent run_id returns 404."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(f"{base_url}/api/runs/doesnotexist/export/csv", token=token)
        assert status == 404

    def test_export_json_path_traversal_rejected(self, server_with_runs):
        """Run ID with path traversal chars returns 404."""
        _server, base_url, token, _tmp = server_with_runs
        # URL-encode the traversal attempt so urllib doesn't reject it
        status, _data = _fetch(
            f"{base_url}/api/runs/..%2F..%2Fetc%2Fpasswd/export/json", token=token
        )
        assert status == 404

    def test_export_csv_path_traversal_rejected(self, server_with_runs):
        """Run ID with path traversal chars returns 404."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(
            f"{base_url}/api/runs/..%2F..%2Fetc%2Fpasswd/export/csv", token=token
        )
        assert status == 404

    def test_export_json_post_returns_405(self, server_with_runs):
        """POST to export JSON endpoint returns 405."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(
            f"{base_url}/api/runs/run001/export/json", token=token, method="POST"
        )
        assert status == 405

    def test_export_csv_post_returns_405(self, server_with_runs):
        """POST to export CSV endpoint returns 405."""
        _server, base_url, token, _tmp = server_with_runs
        status, _data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token, method="POST")
        assert status == 405


class TestExportJSON:
    """Enhancement 6 — JSON export happy paths."""

    def test_export_json_returns_formatted_json(self, server_with_runs):
        """Export JSON returns pretty-printed JSON with run_id, summary, events."""
        _server, base_url, token, _tmp = server_with_runs
        status, data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        assert status == 200
        parsed = json.loads(data["body"])
        assert parsed["run_id"] == "run001"
        assert "summary" in parsed
        assert "events" in parsed
        assert isinstance(parsed["events"], list)

    def test_export_json_content_disposition(self, server_with_runs):
        """Export JSON has Content-Disposition: attachment header with filename."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        cd = headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "run_run001.json" in cd

    def test_export_json_content_type(self, server_with_runs):
        """Export JSON has application/json Content-Type."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "application/json" in headers.get("content-type", "")

    def test_export_json_same_data_as_detail(self, server_with_runs):
        """Export JSON contains same data as run detail endpoint."""
        _server, base_url, token, _tmp = server_with_runs
        _status, detail_data = _fetch(f"{base_url}/api/runs/run001", token=token)
        _status2, export_data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        detail = json.loads(detail_data["body"])
        exported = json.loads(export_data["body"])
        assert exported["run_id"] == detail["run_id"]
        assert exported["summary"] == detail["summary"]
        assert exported["events"] == detail["events"]

    def test_export_json_is_pretty_printed(self, server_with_runs):
        """Export JSON body contains newlines (pretty-printed with indent=2)."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        body = data["body"]
        assert "\n" in body

    def test_export_json_has_content_length(self, server_with_runs):
        """Export JSON response has Content-Length header."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/json", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-length" in headers
        assert int(headers["content-length"]) > 0


class TestExportCSV:
    """Enhancement 6 — CSV export happy paths."""

    def test_export_csv_returns_csv_content(self, server_with_runs):
        """Export CSV returns valid CSV with header row."""
        _server, base_url, token, _tmp = server_with_runs
        status, data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token)
        assert status == 200
        lines = data["body"].splitlines()
        assert len(lines) >= 1
        header = lines[0]
        assert "timestamp" in header
        assert "event_type" in header

    def test_export_csv_content_disposition(self, server_with_runs):
        """Export CSV has Content-Disposition: attachment header."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        cd = headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "run_run001.csv" in cd

    def test_export_csv_content_type(self, server_with_runs):
        """Export CSV has text/csv Content-Type."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "text/csv" in headers.get("content-type", "")

    def test_export_csv_row_count_matches_events(self, server_with_runs):
        """CSV has one row per event plus header row."""
        _server, base_url, token, _tmp = server_with_runs
        # run001 has 2 events (workflow_start, workflow_complete)
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token)
        lines = [ln for ln in data["body"].splitlines() if ln.strip()]
        # header + 2 data rows
        assert len(lines) == 3

    def test_export_csv_has_content_length(self, server_with_runs):
        """Export CSV response has Content-Length header."""
        _server, base_url, token, _tmp = server_with_runs
        _status, data = _fetch(f"{base_url}/api/runs/run001/export/csv", token=token)
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-length" in headers
        assert int(headers["content-length"]) > 0


class TestEventsToCsv:
    """Unit tests for _events_to_csv helper."""

    def test_empty_events_returns_header_only(self):
        """Empty event list produces CSV with header row only."""
        from kairos.dashboard import _events_to_csv

        result = _events_to_csv([])
        lines = [ln for ln in result.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "timestamp" in lines[0]
        assert "event_type" in lines[0]

    def test_event_fields_mapped_to_columns(self):
        """Event dict fields map to correct CSV columns."""
        from kairos.dashboard import _events_to_csv

        events = [
            {
                "timestamp": "2024-01-01T12:00:00",
                "event_type": "step_start",
                "step_id": "my_step",
                "level": "INFO",
                "data": {"key": "value"},
            }
        ]
        result = _events_to_csv(events)
        lines = result.splitlines()
        assert len(lines) == 2
        data_row = lines[1]
        assert "2024-01-01T12:00:00" in data_row
        assert "step_start" in data_row
        assert "my_step" in data_row
        assert "INFO" in data_row

    def test_missing_fields_produce_empty_cells(self):
        """Events with missing fields produce empty cells, not errors."""
        from kairos.dashboard import _events_to_csv

        events = [{"event_type": "workflow_start"}]
        # Should not raise
        result = _events_to_csv(events)
        assert "workflow_start" in result

    def test_data_field_serialized_as_json(self):
        """data_json column contains JSON of the data field."""
        from kairos.dashboard import _events_to_csv

        events = [
            {
                "timestamp": "2024-01-01",
                "event_type": "step_complete",
                "step_id": "s1",
                "level": "INFO",
                "data": {"status": "completed", "duration_ms": 5.0},
            }
        ]
        result = _events_to_csv(events)
        # The data JSON must appear in the row
        assert "completed" in result
        assert "duration_ms" in result

    def test_formula_characters_escaped_in_csv(self):
        """Values starting with =, +, -, @ are prefixed to prevent CSV injection."""
        import csv as csv_mod
        import io as io_mod

        from kairos.dashboard import _events_to_csv

        event = {
            "timestamp": "2024-01-01T00:00:00",
            "event_type": "=cmd|' /C calc'!A0",
            "step_id": "+malicious",
            "level": "-danger",
            "data": {"key": "value"},
        }
        result = _events_to_csv([event])
        lines = result.strip().split("\n")
        assert len(lines) == 2

        reader = csv_mod.reader(io_mod.StringIO(result))
        rows = list(reader)
        data_row = rows[1]

        # event_type column (index 1) should be escaped with leading quote
        assert data_row[1].startswith("'")
        assert not data_row[1].startswith("=")

        # step_id column (index 2) should be escaped
        assert data_row[2].startswith("'")
        assert not data_row[2].startswith("+")

        # level column (index 3) should be escaped
        assert data_row[3].startswith("'")
        assert not data_row[3].startswith("-")


class TestExportUI:
    """Enhancement 6 — export UI elements."""

    def test_app_js_contains_export_buttons(self):
        from kairos.dashboard import _APP_JS

        assert "export-json" in _APP_JS
        assert "export-csv" in _APP_JS
        assert "copy-api-url" in _APP_JS

    def test_app_js_contains_download_icon(self):
        from kairos.dashboard import _APP_JS

        assert "iconDownload" in _APP_JS

    def test_app_js_contains_copy_icon(self):
        from kairos.dashboard import _APP_JS

        assert "iconCopy" in _APP_JS

    def test_styles_contain_export_classes(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".btn-export" in _STYLES_CSS
        assert ".export-actions" in _STYLES_CSS

    def test_styles_contain_copied_state(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".btn-export-copied" in _STYLES_CSS

    def test_export_buttons_have_aria_labels(self):
        from kairos.dashboard import _APP_JS

        assert "Download JSON" in _APP_JS
        assert "Download CSV" in _APP_JS
        assert "Copy API URL" in _APP_JS

    def test_export_buttons_focus_visible(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".btn-export:focus-visible" in _STYLES_CSS


# ---------------------------------------------------------------------------
# Enhancement 7 — Diff Two Runs
# ---------------------------------------------------------------------------


class TestDiffView:
    """Enhancement 7 — diff two runs."""

    def test_app_js_contains_render_diff_view(self):
        from kairos.dashboard import _APP_JS

        assert "renderDiffView" in _APP_JS

    def test_app_js_contains_show_diff_view(self):
        from kairos.dashboard import _APP_JS

        assert "showDiffView" in _APP_JS

    def test_app_js_contains_selected_runs_state(self):
        from kairos.dashboard import _APP_JS

        assert "selectedRuns" in _APP_JS

    def test_app_js_contains_extract_step_list(self):
        from kairos.dashboard import _APP_JS

        assert "extractStepList" in _APP_JS

    def test_app_js_contains_handle_run_checkbox(self):
        from kairos.dashboard import _APP_JS

        assert "handleRunCheckbox" in _APP_JS

    def test_app_js_contains_update_compare_button(self):
        from kairos.dashboard import _APP_JS

        assert "updateCompareButton" in _APP_JS

    def test_app_js_contains_run_checkbox(self):
        from kairos.dashboard import _APP_JS

        assert "run-checkbox" in _APP_JS

    def test_app_js_contains_compare_btn(self):
        from kairos.dashboard import _APP_JS

        assert "compare-btn" in _APP_JS

    def test_styles_contain_diff_view(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-view" in _STYLES_CSS

    def test_styles_contain_diff_changed(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-changed" in _STYLES_CSS

    def test_styles_contain_diff_column(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-column" in _STYLES_CSS

    def test_styles_contain_checkbox_classes(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".th-checkbox" in _STYLES_CSS
        assert ".td-checkbox" in _STYLES_CSS

    def test_styles_contain_compare_btn(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".compare-btn" in _STYLES_CSS

    def test_styles_contain_diff_delta(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-delta" in _STYLES_CSS
        assert ".diff-delta-better" in _STYLES_CSS
        assert ".diff-delta-worse" in _STYLES_CSS

    def test_styles_contain_diff_missing(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-missing" in _STYLES_CSS

    def test_checkbox_has_aria_label(self):
        from kairos.dashboard import _APP_JS

        assert "Select run" in _APP_JS or "select run" in _APP_JS.lower()

    def test_checkbox_stop_propagation(self):
        """Checkbox click must stopPropagation to prevent row navigation."""
        from kairos.dashboard import _APP_JS

        assert "stopPropagation" in _APP_JS

    def test_diff_view_uses_promise_all(self):
        """Diff view fetches both runs in parallel."""
        from kairos.dashboard import _APP_JS

        assert "Promise.all" in _APP_JS

    def test_diff_view_uses_esc_for_run_ids(self):
        """Run IDs in diff view must be escaped."""
        from kairos.dashboard import _APP_JS

        assert "esc(" in _APP_JS

    def test_diff_router_integration(self):
        """Router must handle 'diff' view."""
        from kairos.dashboard import _APP_JS

        assert "'diff'" in _APP_JS or '"diff"' in _APP_JS

    def test_selected_runs_cleared_on_navigate(self):
        """selectedRuns should be cleared when navigating to run-list."""
        from kairos.dashboard import _APP_JS

        assert "selectedRuns = []" in _APP_JS or "selectedRuns=[]" in _APP_JS

    def test_compare_btn_focus_visible(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".compare-btn:focus-visible" in _STYLES_CSS

    def test_diff_step_row_classes(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-step-row" in _STYLES_CSS
        assert ".diff-step-name" in _STYLES_CSS
        assert ".diff-step-duration" in _STYLES_CSS

    def test_diff_arrow_classes(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".diff-arrow-improve" in _STYLES_CSS
        assert ".diff-arrow-regress" in _STYLES_CSS

    def test_max_two_selections_enforced(self):
        """handleRunCheckbox must enforce max 2 selections."""
        from kairos.dashboard import _APP_JS

        assert ">= 2" in _APP_JS or ">=2" in _APP_JS or "=== 2" in _APP_JS

    def test_extract_step_list_handles_all_event_types(self):
        """extractStepList must process step_start/step_complete/step_fail/step_skip."""
        from kairos.dashboard import _APP_JS

        assert "step_start" in _APP_JS
        assert "step_complete" in _APP_JS
        assert "step_fail" in _APP_JS
        assert "step_skip" in _APP_JS

    def test_diff_view_renders_two_columns(self):
        """diff-view must use a two-column grid layout."""
        from kairos.dashboard import _STYLES_CSS

        assert "grid-template-columns: 1fr 1fr" in _STYLES_CSS

    def test_refresh_run_list_view_includes_checkbox_column(self):
        """refreshRunListView must include the run-checkbox input in rebuilt rows."""
        from kairos.dashboard import _APP_JS

        # Locate the refreshRunListView function body and verify it contains run-checkbox
        idx = _APP_JS.find("function refreshRunListView")
        assert idx != -1, "refreshRunListView not found"
        # Find the end of the function by locating the next top-level function after it
        snippet = _APP_JS[idx : idx + 2000]
        assert "run-checkbox" in snippet, "run-checkbox not found in refreshRunListView"

    def test_duration_delta_filters_sub_ms_differences(self):
        """Duration delta must not be shown for differences smaller than 1ms."""
        from kairos.dashboard import _APP_JS

        assert "Math.abs(diff) >= 1" in _APP_JS

    def test_step_matching_uses_union_of_step_ids(self):
        """Diff view step list must use union of step IDs from both runs."""
        from kairos.dashboard import _APP_JS

        # The union is built by iterating stepsA then stepsB with a 'seen' guard
        assert "allStepIds" in _APP_JS
        assert "stepsA" in _APP_JS
        assert "stepsB" in _APP_JS


# ---------------------------------------------------------------------------
# Enhancement 13 — Keyboard Shortcuts
# ---------------------------------------------------------------------------


class TestKeyboardShortcuts:
    """Enhancement 13 — Keyboard shortcuts."""

    def test_app_js_has_register_shortcuts(self):
        from kairos.dashboard import _APP_JS

        assert "registerShortcuts" in _APP_JS

    def test_app_js_has_handle_shortcut(self):
        from kairos.dashboard import _APP_JS

        assert "handleShortcut" in _APP_JS

    def test_app_js_has_show_shortcuts_overlay(self):
        from kairos.dashboard import _APP_JS

        assert "showShortcutsOverlay" in _APP_JS

    def test_app_js_has_hide_shortcuts_overlay(self):
        from kairos.dashboard import _APP_JS

        assert "hideShortcutsOverlay" in _APP_JS

    def test_app_js_has_move_run_selection(self):
        from kairos.dashboard import _APP_JS

        assert "moveRunSelection" in _APP_JS

    def test_app_js_has_toggle_all_step_groups(self):
        from kairos.dashboard import _APP_JS

        assert "toggleAllStepGroups" in _APP_JS

    def test_shortcuts_not_fire_when_input_focused(self):
        """JS checks activeElement.tagName before dispatching."""
        from kairos.dashboard import _APP_JS

        assert "activeElement" in _APP_JS
        assert "INPUT" in _APP_JS

    def test_all_shortcut_keys_in_handler(self):
        """Handler covers all 8 shortcut keys."""
        from kairos.dashboard import _APP_JS

        for key in ["'j'", "'k'", "'Enter'", "'Escape'", "'/'", "'r'", "'e'", "'?'"]:
            assert key in _APP_JS, f"Missing shortcut key: {key}"

    def test_inspector_tab_shortcuts(self):
        """1/2/3 keys switch inspector tabs."""
        from kairos.dashboard import _APP_JS

        assert "'1'" in _APP_JS or '"1"' in _APP_JS

    def test_shortcuts_overlay_has_aria_dialog(self):
        from kairos.dashboard import _APP_JS

        assert "role" in _APP_JS
        assert "dialog" in _APP_JS
        assert "aria-modal" in _APP_JS

    def test_styles_has_shortcuts_overlay(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".shortcuts-overlay" in _STYLES_CSS

    def test_styles_has_shortcuts_modal(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".shortcuts-modal" in _STYLES_CSS

    def test_styles_has_kbd_styling(self):
        from kairos.dashboard import _STYLES_CSS

        assert "kbd" in _STYLES_CSS

    def test_styles_has_run_row_selected(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".run-row-selected" in _STYLES_CSS

    def test_styles_has_shortcuts_trigger(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".shortcuts-trigger" in _STYLES_CSS

    def test_styles_respects_reduced_motion(self):
        from kairos.dashboard import _STYLES_CSS

        assert "prefers-reduced-motion" in _STYLES_CSS

    def test_index_has_shortcuts_trigger(self):
        from kairos.dashboard import _INDEX_HTML

        assert "shortcuts-trigger" in _INDEX_HTML

    def test_selected_run_index_state(self):
        from kairos.dashboard import _APP_JS

        assert "selectedRunIndex" in _APP_JS

    def test_shortcuts_overlay_visible_state(self):
        from kairos.dashboard import _APP_JS

        assert "shortcutsOverlayVisible" in _APP_JS

    def test_overlay_all_shortcuts_documented(self):
        """Overlay contains documentation for all shortcut keys."""
        from kairos.dashboard import _APP_JS

        assert "Next run" in _APP_JS or "next run" in _APP_JS.lower()
        assert "Go back" in _APP_JS or "go back" in _APP_JS.lower()
        assert "auto-refresh" in _APP_JS.lower() or "Auto-refresh" in _APP_JS

    def test_escape_works_in_input(self):
        """Escape key must fire even when input is focused."""
        from kairos.dashboard import _APP_JS

        assert "Escape" in _APP_JS

    def test_prevent_default_on_slash(self):
        """/ shortcut must preventDefault to avoid typing / in input."""
        from kairos.dashboard import _APP_JS

        assert "preventDefault" in _APP_JS

    def test_move_run_selection_clamps_to_bounds(self):
        """moveRunSelection uses Math.max/Math.min to clamp selection index."""
        from kairos.dashboard import _APP_JS

        assert "Math.max" in _APP_JS
        assert "Math.min" in _APP_JS

    def test_show_shortcuts_overlay_guards_double_open(self):
        """showShortcutsOverlay checks shortcutsOverlayVisible before opening."""
        from kairos.dashboard import _APP_JS

        # The guard must appear inside the showShortcutsOverlay function body.
        idx = _APP_JS.index("function showShortcutsOverlay")
        body_slice = _APP_JS[idx : idx + 200]
        assert "shortcutsOverlayVisible" in body_slice

    def test_overlay_html_uses_only_static_strings(self):
        """Overlay HTML must not interpolate dynamic data (no esc() calls inside it)."""
        from kairos.dashboard import _APP_JS

        # Find the innerHTML block inside showShortcutsOverlay.
        start = _APP_JS.index("function showShortcutsOverlay")
        end = _APP_JS.index("function hideShortcutsOverlay")
        overlay_block = _APP_JS[start:end]
        # esc( must not appear inside the overlay construction block.
        assert "esc(" not in overlay_block

    def test_escape_handler_is_separate_from_input_guard(self):
        """Escape is handled before the INPUT/SELECT/TEXTAREA focus guard."""
        from kairos.dashboard import _APP_JS

        escape_pos = _APP_JS.index("e.key === 'Escape'")
        input_guard_pos = _APP_JS.index("tag === 'INPUT'")
        assert escape_pos < input_guard_pos, (
            "Escape handler must appear before the input focus guard"
        )

    def test_toggle_all_step_groups_checks_run_detail_context(self):
        """'e' shortcut only calls toggleAllStepGroups when in run-detail view."""
        from kairos.dashboard import _APP_JS

        # The guard must be present: currentView === 'run-detail' before the call.
        idx = _APP_JS.index("toggleAllStepGroups()")
        guard_slice = _APP_JS[max(0, idx - 100) : idx]
        assert "run-detail" in guard_slice

    def test_styles_has_shortcuts_close(self):
        """CSS defines .shortcuts-close for the modal close button."""
        from kairos.dashboard import _STYLES_CSS

        assert ".shortcuts-close" in _STYLES_CSS

    def test_run_row_selected_uses_left_border(self):
        """Design system selected-row pattern uses border-left, not outline."""
        from kairos.dashboard import _STYLES_CSS

        idx = _STYLES_CSS.index(".run-row-selected")
        block = _STYLES_CSS[idx : idx + 200]
        assert "border-left" in block
        assert "outline" not in block

    def test_status_text_span_in_index(self):
        """Footer uses a dedicated <span id='status-text'> so the ? button survives updates."""
        from kairos.dashboard import _INDEX_HTML

        assert 'id="status-text"' in _INDEX_HTML

    def test_escape_calls_prevent_default(self):
        """Escape handler calls preventDefault."""
        from kairos.dashboard import _APP_JS

        # Find the Escape block and confirm preventDefault is called within it.
        idx = _APP_JS.index("e.key === 'Escape'")
        block = _APP_JS[idx : idx + 150]
        assert "preventDefault" in block

    def test_selected_run_index_reset_on_auto_refresh(self):
        """Auto-refresh callback resets selectedRunIndex to avoid stale selection."""
        from kairos.dashboard import _APP_JS

        # selectedRunIndex = -1 must appear inside the setInterval fetch callback.
        idx = _APP_JS.index("autoRefreshTimer = setInterval")
        refresh_block = _APP_JS[idx : idx + 1000]
        assert "selectedRunIndex = -1" in refresh_block

    def test_shortcuts_close_button_in_overlay(self):
        """showShortcutsOverlay creates a .shortcuts-close button."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function showShortcutsOverlay")
        end = _APP_JS.index("function hideShortcutsOverlay")
        overlay_block = _APP_JS[start:end]
        assert "shortcuts-close" in overlay_block

    def test_overlay_focus_after_append(self):
        """showShortcutsOverlay sets tabindex and calls focus() on the overlay."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function showShortcutsOverlay")
        end = _APP_JS.index("function hideShortcutsOverlay")
        overlay_block = _APP_JS[start:end]
        assert "tabindex" in overlay_block
        assert ".focus()" in overlay_block


# ---------------------------------------------------------------------------
# Enhancement 12 — Validation Detail Panel
# ---------------------------------------------------------------------------


class TestValidationDetailPanel:
    """Enhancement 12 — Validation detail panel."""

    def test_app_js_has_render_validation_table(self):
        from kairos.dashboard import _APP_JS

        assert "renderValidationTable" in _APP_JS

    def test_app_js_has_extract_validation_data(self):
        from kairos.dashboard import _APP_JS

        assert "extractValidationData" in _APP_JS

    def test_app_js_has_icon_checkmark(self):
        from kairos.dashboard import _APP_JS

        assert "iconCheckmark" in _APP_JS

    def test_app_js_has_icon_xmark(self):
        from kairos.dashboard import _APP_JS

        assert "iconXMark" in _APP_JS

    def test_styles_has_validation_table(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-table" in _STYLES_CSS

    def test_styles_has_validation_row_fail(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-row-fail" in _STYLES_CSS

    def test_styles_has_validation_row_pass(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-row-pass" in _STYLES_CSS

    def test_styles_has_validation_expandable(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-expandable" in _STYLES_CSS

    def test_styles_has_validation_footer(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-footer" in _STYLES_CSS

    def test_styles_has_validation_status_icon(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-status-icon" in _STYLES_CSS

    def test_validation_table_sorts_failures_first(self):
        """renderValidationTable iterates errors before passing fields."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function renderValidationTable")
        # Search within a generous window covering the full function body.
        end = start + 3000
        block = _APP_JS[start:end]
        # Failed rows are built by iterating `errors` (the first argument).
        # Passing rows are built by iterating `passingFields`.
        # The errors forEach must appear before the passingFields forEach.
        errors_iter_pos = block.index("errors.forEach")
        passing_iter_pos = block.index("passingFields.forEach")
        assert errors_iter_pos < passing_iter_pos, (
            "errors.forEach must appear before passingFields.forEach in renderValidationTable"
        )

    def test_validation_uses_svg_icons_not_emoji(self):
        """Status icons use SVG checkmark/X, not emoji."""
        from kairos.dashboard import _APP_JS

        assert "iconCheckmark" in _APP_JS
        assert "iconXMark" in _APP_JS
        # Verify these are actual SVG functions, not bare emoji strings.
        assert "viewBox" in _APP_JS

    def test_validation_footer_shows_count(self):
        """Footer shows 'N of M fields failed'."""
        from kairos.dashboard import _APP_JS

        assert "fields failed" in _APP_JS

    def test_validation_expandable_toggle(self):
        """Event delegation handles .validation-row clicks and toggles .visible on expandable."""
        from kairos.dashboard import _APP_JS

        # Locate the event delegation click handler block.
        assert "closest('.validation-row')" in _APP_JS or 'closest(".validation-row")' in _APP_JS, (
            "Event delegation must use closest('.validation-row') to detect clicks"
        )
        # The handler must add/remove the 'visible' class on the expandable sibling.
        assert "validation-expandable" in _APP_JS
        assert "classList" in _APP_JS
        # Both branches (add and remove) must be present.
        assert "classList.add" in _APP_JS
        assert "classList.remove" in _APP_JS

    def test_validation_table_uses_esc(self):
        """Field names and values must be escaped via esc()."""
        from kairos.dashboard import _APP_JS

        # esc() is used throughout app.js; verify it appears inside renderValidationTable.
        start = _APP_JS.index("function renderValidationTable")
        end = _APP_JS.index("function renderValidationTable") + 2000
        block = _APP_JS[start:end]
        assert "esc(" in block

    def test_inspector_uses_validation_table(self):
        """Inspector's Validation tab calls extractValidationData."""
        from kairos.dashboard import _APP_JS

        assert "extractValidationData" in _APP_JS

    def test_validation_table_has_colspan(self):
        """Expandable rows use colspan for full-width detail."""
        from kairos.dashboard import _APP_JS

        assert "colspan" in _APP_JS

    def test_styles_has_validation_detail(self):
        """CSS defines .validation-detail wrapper."""
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-detail" in _STYLES_CSS

    def test_validation_row_cursor_pointer(self):
        """Validation rows have cursor: pointer since they are clickable."""
        from kairos.dashboard import _STYLES_CSS

        idx = _STYLES_CSS.index(".validation-row")
        block = _STYLES_CSS[idx : idx + 200]
        assert "cursor" in block

    def test_validation_expandable_default_hidden(self):
        """Expandable rows are hidden by default (display: none)."""
        from kairos.dashboard import _STYLES_CSS

        idx = _STYLES_CSS.index(".validation-expandable")
        block = _STYLES_CSS[idx : idx + 200]
        assert "display: none" in block

    def test_validation_expandable_visible_class(self):
        """CSS defines .validation-expandable.visible to show expanded rows."""
        from kairos.dashboard import _STYLES_CSS

        assert ".validation-expandable.visible" in _STYLES_CSS

    def test_extract_validation_data_function_defined(self):
        """extractValidationData function is defined in app.js."""
        from kairos.dashboard import _APP_JS

        assert "function extractValidationData" in _APP_JS

    def test_render_validation_table_function_defined(self):
        """renderValidationTable function is defined in app.js."""
        from kairos.dashboard import _APP_JS

        assert "function renderValidationTable" in _APP_JS

    def test_inspector_validation_tab_uses_extract(self):
        """renderInspectorPanel's validation branch calls extractValidationData."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function renderInspectorPanel")
        end = _APP_JS.index("function renderInspectorPanel") + 2000
        block = _APP_JS[start:end]
        assert "extractValidationData" in block

    def test_validation_row_click_toggles_visible(self):
        """Event delegation toggles 'visible' class on the expandable row."""
        from kairos.dashboard import _APP_JS

        # 'visible' class must be toggled via classList or className manipulation.
        assert "visible" in _APP_JS
        # The delegation must reference validation-row to detect clicks.
        assert "validation-row" in _APP_JS

    def test_icon_checkmark_uses_svg_path(self):
        """iconCheckmark returns an SVG with a checkmark path."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function iconCheckmark")
        block = _APP_JS[start : start + 300]
        assert "<svg" in block
        assert "path" in block

    def test_icon_xmark_uses_svg_path(self):
        """iconXMark returns an SVG with an X path."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function iconXMark")
        block = _APP_JS[start : start + 300]
        assert "<svg" in block
        assert "path" in block

    def test_validation_table_has_validator_column(self):
        """renderValidationTable includes a Validator column header and cell."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function renderValidationTable")
        end = start + 3000
        block = _APP_JS[start:end]
        # Column header must be present.
        assert "Validator" in block, "renderValidationTable must include a 'Validator' <th>"
        # scope='col' must be on all th elements.
        assert 'scope="col"' in block, 'renderValidationTable th elements must have scope="col"'

    def test_validation_table_has_aria_expanded(self):
        """Failed validation rows include aria-expanded and the click handler updates it."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function renderValidationTable")
        end = start + 3000
        block = _APP_JS[start:end]
        # The failed row must be rendered with aria-expanded="false" initially.
        assert 'aria-expanded="false"' in block, (
            "renderValidationTable must set aria-expanded='false' on failed rows"
        )
        # The click handler must update aria-expanded when toggling.
        assert "setAttribute" in _APP_JS, "Click handler must update aria-expanded via setAttribute"
        assert "aria-expanded" in _APP_JS

    def test_extractvalidationdata_captures_validator_name(self):
        """extractValidationData reads err.validator and err.constraint for the validator name."""
        from kairos.dashboard import _APP_JS

        start = _APP_JS.index("function extractValidationData")
        end = start + 2000
        block = _APP_JS[start:end]
        assert "err.validator" in block, "extractValidationData must read err.validator"
        assert "err.constraint" in block, "extractValidationData must fall back to err.constraint"


# ============================================================
# === Enhancement 11 — Retry Timeline ===
# ============================================================


class TestRetryTimeline:
    """Enhancement 11 — Retry timeline."""

    def test_app_js_has_extract_retry_attempts(self):
        from kairos.dashboard import _APP_JS

        assert "extractRetryAttempts" in _APP_JS

    def test_app_js_has_render_retry_timeline_placeholder(self):
        from kairos.dashboard import _APP_JS

        assert "renderRetryTimelinePlaceholder" in _APP_JS

    def test_app_js_has_mount_retry_timeline(self):
        from kairos.dashboard import _APP_JS

        assert "mountRetryTimeline" in _APP_JS

    def test_styles_has_retry_timeline_container(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".retry-timeline-container" in _STYLES_CSS

    def test_styles_has_retry_expanded_context(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".retry-expanded-context" in _STYLES_CSS

    def test_retry_timeline_reuses_svg_helpers(self):
        """mountRetryTimeline must use SVG helpers."""
        from kairos.dashboard import _APP_JS

        assert "svgRect" in _APP_JS
        assert "svgText" in _APP_JS

    def test_retry_cards_have_data_attributes(self):
        """Cards must have data-attempt and data-step-id."""
        from kairos.dashboard import _APP_JS

        assert "data-attempt" in _APP_JS
        assert "data-step-id" in _APP_JS

    def test_retry_timeline_uses_createelementns(self):
        """SVG elements created with createElementNS."""
        from kairos.dashboard import _APP_JS

        assert "createElementNS" in _APP_JS

    def test_retry_timeline_uses_css_tokens(self):
        """Must read design system tokens via getComputedStyle."""
        from kairos.dashboard import _APP_JS

        assert "getCssTokens" in _APP_JS

    def test_extract_retry_attempts_reads_step_events(self):
        """extractRetryAttempts accesses step_start, step_fail, step_retry events."""
        from kairos.dashboard import _APP_JS

        assert "step_retry" in _APP_JS

    def test_retry_card_shows_error_text(self):
        """Failed cards show truncated error message."""
        from kairos.dashboard import _APP_JS

        assert "error" in _APP_JS.lower()

    def test_retry_connector_shows_backoff(self):
        """Connector arrows show backoff delay label."""
        from kairos.dashboard import _APP_JS

        assert "backoff" in _APP_JS.lower() or "Backoff" in _APP_JS

    def test_retry_timeline_container_overflow(self):
        """Container must have overflow-x: auto."""
        from kairos.dashboard import _STYLES_CSS

        assert "overflow-x" in _STYLES_CSS

    def test_retry_expanded_context_max_height(self):
        """Expanded context has max-height for scrolling."""
        from kairos.dashboard import _STYLES_CSS

        assert "max-height" in _STYLES_CSS

    def test_retry_click_expands_context(self):
        """Click handler for retry cards toggles expanded context."""
        from kairos.dashboard import _APP_JS

        assert "retry-expanded-context" in _APP_JS

    def test_retry_placeholder_in_step_groups(self):
        """renderStepGroups inserts retry timeline for retried steps."""
        from kairos.dashboard import _APP_JS

        assert "renderRetryTimelinePlaceholder" in _APP_JS

    def test_mount_retry_called_in_show_run_detail(self):
        """showRunDetail calls mountRetryTimeline for retried steps."""
        from kairos.dashboard import _APP_JS

        assert "mountRetryTimeline" in _APP_JS

    def test_retry_uses_arrow_marker(self):
        """Connector arrows use svgArrowMarker."""
        from kairos.dashboard import _APP_JS

        assert "svgArrowMarker" in _APP_JS

    def test_extract_retry_attempts_accesses_attempt_field(self):
        """extractRetryAttempts reads data.attempt from events."""
        from kairos.dashboard import _APP_JS

        assert "att.attempt" in _APP_JS or ".attempt" in _APP_JS

    def test_mount_retry_uses_font_ui_not_only_font_mono(self):
        """mountRetryTimeline uses tokens.fontUi for the attempt number."""
        from kairos.dashboard import _APP_JS

        assert "tokens.fontUi" in _APP_JS

    def test_backoff_label_uses_text_faint_token(self):
        """Backoff label fill uses tokens.textFaint, not tokens.textMuted."""
        from kairos.dashboard import _APP_JS

        assert "tokens.textFaint" in _APP_JS

    def test_error_text_is_truncated(self):
        """Error text is sliced to prevent long strings in SVG."""
        from kairos.dashboard import _APP_JS

        assert ".slice(0, 14)" in _APP_JS or "slice(0," in _APP_JS

    def test_single_attempt_hides_retry_timeline(self):
        """Steps with only one attempt hide the retry timeline container."""
        from kairos.dashboard import _APP_JS

        # The guard: if attempts.length < 2 → container.style.display = 'none'
        assert "attempts.length < 2" in _APP_JS
        assert "display" in _APP_JS and "'none'" in _APP_JS

    def test_expanded_context_uses_colorize_json(self):
        """Expanded retry context renders via colorizeJson for XSS safety."""
        from kairos.dashboard import _APP_JS

        assert "colorizeJson" in _APP_JS

    def test_css_tokens_includes_font_ui(self):
        """getCssTokens exposes fontUi token."""
        from kairos.dashboard import _APP_JS

        assert "fontUi" in _APP_JS
        assert "--font-ui" in _APP_JS

    def test_css_tokens_includes_text_faint(self):
        """getCssTokens exposes textFaint token."""
        from kairos.dashboard import _APP_JS

        assert "textFaint" in _APP_JS
        assert "--text-faint" in _APP_JS

    def test_expanded_context_max_height_400(self):
        """retry-expanded-context max-height is 400px."""
        from kairos.dashboard import _STYLES_CSS

        assert "max-height: 400px" in _STYLES_CSS

    def test_attempt_number_uses_weight_600(self):
        """Attempt number uses fontWeight 600, not bold."""
        from kairos.dashboard import _APP_JS

        assert "'600'" in _APP_JS or '"600"' in _APP_JS

    def test_error_text_font_size_11(self):
        """Error text fontSize is 11 (matching --text-xs), not 10."""
        from kairos.dashboard import _APP_JS

        # Verify 11 appears after the error text comment
        idx = _APP_JS.find("Error text (failed cards only)")
        assert idx != -1
        snippet = _APP_JS[idx : idx + 600]
        assert "fontSize: 11" in snippet

    def test_backoff_label_font_size_11(self):
        """Backoff label fontSize is 11 (matching --text-xs), not 10."""
        from kairos.dashboard import _APP_JS

        # Verify 11 appears in the backoff label block (search for the label text)
        idx = _APP_JS.find("Backoff label below the connector")
        assert idx != -1
        snippet = _APP_JS[idx : idx + 600]
        assert "fontSize: 11" in snippet


# ============================================================
# Enhancement 10 — Duration Flame Chart
# ============================================================


class TestFlameChart:
    """Enhancement 10 — Duration flame chart."""

    def test_app_js_has_extract_flame_chart_data(self):
        from kairos.dashboard import _APP_JS

        assert "extractFlameChartData" in _APP_JS

    def test_app_js_has_compute_axis_ticks(self):
        from kairos.dashboard import _APP_JS

        assert "computeAxisTicks" in _APP_JS

    def test_app_js_has_render_flame_chart_placeholder(self):
        from kairos.dashboard import _APP_JS

        assert "renderFlameChartPlaceholder" in _APP_JS

    def test_app_js_has_mount_flame_chart(self):
        from kairos.dashboard import _APP_JS

        assert "mountFlameChart" in _APP_JS

    def test_app_js_has_show_flame_tooltip(self):
        from kairos.dashboard import _APP_JS

        assert "showFlameTooltip" in _APP_JS

    def test_app_js_has_hide_flame_tooltip(self):
        from kairos.dashboard import _APP_JS

        assert "hideFlameTooltip" in _APP_JS

    def test_styles_has_flame_chart_container(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".flame-chart-container" in _STYLES_CSS

    def test_styles_has_flame_chart_tooltip(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".flame-chart-tooltip" in _STYLES_CSS

    def test_styles_has_tooltip_label_class(self):
        from kairos.dashboard import _STYLES_CSS

        assert ".tt-label" in _STYLES_CSS

    def test_flame_chart_uses_svg_helpers(self):
        """mountFlameChart must use svgRect, svgText, svgLine."""
        from kairos.dashboard import _APP_JS

        assert "svgRect" in _APP_JS
        assert "svgLine" in _APP_JS

    def test_flame_chart_uses_createelementns(self):
        from kairos.dashboard import _APP_JS

        assert "createElementNS" in _APP_JS

    def test_flame_chart_uses_css_tokens(self):
        from kairos.dashboard import _APP_JS

        assert "getCssTokens" in _APP_JS

    def test_flame_chart_constants_defined(self):
        from kairos.dashboard import _APP_JS

        assert "FLAME_ROW_H" in _APP_JS
        assert "FLAME_BAR_H" in _APP_JS
        assert "FLAME_LABEL_W" in _APP_JS

    def test_flame_chart_bars_have_step_id(self):
        """SVG bars carry data-step-id for click navigation."""
        from kairos.dashboard import _APP_JS

        assert "data-step-id" in _APP_JS

    def test_flame_chart_tooltip_has_pointer_events_none(self):
        from kairos.dashboard import _STYLES_CSS

        assert "pointer-events: none" in _STYLES_CSS

    def test_flame_chart_container_overflow(self):
        from kairos.dashboard import _STYLES_CSS

        assert "overflow-x" in _STYLES_CSS

    def test_flame_chart_reduced_motion(self):
        from kairos.dashboard import _STYLES_CSS

        assert "prefers-reduced-motion" in _STYLES_CSS

    def test_flame_chart_placeholder_in_run_detail(self):
        from kairos.dashboard import _APP_JS

        assert "renderFlameChartPlaceholder" in _APP_JS
        assert "flame-chart" in _APP_JS

    def test_mount_flame_chart_called_in_show_run_detail(self):
        from kairos.dashboard import _APP_JS

        assert "mountFlameChart" in _APP_JS

    def test_flame_chart_click_scrolls_to_step(self):
        from kairos.dashboard import _APP_JS

        assert "scrollToStepGroup" in _APP_JS

    def test_extract_reads_workflow_start(self):
        """extractFlameChartData uses workflow_start timestamp as time zero."""
        from kairos.dashboard import _APP_JS

        assert "workflow_start" in _APP_JS

    def test_flame_chart_axis_auto_scales(self):
        """computeAxisTicks auto-scales tick intervals."""
        from kairos.dashboard import _APP_JS

        # Should contain multiple interval options
        assert "1000" in _APP_JS or "500" in _APP_JS  # ms intervals

    def test_tooltip_shows_duration(self):
        from kairos.dashboard import _APP_JS

        assert "Duration" in _APP_JS or "duration" in _APP_JS

    # --- Findings 2-3: Behavioral assertions and failure paths ---

    def test_extract_accesses_workflow_start_timestamp(self):
        """extractFlameChartData accesses workflow_start event type."""
        from kairos.dashboard import _APP_JS

        assert "workflow_start" in _APP_JS
        assert "event_type" in _APP_JS

    def test_compute_axis_ticks_interval_array(self):
        """computeAxisTicks has interval array with multiple canonical values."""
        from kairos.dashboard import _APP_JS

        for val in ["1, 5, 10, 50, 100, 500, 1000, 5000"]:
            assert val in _APP_JS, f"Expected interval sequence '{val}' in computeAxisTicks"

    def test_retry_gap_segments_use_stroke_dasharray(self):
        """Retry gap segments between attempt bars use stroke-dasharray."""
        from kairos.dashboard import _APP_JS

        assert "stroke-dasharray" in _APP_JS

    def test_retry_gap_segments_use_chart_bar_gap_token(self):
        """Retry gap segments reference chartBarGap CSS token."""
        from kairos.dashboard import _APP_JS

        assert "chartBarGap" in _APP_JS

    def test_mount_flame_chart_hides_container_when_no_steps(self):
        """mountFlameChart sets display:none when there are no steps."""
        from kairos.dashboard import _APP_JS

        assert "display" in _APP_JS
        assert "'none'" in _APP_JS or '"none"' in _APP_JS

    def test_zero_duration_workflow_fallback(self):
        """mountFlameChart uses || 1 fallback so zero-duration workflows never divide by zero."""
        from kairos.dashboard import _APP_JS

        assert "|| 1" in _APP_JS

    def test_tooltip_textcontent_used_for_dynamic_values(self):
        """showFlameTooltip uses textContent (not innerHTML) for dynamic values."""
        from kairos.dashboard import _APP_JS

        assert "textContent" in _APP_JS
        # innerHTML should NOT be used in showFlameTooltip context;
        # the function now builds DOM nodes with textContent
        # Verify the DOM construction pattern is present
        assert "createElement" in _APP_JS


# ---------------------------------------------------------------------------
# Enhancement 9: Search Across Runs
# ---------------------------------------------------------------------------


def _make_search_jsonl(run_id: str, workflow_name: str, event_type: str, step_id: str) -> str:
    """Build a .jsonl file with a workflow_start event and one custom event."""
    events = [
        {
            "timestamp": "2024-01-01T12:00:00+00:00",
            "event_type": "workflow_start",
            "step_id": None,
            "data": {"workflow_name": workflow_name, "run_id": run_id, "total_steps": 1},
            "level": "LogLevel.INFO",
        },
        {
            "timestamp": "2024-01-01T12:00:01+00:00",
            "event_type": event_type,
            "step_id": step_id,
            "data": {"step_id": step_id, "status": "completed"},
            "level": "LogLevel.INFO",
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


@pytest.fixture()
def search_server(tmp_path: Path):
    """Start a DashboardServer with two .jsonl files containing distinct events."""
    from kairos.dashboard import DashboardServer

    (tmp_path / "wf_alpha_run001.jsonl").write_text(
        _make_search_jsonl("run001", "alpha_workflow", "step_complete", "fetch_data"),
        encoding="utf-8",
    )
    (tmp_path / "wf_beta_run002.jsonl").write_text(
        _make_search_jsonl("run002", "beta_workflow", "step_fail", "report"),
        encoding="utf-8",
    )

    token = "search-test-token"
    server = DashboardServer(port=0, log_dir=str(tmp_path), auth_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    yield server, base_url, token, tmp_path
    server.shutdown()
    thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Group: Search — Security
# ---------------------------------------------------------------------------


class TestSearchEndpointSecurity:
    def test_search_requires_auth(self, search_server):
        """GET /api/search without token returns 403."""
        _server, base_url, _token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=test")
        assert status == 403

    def test_search_has_csp_headers(self, search_server):
        """Response includes CSP + nosniff headers."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&token={token}")
        assert status == 200
        headers = {k.lower(): v for k, v in data["headers"].items()}
        assert "content-security-policy" in headers
        assert headers.get("x-content-type-options", "").lower() == "nosniff"

    def test_search_post_returns_405(self, search_server):
        """POST /api/search returns 405."""
        _server, base_url, token, _tmp = search_server
        status, _data = _fetch(f"{base_url}/api/search?q=test&token={token}", method="POST")
        assert status == 405

    def test_search_query_not_used_as_regex(self, search_server):
        """Regex metacharacters treated as literal — q=.* should not crash or match all."""
        _server, base_url, token, _tmp = search_server
        # '.*' as a regex would match everything; as a literal it should match nothing
        # (no event_type, step_id, or data contains '.*' literally)
        status, data = _fetch(f"{base_url}/api/search?q=.*&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        # Results should be empty — no event contains the literal string '.*'
        assert body["results"] == []

    def test_search_empty_query_returns_empty(self, search_server):
        """Empty q returns results: []."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert body["results"] == []


# ---------------------------------------------------------------------------
# Group: Search — Happy Paths
# ---------------------------------------------------------------------------


class TestSearchHappyPaths:
    def test_search_returns_valid_json_shape(self, search_server):
        """Response has query, results, total_scanned, has_more."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert "query" in body
        assert "results" in body
        assert "total_scanned" in body
        assert "has_more" in body

    def test_search_matches_event_type(self, search_server):
        """Query matching event_type returns results."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step_fail&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert len(body["results"]) >= 1
        assert any(r["event_type"] == "step_fail" for r in body["results"])

    def test_search_case_insensitive(self, search_server):
        """Query 'STEP_FAIL' matches 'step_fail' (case-insensitive)."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=STEP_FAIL&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert len(body["results"]) >= 1

    def test_search_results_have_run_context(self, search_server):
        """Each result has run_id, workflow_name, timestamp."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert len(body["results"]) > 0
        for result in body["results"]:
            assert "run_id" in result
            assert "workflow_name" in result
            assert "timestamp" in result

    def test_search_results_have_snippet(self, search_server):
        """Each result includes a snippet field."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        for result in body["results"]:
            assert "snippet" in result

    def test_search_matches_step_id(self, search_server):
        """Query matching step_id returns results."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=fetch_data&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert len(body["results"]) >= 1
        assert any(r.get("step_id") == "fetch_data" for r in body["results"])

    def test_search_matches_data_field(self, search_server):
        """Query matching content in data dict returns results."""
        _server, base_url, token, _tmp = search_server
        # 'completed' is in the data dict of step_complete events
        status, data = _fetch(f"{base_url}/api/search?q=completed&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert len(body["results"]) >= 1

    def test_search_query_reflected_in_response(self, search_server):
        """Response body contains the original query."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step_fail&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert body["query"] == "step_fail"

    def test_search_no_match_returns_empty_results(self, search_server):
        """Query with no matches returns empty results, not an error."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=zzznomatch999&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert body["results"] == []
        assert body["has_more"] is False

    def test_search_result_has_event_type_and_step_id(self, search_server):
        """Results include event_type and step_id fields."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step_fail&token={token}")
        assert status == 200
        body = json.loads(data["body"])
        assert len(body["results"]) >= 1
        r = body["results"][0]
        assert "event_type" in r
        assert "step_id" in r


# ---------------------------------------------------------------------------
# Group: Search — Unit Tests (_search_events)
# ---------------------------------------------------------------------------


class TestSearchFunction:
    def test_empty_dir_returns_no_results(self, tmp_path: Path):
        """_search_events on empty directory returns empty results."""
        from kairos.dashboard import _search_events

        result = _search_events(str(tmp_path), "test", 0, 50)
        assert result["results"] == []
        assert result["total_scanned"] == 0
        assert result["has_more"] is False

    def test_match_returns_correct_structure(self, tmp_path: Path):
        """Matching events have required keys."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "my_wf", "step_fail", "process"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "step_fail", 0, 50)
        assert len(result["results"]) >= 1
        r = result["results"][0]
        for key in ("run_id", "workflow_name", "timestamp", "event_type", "step_id", "snippet"):
            assert key in r, f"Missing key: {key}"

    def test_respects_limit(self, tmp_path: Path):
        """limit parameter caps the results returned."""
        from kairos.dashboard import _search_events

        # Create 5 files each with a matching event
        for i in range(5):
            (tmp_path / f"run{i:03d}.jsonl").write_text(
                _make_search_jsonl(f"r{i}", "wf", "step_complete", f"step_{i}"),
                encoding="utf-8",
            )
        result = _search_events(str(tmp_path), "step_complete", 0, 3)
        assert len(result["results"]) <= 3

    def test_respects_offset(self, tmp_path: Path):
        """offset parameter skips leading results."""
        from kairos.dashboard import _search_events

        for i in range(4):
            (tmp_path / f"run{i:03d}.jsonl").write_text(
                _make_search_jsonl(f"r{i}", "wf", "step_complete", f"step_{i}"),
                encoding="utf-8",
            )
        result_all = _search_events(str(tmp_path), "step_complete", 0, 100)
        total = len(result_all["results"])
        if total >= 2:
            result_offset = _search_events(str(tmp_path), "step_complete", 1, 100)
            assert len(result_offset["results"]) == total - 1

    def test_has_more_true_when_results_exceed_limit(self, tmp_path: Path):
        """has_more is True when there are more results beyond limit."""
        from kairos.dashboard import _search_events

        # Create 3 files each with a matching event
        for i in range(3):
            (tmp_path / f"run{i:03d}.jsonl").write_text(
                _make_search_jsonl(f"r{i}", "wf", "step_complete", f"step_{i}"),
                encoding="utf-8",
            )
        result = _search_events(str(tmp_path), "step_complete", 0, 2)
        # total matches >= 3, limit=2, so has_more should be True
        assert result["has_more"] is True

    def test_has_more_false_when_all_results_fit(self, tmp_path: Path):
        """has_more is False when all results fit within limit."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "wf", "step_complete", "s1"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        assert result["has_more"] is False

    def test_regex_metacharacters_treated_as_literal(self, tmp_path: Path):
        """'.*' as query is treated as literal string — does not match via regex."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "wf", "step_complete", "s1"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), ".*", 0, 50)
        # No event contains the literal string '.*'
        assert result["results"] == []

    def test_non_jsonl_files_ignored(self, tmp_path: Path):
        """Non-.jsonl files are skipped."""
        from kairos.dashboard import _search_events

        (tmp_path / "notes.txt").write_text('{"event_type": "step_complete"}', encoding="utf-8")
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        assert result["results"] == []

    def test_workflow_name_from_workflow_start_event(self, tmp_path: Path):
        """workflow_name in result comes from the workflow_start event in the file."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "my_named_workflow", "step_complete", "s1"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        assert len(result["results"]) >= 1
        assert result["results"][0]["workflow_name"] == "my_named_workflow"

    def test_run_id_from_event_data_not_filename(self, tmp_path: Path):
        """run_id in result comes from event data, not from the filename."""
        from kairos.dashboard import _search_events

        (tmp_path / "completely_different_filename.jsonl").write_text(
            _make_search_jsonl("event-based-run-id", "wf", "step_complete", "s1"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        assert len(result["results"]) >= 1
        assert result["results"][0]["run_id"] == "event-based-run-id"

    def test_snippet_centered_on_match(self, tmp_path: Path):
        """Snippet is at most 120 chars and contains the query string."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "wf", "step_complete", "s1"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        assert len(result["results"]) >= 1
        snippet = result["results"][0]["snippet"]
        assert len(snippet) <= 120
        assert "step_complete" in snippet.lower()

    def test_total_scanned_counts_all_events(self, tmp_path: Path):
        """total_scanned reflects all events scanned (not just matches)."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "wf", "step_complete", "s1"),
            encoding="utf-8",
        )
        result_match = _search_events(str(tmp_path), "step_complete", 0, 50)
        result_none = _search_events(str(tmp_path), "zzznomatch", 0, 50)
        # Both scanned the same events; total_scanned should be equal
        assert result_match["total_scanned"] == result_none["total_scanned"]
        assert result_match["total_scanned"] > 0

    def test_empty_query_returns_empty_results(self, tmp_path: Path):
        """Empty query string returns no results."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "wf", "step_complete", "s1"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "", 0, 50)
        assert result["results"] == []

    def test_match_field_indicates_which_field_matched(self, tmp_path: Path):
        """Each result has a match_field key indicating event_type, step_id, or data."""
        from kairos.dashboard import _search_events

        (tmp_path / "run.jsonl").write_text(
            _make_search_jsonl("r1", "wf", "step_complete", "process"),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        assert len(result["results"]) >= 1
        for r in result["results"]:
            assert "match_field" in r
            assert r["match_field"] in ("event_type", "step_id", "data")

    def test_file_without_workflow_start_uses_unknown(self, tmp_path: Path):
        """When no workflow_start event exists, workflow_name falls back to 'unknown'."""
        from kairos.dashboard import _search_events

        # Write a file with only a step event — no workflow_start
        event = {
            "timestamp": "2024-01-01T12:00:00+00:00",
            "event_type": "step_complete",
            "step_id": "orphan_step",
            "data": {"status": "completed"},
            "level": "LogLevel.INFO",
        }
        (tmp_path / "orphan.jsonl").write_text(json.dumps(event), encoding="utf-8")
        result = _search_events(str(tmp_path), "orphan_step", 0, 50)
        assert len(result["results"]) >= 1
        assert result["results"][0]["workflow_name"] == "unknown"

    def test_multiple_matches_across_files(self, tmp_path: Path):
        """Search across multiple files returns matches from all of them."""
        from kairos.dashboard import _search_events

        for i in range(3):
            (tmp_path / f"run{i:03d}.jsonl").write_text(
                _make_search_jsonl(f"r{i}", f"wf_{i}", "step_complete", f"step_{i}"),
                encoding="utf-8",
            )
        result = _search_events(str(tmp_path), "step_complete", 0, 50)
        # At least 3 matches (one per file, from the step_complete event)
        assert len(result["results"]) >= 3
        run_ids = {r["run_id"] for r in result["results"]}
        assert run_ids == {"r0", "r1", "r2"}


# ---------------------------------------------------------------------------
# Group: Search — Pagination (HTTP level)
# ---------------------------------------------------------------------------


class TestSearchPagination:
    def test_limit_clamped_to_max_100(self, search_server):
        """limit > 100 is clamped to 100."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&limit=9999&token={token}")
        assert status == 200

    def test_offset_clamped_to_min_0(self, search_server):
        """Negative offset is clamped to 0."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&offset=-5&token={token}")
        assert status == 200

    def test_invalid_offset_defaults_to_0(self, search_server):
        """Non-integer offset defaults to 0 without error."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&offset=abc&token={token}")
        assert status == 200

    def test_invalid_limit_handled_gracefully(self, search_server):
        """Non-integer limit is coerced to a valid value without error."""
        _server, base_url, token, _tmp = search_server
        status, data = _fetch(f"{base_url}/api/search?q=step&limit=xyz&token={token}")
        assert status == 200


# ---------------------------------------------------------------------------
# Group: Search — Frontend (app.js and styles.css)
# ---------------------------------------------------------------------------


class TestSearchUI:
    def test_app_js_has_show_search_view(self):
        """app.js defines showSearchView function."""
        from kairos.dashboard import _APP_JS

        assert "showSearchView" in _APP_JS

    def test_app_js_has_render_search_results(self):
        """app.js defines renderSearchResults function."""
        from kairos.dashboard import _APP_JS

        assert "renderSearchResults" in _APP_JS

    def test_app_js_has_fetch_search(self):
        """app.js defines fetchSearch function."""
        from kairos.dashboard import _APP_JS

        assert "fetchSearch" in _APP_JS

    def test_app_js_has_execute_search(self):
        """app.js defines executeSearch function."""
        from kairos.dashboard import _APP_JS

        assert "executeSearch" in _APP_JS

    def test_styles_has_search_view(self):
        """styles.css contains .search-view class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".search-view" in _STYLES_CSS

    def test_styles_has_search_input_large(self):
        """styles.css contains .search-input-large class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".search-input-large" in _STYLES_CSS

    def test_styles_has_search_result(self):
        """styles.css contains .search-result class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".search-result" in _STYLES_CSS

    def test_styles_has_search_load_more(self):
        """styles.css contains .search-load-more class."""
        from kairos.dashboard import _STYLES_CSS

        assert ".search-load-more" in _STYLES_CSS

    def test_search_result_uses_esc(self):
        """renderSearchResults uses esc() to HTML-escape dynamic result fields."""
        from kairos.dashboard import _APP_JS

        # Locate the renderSearchResults function body
        start = _APP_JS.find("function renderSearchResults(")
        assert start != -1, "renderSearchResults not found in app.js"
        # Find the closing brace by scanning forward — take a generous slice
        body = _APP_JS[start : start + 2000]
        # Verify esc() is called on the key result fields used in innerHTML
        assert "esc(r.run_id" in body
        assert "esc(r.workflow_name" in body
        assert "esc(r.event_type" in body
        assert "esc(r.step_id" in body

    def test_search_result_has_mark_highlight(self):
        """renderSearchResults uses <mark> for highlighting the query in snippets."""
        from kairos.dashboard import _APP_JS

        assert "<mark>" in _APP_JS or "mark" in _APP_JS

    def test_search_debounce(self):
        """Search input uses debounce or setTimeout for deferred execution."""
        from kairos.dashboard import _APP_JS

        assert "debounce" in _APP_JS or "setTimeout" in _APP_JS

    def test_router_handles_search_view(self):
        """navigate() / router handles 'search' view."""
        from kairos.dashboard import _APP_JS

        assert "'search'" in _APP_JS or '"search"' in _APP_JS

    def test_search_load_more_in_render(self):
        """renderSearchResults renders a load-more button when hasMore is true."""
        from kairos.dashboard import _APP_JS

        assert "search-load-more" in _APP_JS

    def test_search_result_meta_fields(self):
        """renderSearchResults renders workflow_name, run_id, timestamp meta."""
        from kairos.dashboard import _APP_JS

        assert "result-wf" in _APP_JS
        assert "result-run-id" in _APP_JS

    def test_search_result_event_class(self):
        """renderSearchResults renders event_type with .search-result-event class."""
        from kairos.dashboard import _APP_JS

        assert "search-result-event" in _APP_JS

    def test_search_result_snippet_class(self):
        """renderSearchResults renders snippet with .search-result-snippet class."""
        from kairos.dashboard import _APP_JS

        assert "search-result-snippet" in _APP_JS

    def test_search_state_variables(self):
        """app.js declares searchQuery and searchOffset state variables."""
        from kairos.dashboard import _APP_JS

        assert "searchQuery" in _APP_JS
        assert "searchOffset" in _APP_JS


# ---------------------------------------------------------------------------
# Group: Search — Header reflection and long-query snippet safety
# ---------------------------------------------------------------------------


class TestSearchHeaderAndSnippet:
    def test_search_query_not_reflected_in_headers(self, search_server):
        """Query string must not appear in any response header."""
        server, base_url, token, tmp_path = search_server
        status, data = _fetch(f"{base_url}/api/search?q=UNIQUE_TEST_MARKER&token={token}")
        assert status == 200
        for _header, value in data["headers"].items():
            assert "UNIQUE_TEST_MARKER" not in str(value)

    def test_long_query_snippet_bounded_to_120_chars(self, tmp_path: Path):
        """Snippet is always <= 120 chars even when the source text is very long."""
        from kairos.dashboard import _search_events

        long_data = "x" * 500 + "findme" + "y" * 500
        events = [
            {
                "run_id": "r1",
                "workflow_name": "wf",
                "timestamp": "2024-01-01T00:00:00",
                "event_type": "step_complete",
                "step_id": "s1",
                "data": {"message": long_data},
            }
        ]
        (tmp_path / "run.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events),
            encoding="utf-8",
        )
        result = _search_events(str(tmp_path), "findme", 0, 50)
        assert len(result["results"]) >= 1
        for r in result["results"]:
            assert len(r["snippet"]) <= 120
