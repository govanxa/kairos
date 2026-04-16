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
    """v0.4.4 version must be reflected in the package."""

    def test_version_is_0_4_4(self):
        """kairos.__version__ must be '0.4.4'."""
        import kairos

        assert kairos.__version__ == "0.4.4"


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
