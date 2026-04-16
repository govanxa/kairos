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
_CSP_HEADER: str = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
)

# Safe run ID pattern: only allow alphanumeric, hyphens, underscores
# Rejects: .., /, \, %, spaces, and other traversal patterns
_SAFE_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# ---------------------------------------------------------------------------
# Embedded single-page HTML dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_HTML: str = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kairos Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  .header { background: #1e293b; border-bottom: 1px solid #334155;
            padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.25rem; font-weight: 700; color: #f8fafc; }
  .header span { font-size: 0.75rem; color: #94a3b8; background: #0f172a;
                 padding: 2px 8px; border-radius: 9999px; }
  .main { padding: 24px; }
  .panel { background: #1e293b; border: 1px solid #334155; border-radius: 8px;
           margin-bottom: 24px; }
  .panel-header { padding: 14px 20px; border-bottom: 1px solid #334155;
                  font-size: 0.875rem; font-weight: 600; color: #94a3b8;
                  text-transform: uppercase; letter-spacing: 0.05em; }
  table { width: 100%; border-collapse: collapse; }
  th { padding: 10px 20px; text-align: left; font-size: 0.75rem;
       font-weight: 600; color: #64748b; text-transform: uppercase;
       letter-spacing: 0.05em; border-bottom: 1px solid #334155; }
  td { padding: 12px 20px; font-size: 0.875rem; border-bottom: 1px solid #1e293b; }
  tr:last-child td { border-bottom: none; }
  tr.clickable:hover { background: #263548; cursor: pointer; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 9999px;
           font-size: 0.75rem; font-weight: 600; }
  .badge-complete { background: #14532d; color: #86efac; }
  .badge-failed   { background: #450a0a; color: #fca5a5; }
  .badge-incomplete { background: #1c1917; color: #a8a29e; }
  .badge-other    { background: #1c1917; color: #a8a29e; }
  .event-list { list-style: none; padding: 16px 20px; }
  .event-list li { display: flex; gap: 12px; padding: 6px 0;
                   border-bottom: 1px solid #1e293b; font-size: 0.8125rem; }
  .event-list li:last-child { border-bottom: none; }
  .evt-ts { color: #475569; min-width: 86px; }
  .evt-type { color: #7dd3fc; min-width: 180px; }
  .evt-data { color: #94a3b8; word-break: break-all; }
  .back-btn { background: #334155; border: none; color: #e2e8f0; padding: 8px 16px;
              border-radius: 6px; cursor: pointer; margin-bottom: 16px;
              font-size: 0.875rem; }
  .back-btn:hover { background: #475569; }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
                  gap: 1px; background: #334155; }
  .summary-cell { background: #1e293b; padding: 16px 20px; }
  .summary-cell .label { font-size: 0.7rem; color: #64748b; text-transform: uppercase;
                         letter-spacing: 0.05em; margin-bottom: 4px; }
  .summary-cell .value { font-size: 1.25rem; font-weight: 700; color: #f8fafc; }
  #status-bar { padding: 10px 24px; font-size: 0.8rem; color: #64748b;
                background: #1e293b; border-top: 1px solid #334155;
                position: fixed; bottom: 0; left: 0; right: 0; }
  .empty { padding: 40px 20px; text-align: center; color: #475569; font-size: 0.875rem; }
</style>
</head>
<body>
<div class="header">
  <h1>Kairos Dashboard</h1>
  <span id="run-count">loading…</span>
</div>
<div class="main" id="app">
  <div class="empty">Loading run history…</div>
</div>
<div id="status-bar">Connecting…</div>
<script>
(function() {
  // Extract auth token from the current URL query string
  const params = new URLSearchParams(window.location.search);
  const TOKEN = params.get('token') || '';

  function apiUrl(path) {
    return TOKEN ? path + '?token=' + encodeURIComponent(TOKEN) : path;
  }

  function statusBadge(status) {
    const cls = status === 'complete' ? 'badge-complete'
              : status === 'failed'   ? 'badge-failed'
              : status === 'incomplete' ? 'badge-incomplete'
              : 'badge-other';
    return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtDuration(ms) {
    if (!ms) return '—';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(2) + 's';
  }

  function fmtTs(ts) {
    if (!ts) return '—';
    try { return new Date(ts).toLocaleString(); } catch(e) { return ts; }
  }

  function fmtTsShort(ts) {
    if (!ts) return '—';
    try { return new Date(ts).toLocaleTimeString(); } catch(e) { return ts; }
  }

  async function fetchJson(path) {
    const resp = await fetch(apiUrl(path));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }

  function showRunList(runs) {
    const app = document.getElementById('app');
    document.getElementById('run-count').textContent =
      runs.length + ' run' + (runs.length !== 1 ? 's' : '');

    if (runs.length === 0) {
      app.innerHTML = '<div class="panel"><div class="empty">' +
        'No runs found. Run a workflow with <code>--log-format=jsonl</code> to populate the dashboard.' +
        '</div></div>';
      return;
    }

    let rows = '';
    for (const run of runs) {
      rows += '<tr class="clickable" data-run-id="' + esc(run.run_id) + '">' +
        '<td><code style="font-size:0.75rem;color:#94a3b8">' + esc((run.run_id||'').slice(0,8)) + '</code></td>' +
        '<td>' + esc(run.workflow_name || '—') + '</td>' +
        '<td>' + statusBadge(run.status || 'unknown') + '</td>' +
        '<td>' + esc((run.completed_steps||0) + '/' + (run.total_steps||0)) + '</td>' +
        '<td>' + fmtDuration(run.duration_ms) + '</td>' +
        '<td style="color:#64748b;font-size:0.75rem">' + fmtTs(run.started_at) + '</td>' +
        '</tr>';
    }

    app.innerHTML =
      '<div class="panel">' +
      '<div class="panel-header">Run History</div>' +
      '<table>' +
      '<thead><tr>' +
        '<th>Run ID</th><th>Workflow</th><th>Status</th>' +
        '<th>Steps</th><th>Duration</th><th>Started</th>' +
      '</tr></thead>' +
      '<tbody>' + rows + '</tbody>' +
      '</table></div>';

    document.querySelectorAll('tr.clickable').forEach(function(row) {
      row.addEventListener('click', function() {
        const runId = row.getAttribute('data-run-id');
        if (runId) showRunDetail(runId);
      });
    });
  }

  function showRunDetail(runId) {
    const app = document.getElementById('app');
    app.innerHTML = '<div class="empty">Loading run ' + esc(runId) + '…</div>';

    fetchJson('/api/runs/' + encodeURIComponent(runId))
      .then(function(data) {
        const summary = data.summary || {};
        const events = data.events || [];

        const summaryHtml =
          '<div class="panel" style="margin-bottom:16px">' +
          '<div class="panel-header">Run Summary — ' + esc(runId.slice(0,8)) + '</div>' +
          '<div class="summary-grid">' +
            '<div class="summary-cell"><div class="label">Status</div>' +
              '<div class="value">' + statusBadge(summary.status || 'unknown') + '</div></div>' +
            '<div class="summary-cell"><div class="label">Workflow</div>' +
              '<div class="value" style="font-size:1rem">' + esc(summary.workflow_name||'?') + '</div></div>' +
            '<div class="summary-cell"><div class="label">Duration</div>' +
              '<div class="value">' + fmtDuration(summary.duration_ms) + '</div></div>' +
            '<div class="summary-cell"><div class="label">Steps</div>' +
              '<div class="value">' + esc((summary.completed_steps||0)+'/'+  (summary.total_steps||0)) + '</div></div>' +
          '</div></div>';

        let evtItems = '';
        for (const evt of events) {
          const ts = fmtTsShort(evt.timestamp);
          const data = evt.data || {};
          const dataStr = Object.keys(data).length
            ? JSON.stringify(data).slice(0, 120)
            : '';
          evtItems += '<li>' +
            '<span class="evt-ts">' + esc(ts) + '</span>' +
            '<span class="evt-type">' + esc(evt.event_type||'') + '</span>' +
            '<span class="evt-data">' + esc(dataStr) + '</span>' +
            '</li>';
        }

        app.innerHTML =
          '<button class="back-btn" id="back-btn">&#8592; Back to runs</button>' +
          summaryHtml +
          '<div class="panel">' +
          '<div class="panel-header">Events (' + events.length + ')</div>' +
          (evtItems
            ? '<ul class="event-list">' + evtItems + '</ul>'
            : '<div class="empty">No events recorded.</div>') +
          '</div>';

        document.getElementById('back-btn').addEventListener('click', loadRuns);
      })
      .catch(function(err) {
        app.innerHTML = '<div class="empty">Error loading run detail: ' + esc(String(err)) + '</div>' +
          '<button class="back-btn" onclick="history.back()">&#8592; Back</button>';
      });
  }

  function loadRuns() {
    const app = document.getElementById('app');
    app.innerHTML = '<div class="empty">Loading…</div>';
    fetchJson('/api/runs')
      .then(showRunList)
      .catch(function(err) {
        app.innerHTML = '<div class="empty" style="color:#fca5a5">Error loading runs: ' +
          esc(String(err)) + '</div>';
        document.getElementById('status-bar').textContent = 'Error: ' + String(err);
      });
  }

  // Boot
  document.getElementById('status-bar').textContent =
    TOKEN ? 'Authenticated — ' + window.location.host : 'No auth — ' + window.location.host;
  loadRuns();
})();
</script>
</body>
</html>"""


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
        """Serve the single-page HTML dashboard."""
        self._send_html(_DASHBOARD_HTML)

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
