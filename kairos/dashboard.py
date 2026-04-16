"""Kairos dashboard — localhost-only web UI for run history visualization.

Provides a read-only HTTP dashboard served on 127.0.0.1 only.  Run history
is loaded from .jsonl log files produced by the JSONLinesSink.

Security contracts (S17):
  S17.1: Binds to 127.0.0.1 exclusively. _BIND_HOST is a constant, not a
         parameter. There is no --host flag and no host parameter on DashboardServer.
  S17.2: Token authentication required for all endpoints except /api/health.
         Token checked via ?token= query param or Authorization: Bearer header.
         403 returned on authentication failure.
  S17.3: --no-auth sets auth_token=None. All requests pass when token is None.
         _print_noauth_warning() emits a warning to stderr.
  S17.4: Content-Security-Policy and X-Content-Type-Options: nosniff on EVERY
         response, including errors (403, 404, 405).
  S17.5: All non-GET methods return 405 Method Not Allowed with Allow: GET header.
  S17.6: Never imports kairos.state or kairos.logger. Reads pre-redacted .jsonl
         files only. Uses json.loads() only — no dynamic code execution.
  S17.7: Run IDs validated before use in file operations. Path traversal rejected.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PORT: int = 8420
_BIND_HOST: str = "127.0.0.1"
_TOKEN_LENGTH: int = 32

# v0.4.4: 'unsafe-inline' removed from script-src — JS now loads from
# /static/app.js (same-origin), so inline scripts are no longer needed.
# style-src keeps 'unsafe-inline' because JS sets inline styles on DOM elements.
_CSP_HEADER: str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"

# Safe run ID pattern: only allow alphanumeric, hyphens, underscores
# Rejects: .., /, \, %, spaces, and other traversal patterns
_SAFE_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# ---------------------------------------------------------------------------
# Static UI files — loaded once at import time (not per-request)
# ---------------------------------------------------------------------------

# _UI_DIR is the dashboard_ui/ package directory sitting next to this file.
_UI_DIR: Path = Path(__file__).parent / "dashboard_ui"
_INDEX_HTML: str = (_UI_DIR / "index.html").read_text(encoding="utf-8")
_STYLES_CSS: str = (_UI_DIR / "styles.css").read_text(encoding="utf-8")
_APP_JS: str = (_UI_DIR / "app.js").read_text(encoding="utf-8")

# Backward-compatibility alias so any existing code referencing _DASHBOARD_HTML still works.
_DASHBOARD_HTML: str = _INDEX_HTML

# Allowed static file paths — restricts /static/ route to known filenames only.
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/static/styles.css": ("text/css; charset=utf-8", _STYLES_CSS),
    "/static/app.js": ("text/javascript; charset=utf-8", _APP_JS),
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def generate_token() -> str:
    """Generate a cryptographically secure random URL-safe token.

    Returns:
        A URL-safe base64-encoded string of at least 32 characters.
    """
    return secrets.token_urlsafe(_TOKEN_LENGTH)


def _print_noauth_warning() -> None:
    """Print a security warning to stderr when --no-auth mode is active.

    This function is called by start_dashboard() when no_auth=True.
    """
    print(  # noqa: T20
        "WARNING: --no-auth mode is active. "
        "Dashboard is accessible without authentication. "
        "Do not use this in shared or public environments.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Data loading — pure functions, no dependency on kairos.state / kairos.logger
# ---------------------------------------------------------------------------


def _list_jsonl_files(log_dir: Path) -> list[Path]:
    """Glob all .jsonl files in *log_dir*, sorted newest-modified first.

    Args:
        log_dir: Directory to search for .jsonl files.

    Returns:
        List of Path objects sorted by modification time, newest first.
    """
    files = list(log_dir.glob("*.jsonl"))
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files


def _read_events(file_path: Path) -> list[dict[str, object]]:
    """Parse a .jsonl file and return a list of valid event dicts.

    Each line is parsed independently with json.loads().  Malformed lines and
    non-dict lines are silently skipped.  Empty files return an empty list.

    Unlike the CLI's _inspect_read_events(), this function does NOT raise on
    empty files — the dashboard treats them as zero-event runs.

    Args:
        file_path: Path to a .jsonl file.

    Returns:
        List of parsed event dicts (may be empty).
    """
    events: list[dict[str, object]] = []
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except OSError:
        return events

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                events.append(cast(dict[str, object], parsed))
        except json.JSONDecodeError:
            pass  # silently skip malformed lines

    return events


def _extract_summary(events: list[dict[str, object]]) -> dict[str, object]:
    """Extract a run summary dict from a list of events.

    Mirrors the logic of cli._inspect_extract_summary() but is independent
    of that module.

    Args:
        events: List of parsed event dicts from a .jsonl file.

    Returns:
        Summary dict with keys: workflow_name, run_id, status, started_at,
        duration_ms, total_steps, completed_steps, failed_steps, skipped_steps.
    """
    workflow_name: str = "unknown"
    run_id: str = ""
    status: str = "incomplete"
    started_at: str = ""
    duration_ms: float = 0.0
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0

    for event in events:
        etype = event.get("event_type", "")
        raw_data: object = event.get("data") or {}
        data: dict[str, object] = (
            cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        )

        if etype == "workflow_start":
            workflow_name = str(data.get("workflow_name", "unknown"))
            run_id = str(data.get("run_id", ""))
            started_at = str(event.get("timestamp", ""))
            total_steps = int(str(data.get("total_steps", 0)))

        elif etype == "workflow_complete":
            status = str(data.get("status", "unknown"))
            raw_summary: object = data.get("summary")
            if isinstance(raw_summary, dict):
                sd = cast(dict[str, object], raw_summary)
                total_steps = int(str(sd.get("total_steps", total_steps)))
                completed_steps = int(str(sd.get("completed_steps", 0)))
                failed_steps = int(str(sd.get("failed_steps", 0)))
                skipped_steps = int(str(sd.get("skipped_steps", 0)))
                duration_ms = float(str(sd.get("total_duration_ms", 0.0)))

    if status == "incomplete":
        seen_completed: set[str] = set()
        seen_failed: set[str] = set()
        seen_skipped: set[str] = set()
        for event in events:
            etype = event.get("event_type", "")
            step_id = event.get("step_id")
            if step_id is None:
                continue
            if etype == "step_complete":
                seen_completed.add(str(step_id))
            elif etype == "step_fail":
                seen_failed.add(str(step_id))
            elif etype == "step_skip":
                seen_skipped.add(str(step_id))
        completed_steps = len(seen_completed)
        failed_steps = len(seen_failed)
        skipped_steps = len(seen_skipped)

    return {
        "workflow_name": workflow_name,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "total_steps": total_steps,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "skipped_steps": skipped_steps,
    }


def _list_runs(log_dir: str) -> list[dict[str, object]]:
    """Aggregate run summaries from all .jsonl files in *log_dir*.

    Files are read and summarized; results are sorted newest-modified first.

    Args:
        log_dir: Directory path to search for .jsonl run log files.

    Returns:
        List of summary dicts, one per .jsonl file, newest first.
    """
    dir_path = Path(log_dir)
    files = _list_jsonl_files(dir_path)
    runs: list[dict[str, object]] = []
    for file_path in files:
        events = _read_events(file_path)
        summary = _extract_summary(events)
        runs.append(summary)
    return runs


def _get_run_events(log_dir: str, run_id: str) -> list[dict[str, object]] | None:
    """Find and return all events for a specific run_id.

    Scans .jsonl files newest-first, looking for one whose workflow_start event
    contains the matching run_id.

    Security: run_id is validated against _SAFE_RUN_ID_RE before use.
    Returns None for invalid or traversal run_ids.

    Args:
        log_dir: Directory path to search.
        run_id: The exact run_id to find.

    Returns:
        List of events for the matching run, or None if not found.
    """
    # S17.7: Reject any run_id that could cause path traversal
    if not run_id or not _SAFE_RUN_ID_RE.match(run_id):
        return None

    dir_path = Path(log_dir)
    for file_path in _list_jsonl_files(dir_path):
        events = _read_events(file_path)
        for event in events:
            if event.get("event_type") == "workflow_start":
                raw_data = event.get("data")
                if isinstance(raw_data, dict):
                    found_id = str(cast(dict[str, object], raw_data).get("run_id", ""))
                    if found_id == run_id:
                        return events
    return None


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Kairos dashboard.

    Routes GET requests to appropriate handlers.  All other methods return
    405 Method Not Allowed.  Every response includes CSP and nosniff headers.

    The server instance is accessible via self.server, which must be a
    DashboardServer providing .auth_token and .log_dir attributes.
    """

    # ---------- auth ----------

    def _check_auth(self) -> bool:
        """Validate the request token.

        Checks ?token= query param first, then Authorization: Bearer header.
        If server.auth_token is None (--no-auth mode), always returns True.

        Returns:
            True if authentication passes, False otherwise.
            Sends a 403 JSON response and returns False on failure.
        """
        server = cast("DashboardServer", self.server)
        if server.auth_token is None:
            return True

        # Check query param
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        token_params = params.get("token", [])
        if token_params and hmac.compare_digest(token_params[0], server.auth_token):
            return True

        # Check Authorization: Bearer header
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            bearer_token = auth_header[len("Bearer ") :].strip()
            if hmac.compare_digest(bearer_token, server.auth_token):
                return True

        self._send_error_json(403, "Forbidden: valid token required")
        return False

    # ---------- common headers ----------

    def _set_common_headers(self, content_type: str) -> None:
        """Set security headers and Content-Type on the current response.

        Must be called after send_response() and before end_headers().

        Args:
            content_type: The MIME type to set (e.g. 'application/json').
        """
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Type", content_type)

    # ---------- response helpers ----------

    def _send_static(self, path: str) -> None:
        """Serve a static UI asset from the pre-loaded _STATIC_FILES dict.

        Only allows paths explicitly listed in _STATIC_FILES.  Any other path
        — including traversal attempts — returns 404.

        Args:
            path: The URL path portion (e.g. '/static/styles.css').
        """
        entry = _STATIC_FILES.get(path)
        if entry is None:
            self._send_error_json(404, "Not found")
            return
        content_type, body_str = entry
        body = body_str.encode("utf-8")
        self.send_response(200)
        self._set_common_headers(content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: object, status: int = 200) -> None:
        """Serialize *data* to JSON and send as a response.

        Args:
            data: JSON-serializable object to send.
            status: HTTP status code.
        """
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self._set_common_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        """Send *html* as an HTML response.

        Args:
            html: HTML string to send.
            status: HTTP status code.
        """
        body = html.encode("utf-8")
        self.send_response(status)
        self._set_common_headers("text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        """Send a JSON error response.

        Args:
            status: HTTP error status code.
            message: Human-readable error description.
        """
        self._send_json({"error": message}, status=status)

    # ---------- route handlers ----------

    def _handle_index(self) -> None:
        """Serve the single-page HTML dashboard (loaded from dashboard_ui/index.html)."""
        self._send_html(_INDEX_HTML)

    def _handle_api_runs(self) -> None:
        """List all runs as JSON (GET /api/runs)."""
        server = cast("DashboardServer", self.server)
        runs = _list_runs(server.log_dir)
        self._send_json(runs)

    def _handle_api_run_detail(self, run_id: str) -> None:
        """Return events for a single run (GET /api/runs/<run_id>).

        Args:
            run_id: The run identifier extracted from the URL path.
        """
        server = cast("DashboardServer", self.server)

        # S17.7: validate the run_id before any file operations
        if not run_id or not _SAFE_RUN_ID_RE.match(run_id):
            self._send_error_json(404, "Run not found")
            return

        events = _get_run_events(server.log_dir, run_id)
        if events is None:
            self._send_error_json(404, f"Run not found: {run_id}")
            return

        summary = _extract_summary(events)
        self._send_json({"run_id": run_id, "summary": summary, "events": events})

    def _handle_api_health(self) -> None:
        """Health check — no authentication required (GET /api/health)."""
        self._send_json({"status": "ok"})

    # ---------- HTTP method dispatch ----------

    def do_GET(self) -> None:  # noqa: N802
        """Route GET requests to the appropriate handler."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Health check — no auth required
        if path == "/api/health":
            self._handle_api_health()
            return

        # Static assets — no auth required (CSS/JS contain no sensitive data;
        # the browser loads them via <link>/<script> tags which cannot carry
        # the auth token. The API endpoints that serve actual run data still
        # require auth.)
        if path.startswith("/static/"):
            self._send_static(path)
            return

        # Favicon — browsers request this automatically. Return 204 No Content
        # to suppress the 403 error in the browser console.
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # All other endpoints require authentication
        if not self._check_auth():
            return

        match path:
            case "/":
                self._handle_index()
            case "/api/runs":
                self._handle_api_runs()
            case _ if path.startswith("/api/runs/"):
                run_id = path[len("/api/runs/") :]
                # Strip any trailing slashes
                run_id = run_id.rstrip("/")
                self._handle_api_run_detail(run_id)
            case _:
                self._send_error_json(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        """Return 405 Method Not Allowed for POST requests."""
        self._send_method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        """Return 405 Method Not Allowed for PUT requests."""
        self._send_method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        """Return 405 Method Not Allowed for DELETE requests."""
        self._send_method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        """Return 405 Method Not Allowed for PATCH requests."""
        self._send_method_not_allowed()

    def do_HEAD(self) -> None:  # noqa: N802
        """Return 405 Method Not Allowed for HEAD requests."""
        self._send_method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Return 405 Method Not Allowed for OPTIONS requests."""
        self._send_method_not_allowed()

    def _send_method_not_allowed(self) -> None:
        """Send 405 Method Not Allowed with Allow: GET header."""
        body = json.dumps({"error": "Method not allowed"}).encode("utf-8")
        self.send_response(405)
        self._set_common_headers("application/json; charset=utf-8")
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Suppress default HTTP request logging to stderr."""
        # Intentionally empty — all request logging is suppressed


# ---------------------------------------------------------------------------
# DashboardServer
# ---------------------------------------------------------------------------


class DashboardServer(HTTPServer):
    """HTTP server for the Kairos dashboard.

    Always binds to 127.0.0.1 (S17.1).  There is no host parameter.

    Args:
        port: TCP port to listen on. Use 0 to bind to a random available port.
        log_dir: Directory containing .jsonl run log files.
        auth_token: Token required for authenticated endpoints, or None for
            no-auth mode (S17.2, S17.3).
    """

    def __init__(self, port: int, log_dir: str, auth_token: str | None) -> None:
        self.log_dir = log_dir
        self.auth_token = auth_token
        # S17.1: _BIND_HOST is always 127.0.0.1 — not a parameter
        super().__init__((_BIND_HOST, port), DashboardHandler)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def start_dashboard(  # pragma: no cover
    port: int,
    log_dir: str,
    no_auth: bool,
    open_browser: bool,
) -> None:
    """Start the dashboard HTTP server and block until interrupted.

    This function blocks until a KeyboardInterrupt is received.  It is the
    CLI entry point invoked by ``kairos dashboard`` and is excluded from
    coverage measurement because it cannot be unit-tested without running
    a real blocking server loop.

    Args:
        port: TCP port to listen on.
        log_dir: Directory containing .jsonl run log files.
        no_auth: When True, no token is required. Prints a warning to stderr.
        open_browser: When True, opens the dashboard URL in the default browser.
    """
    auth_token: str | None
    if no_auth:
        auth_token = None
        _print_noauth_warning()
    else:
        auth_token = generate_token()

    server = DashboardServer(port=port, log_dir=log_dir, auth_token=auth_token)
    actual_port = server.server_address[1]

    if auth_token is not None:
        url = f"http://{_BIND_HOST}:{actual_port}?token={auth_token}"
    else:
        url = f"http://{_BIND_HOST}:{actual_port}"

    print(f"Dashboard running at {url}", file=sys.stdout)  # noqa: T20

    if open_browser:
        import webbrowser

        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\nDashboard stopped.", file=sys.stdout)  # noqa: T20
