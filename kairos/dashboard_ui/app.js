// ============================================================
// Kairos Dashboard — app.js
// All JavaScript in a single IIFE. Zero external dependencies.
// ============================================================

(function () {
  'use strict';

  // ============================================================
  // === State ===
  // ============================================================

  let currentView = 'run-list';
  let currentRunId = null;
  let runs = [];
  let autoRefreshTimer = null;
  let autoRefreshEnabled = false;
  let filters = { status: 'all', workflow: 'all', search: '' };
  let expandedEvents = new Set();   // tracks expanded event row indices
  let expandCounter = 0;            // monotonic counter for unique expand IDs

  // ============================================================
  // === Constants ===
  // ============================================================

  const DEFAULT_INTERVAL_MS = 5000;

  // ============================================================
  // === API ===
  // ============================================================

  /** Extract auth token from the current URL query string. */
  const params = new URLSearchParams(window.location.search);
  const TOKEN = params.get('token') || '';

  function apiUrl(path) {
    return TOKEN ? path + '?token=' + encodeURIComponent(TOKEN) : path;
  }

  async function fetchJson(path) {
    const resp = await fetch(apiUrl(path));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }

  async function fetchRuns() {
    return fetchJson('/api/runs');
  }

  async function fetchRunDetail(runId) {
    return fetchJson('/api/runs/' + encodeURIComponent(runId));
  }

  // ============================================================
  // === Utilities ===
  // ============================================================

  /** HTML-escape a value before inserting into innerHTML. */
  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtDuration(ms) {
    if (!ms) return '\u2014';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(2) + 's';
  }

  function fmtTs(ts) {
    if (!ts) return '\u2014';
    try { return new Date(ts).toLocaleString(); } catch (e) { return ts; }
  }

  function fmtTsShort(ts) {
    if (!ts) return '\u2014';
    try { return new Date(ts).toLocaleTimeString(); } catch (e) { return ts; }
  }

  function statusBadge(status) {
    const cls = status === 'complete'   ? 'badge-complete'
              : status === 'failed'     ? 'badge-failed'
              : status === 'incomplete' ? 'badge-incomplete'
              : status === 'running'    ? 'badge-running'
              : status === 'skipped'   ? 'badge-skipped'
              : 'badge-other';
    return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
  }

  function statusGroupClass(status) {
    if (status === 'complete' || status === 'completed') return 'status-complete';
    if (status === 'failed') return 'status-failed';
    if (status === 'skipped') return 'status-skipped';
    if (status === 'running') return 'status-running';
    return '';
  }

  function debounce(fn, ms) {
    let timer;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  // ============================================================
  // === Icons ===
  // ============================================================

  function iconChevronRight() {
    return '<svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M6 4l4 4-4 4"/></svg>';
  }

  /** Workflow graph icon — used in empty states. */
  function iconWorkflowGraph() {
    return '<svg class="empty-icon" viewBox="0 0 64 64" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.5">' +
      '<rect x="20" y="4" width="24" height="12" rx="3"/>' +
      '<rect x="4" y="28" width="24" height="12" rx="3"/>' +
      '<rect x="36" y="28" width="24" height="12" rx="3"/>' +
      '<rect x="20" y="52" width="24" height="12" rx="3"/>' +
      '<line x1="32" y1="16" x2="16" y2="28"/>' +
      '<line x1="32" y1="16" x2="48" y2="28"/>' +
      '<line x1="16" y1="40" x2="32" y2="52"/>' +
      '<line x1="48" y1="40" x2="32" y2="52"/>' +
      '</svg>';
  }

  // ============================================================
  // === JSON Coloring (Enhancement 1) ===
  // ============================================================

  /**
   * Recursively colorize a JSON value into HTML.
   * All string values go through esc() before insertion into HTML.
   *
   * @param {*} value  — any JSON-serializable value
   * @param {number} indent  — current indentation level
   * @param {number} depth  — recursion depth guard (max 10)
   * @returns {string} HTML string with syntax coloring spans
   */
  function colorizeJson(value, indent, depth) {
    indent = indent || 0;
    depth = depth || 0;

    if (depth > 10) {
      return '<span class="json-string">"[max depth]"</span>';
    }

    const pad = '  '.repeat(indent);
    const padClose = indent > 0 ? '  '.repeat(indent - 1) : '';

    if (value === null) {
      return '<span class="json-null">null</span>';
    }

    if (typeof value === 'boolean') {
      return '<span class="json-boolean">' + esc(String(value)) + '</span>';
    }

    if (typeof value === 'number') {
      return '<span class="json-number">' + esc(String(value)) + '</span>';
    }

    if (typeof value === 'string') {
      let display = value;
      if (display.length > 500) display = display.slice(0, 500) + '...';
      return '<span class="json-string">"' + esc(display) + '"</span>';
    }

    if (Array.isArray(value)) {
      if (value.length === 0) {
        return '<span class="json-bracket">[]</span>';
      }
      const items = value.map(function (item) {
        return pad + '  ' + colorizeJson(item, indent + 1, depth + 1);
      });
      return (
        '<span class="json-bracket">[</span>\n' +
        items.join('<span class="json-bracket">,</span>\n') + '\n' +
        pad + '<span class="json-bracket">]</span>'
      );
    }

    if (typeof value === 'object') {
      const keys = Object.keys(value);
      if (keys.length === 0) {
        return '<span class="json-bracket">{}</span>';
      }
      const pairs = keys.map(function (k) {
        return (
          pad + '  ' +
          '<span class="json-key">"' + esc(k) + '"</span>' +
          '<span class="json-bracket">: </span>' +
          colorizeJson(value[k], indent + 1, depth + 1)
        );
      });
      return (
        '<span class="json-bracket">{</span>\n' +
        pairs.join('<span class="json-bracket">,</span>\n') + '\n' +
        pad + '<span class="json-bracket">}</span>'
      );
    }

    return esc(String(value));
  }

  // ============================================================
  // === Components ===
  // ============================================================

  /**
   * Render the run list table (with filter bar).
   * @param {Array} allRuns — complete run list from API
   * @param {Object} f — current filters {status, workflow, search}
   */
  function renderRunTable(allRuns, f) {
    const filtered = applyFilters(allRuns, f);

    // Build workflow dropdown options
    const workflows = Array.from(new Set(allRuns.map(r => r.workflow_name || ''))).filter(Boolean).sort();
    const wfOptions = '<option value="all">All workflows</option>' +
      workflows.map(w => '<option value="' + esc(w) + '"' + (f.workflow === w ? ' selected' : '') + '>' + esc(w) + '</option>').join('');

    const statusOptions = ['all', 'complete', 'failed', 'incomplete'].map(s =>
      '<option value="' + s + '"' + (f.status === s ? ' selected' : '') + '>' +
      (s === 'all' ? 'All statuses' : s.charAt(0).toUpperCase() + s.slice(1)) + '</option>'
    ).join('');

    const isFiltered = filtered.length < allRuns.length;
    const barClass = 'filter-bar' + (isFiltered ? ' filters-active' : '');

    // Build active filter badges
    let badges = '';
    if (f.status !== 'all') {
      badges += '<span class="filter-badge">' +
        'Status: ' + esc(f.status.charAt(0).toUpperCase() + f.status.slice(1)) +
        ' <button class="filter-badge-remove" data-filter-clear="status" aria-label="Remove status filter">\u00d7</button>' +
        '</span>';
    }
    if (f.workflow !== 'all') {
      badges += '<span class="filter-badge">' +
        esc(f.workflow) +
        ' <button class="filter-badge-remove" data-filter-clear="workflow" aria-label="Remove workflow filter">\u00d7</button>' +
        '</span>';
    }
    if (f.search) {
      badges += '<span class="filter-badge">' +
        '\u201c' + esc(f.search) + '\u201d' +
        ' <button class="filter-badge-remove" data-filter-clear="search" aria-label="Remove search filter">\u00d7</button>' +
        '</span>';
    }
    const badgesHtml = badges ? '<div class="filter-badges">' + badges + '</div>' : '';

    const filterBar =
      '<div class="' + barClass + '">' +
      '<select id="filter-status" aria-label="Filter by status">' + statusOptions + '</select>' +
      '<select id="filter-workflow" aria-label="Filter by workflow">' + wfOptions + '</select>' +
      '<input type="text" id="filter-search" aria-label="Search runs" placeholder="Search by name or run ID\u2026" value="' + esc(f.search) + '">' +
      badgesHtml +
      '<span class="filter-count">Showing ' + filtered.length + ' of ' + allRuns.length + ' runs</span>' +
      (isFiltered ? '<button class="filter-clear" id="filter-clear">Clear all</button>' : '') +
      '</div>';

    if (filtered.length === 0) {
      return (
        '<div class="panel">' +
        '<div class="panel-header">Run History</div>' +
        filterBar +
        '<div class="empty-structured">' +
        '<div class="empty-heading">No matching runs</div>' +
        '<div class="empty-text">No runs match the current filters. Try adjusting your search or status filter.</div>' +
        '<div class="empty-action"><button id="filter-clear-empty">Clear all filters</button></div>' +
        '</div>' +
        '</div>'
      );
    }

    let rows = '';
    for (const run of filtered) {
      rows +=
        '<tr class="clickable" data-run-id="' + esc(run.run_id) + '" tabindex="0" role="button">' +
        '<td><span class="mono">' + esc((run.run_id || '').slice(0, 8)) + '</span></td>' +
        '<td>' + esc(run.workflow_name || '\u2014') + '</td>' +
        '<td>' + statusBadge(run.status || 'unknown') + '</td>' +
        '<td><span class="mono data-metric">' + esc((run.completed_steps || 0) + '/' + (run.total_steps || 0)) + '</span></td>' +
        '<td><span class="mono data-metric">' + fmtDuration(run.duration_ms) + '</span></td>' +
        '<td><span class="mono ts-cell">' + fmtTs(run.started_at) + '</span></td>' +
        '</tr>';
    }

    return (
      '<div class="panel">' +
      '<div class="panel-header">Run History</div>' +
      filterBar +
      '<table>' +
      '<thead><tr>' +
      '<th>Run ID</th><th>Workflow</th><th>Status</th>' +
      '<th>Steps</th><th>Duration</th><th>Started</th>' +
      '</tr></thead>' +
      '<tbody>' + rows + '</tbody>' +
      '</table>' +
      '</div>'
    );
  }

  /**
   * Apply filters to a run list.
   * @param {Array} allRuns
   * @param {Object} f — {status, workflow, search}
   * @returns {Array} filtered subset
   */
  function applyFilters(allRuns, f) {
    return allRuns.filter(function (run) {
      if (f.status !== 'all' && run.status !== f.status) return false;
      if (f.workflow !== 'all' && run.workflow_name !== f.workflow) return false;
      if (f.search) {
        const q = f.search.toLowerCase();
        const name = (run.workflow_name || '').toLowerCase();
        const rid = (run.run_id || '').toLowerCase();
        if (!name.includes(q) && !rid.includes(q)) return false;
      }
      return true;
    });
  }

  /**
   * Render the event timeline grouped by step (Enhancement 2).
   * @param {Array} events
   * @returns {string} HTML
   */
  function renderStepGroups(events) {
    // Separate workflow-level events (step_id === null/undefined) from step events
    const workflowEvents = [];
    const stepMap = new Map(); // step_id -> {events, status}

    for (const evt of events) {
      const sid = evt.step_id;
      if (!sid) {
        workflowEvents.push(evt);
      } else {
        if (!stepMap.has(sid)) stepMap.set(sid, { events: [], status: '' });
        stepMap.get(sid).events.push(evt);
        // Track step status from step_complete / step_fail / step_skip
        const et = evt.event_type || '';
        if (et === 'step_complete') stepMap.get(sid).status = 'complete';
        else if (et === 'step_fail') {
          if (stepMap.get(sid).status !== 'complete') stepMap.get(sid).status = 'failed';
        }
        else if (et === 'step_skip') {
          if (!stepMap.get(sid).status) stepMap.get(sid).status = 'skipped';
        }
      }
    }

    // Render workflow-level events (not collapsible)
    let html = '';
    if (workflowEvents.length > 0) {
      html += '<ul class="event-list workflow-level-events">';
      for (const evt of workflowEvents) {
        html += renderEventRow(evt, 'wf');
      }
      html += '</ul>';
    }

    // Render step groups
    if (stepMap.size > 0) {
      html += '<ul class="step-groups">';
      stepMap.forEach(function (group, stepId) {
        const status = group.status || 'incomplete';
        const isFailed = status === 'failed';
        const headerClass = 'step-group-header ' + statusGroupClass(status);
        const chevronClass = 'group-chevron' + (isFailed ? ' expanded' : '');
        const eventsClass = 'step-group-events' + (isFailed ? ' visible' : '');

        // Derive step duration from start/complete timestamps
        let duration = '';
        const startEvt = group.events.find(e => e.event_type === 'step_start');
        const endEvt = group.events.find(e => e.event_type === 'step_complete' || e.event_type === 'step_fail');
        if (startEvt && endEvt) {
          try {
            const ms = new Date(endEvt.timestamp) - new Date(startEvt.timestamp);
            if (!isNaN(ms)) duration = fmtDuration(ms);
          } catch (e) { /* ignore */ }
        }

        html +=
          '<li class="step-group">' +
          '<div class="' + headerClass + '" data-step-id="' + esc(stepId) + '">' +
          '<span class="' + chevronClass + '">' + iconChevronRight() + '</span>' +
          '<span class="group-name">' + esc(stepId) + '</span>' +
          statusBadge(status) +
          (duration ? '<span class="group-duration">' + esc(duration) + '</span>' : '') +
          '<span class="group-count">(' + group.events.length + ' events)</span>' +
          '</div>' +
          '<ul class="' + eventsClass + '">';
        for (const evt of group.events) {
          html += renderEventRow(evt, 'step-' + stepId);
        }
        html += '</ul></li>';
      });
      html += '</ul>';
    }

    return html || '<div class="empty">No events recorded.</div>';
  }

  /**
   * Render a single expandable event row.
   * @param {Object} evt
   * @param {string} groupKey — used to build unique row IDs
   * @returns {string} HTML — one <li> element
   */
  function renderEventRow(evt, groupKey) {
    const ts = fmtTsShort(evt.timestamp);
    const et = evt.event_type || '';
    const data = evt.data || {};
    const hasData = Object.keys(data).length > 0;

    const dataJson = hasData ? JSON.stringify(data) : '';
    const dataStr = dataJson.length > 120
      ? dataJson.slice(0, 120) + '\u2026'
      : dataJson;

    const rowCls = (et === 'step_fail' || et === 'validation_fail')
      ? ' evt-error'
      : (et === 'step_retry')
      ? ' evt-warn'
      : '';

    const expandId = 'expand-' + esc(groupKey) + '-' + esc(et) + '-' + (++expandCounter);

    return (
      '<li class="event-row' + rowCls + '" data-expand-id="' + expandId + '">' +
      '<span class="evt-chevron" aria-hidden="true">' + iconChevronRight() + '</span>' +
      '<span class="evt-ts">' + esc(ts) + '</span>' +
      '<span class="evt-type">' + esc(et) + '</span>' +
      '<span class="evt-data">' + esc(dataStr) + '</span>' +
      '</li>' +
      '<li class="event-expanded" id="' + expandId + '">' +
      '<pre>' + (hasData ? colorizeJson(data, 0, 0) : '<span class="text-faint">no data</span>') + '</pre>' +
      '</li>'
    );
  }

  // ============================================================
  // === Views ===
  // ============================================================

  function showRunList(runList) {
    currentView = 'run-list';
    currentRunId = null;
    runs = runList;
    document.title = 'Kairos Dashboard';

    const app = document.getElementById('app');
    document.getElementById('run-count').textContent =
      runList.length + ' run' + (runList.length !== 1 ? 's' : '');

    if (runList.length === 0) {
      app.innerHTML =
        '<div class="panel"><div class="empty-structured">' +
        iconWorkflowGraph() +
        '<div class="empty-heading">No runs yet</div>' +
        '<div class="empty-text">Run a workflow with logging enabled to see it here.</div>' +
        '<div class="empty-code">' +
        '<span class="cmd">kairos run</span> <span class="arg">my_workflow.py</span> ' +
        '<span class="flag">--log-format</span> jsonl ' +
        '<span class="flag">--log-file</span> ./logs' +
        '</div>' +
        '</div></div>';
      return;
    }

    app.innerHTML = renderRunTable(runList, filters);
    attachFilterListeners();
  }

  function showRunDetail(runId) {
    currentView = 'run-detail';
    currentRunId = runId;

    // Pause auto-refresh while viewing run detail
    const wasRefreshing = autoRefreshEnabled;
    if (wasRefreshing) stopAutoRefresh();

    const app = document.getElementById('app');
    app.innerHTML = '<div class="empty">Loading run ' + esc(runId.slice(0, 8)) + '\u2026</div>';

    fetchRunDetail(runId)
      .then(function (data) {
        const summary = data.summary || {};
        const events = data.events || [];

        const summaryHtml =
          '<div class="panel panel-detail">' +
          '<div class="panel-header">Run Summary \u2014 ' + esc(runId.slice(0, 8)) + '</div>' +
          '<div class="summary-grid">' +
          '<div class="summary-cell"><div class="label">Status</div>' +
          '<div class="value value-md">' + statusBadge(summary.status || 'unknown') + '</div></div>' +
          '<div class="summary-cell"><div class="label">Workflow</div>' +
          '<div class="value value-md">' + esc(summary.workflow_name || '?') + '</div></div>' +
          '<div class="summary-cell"><div class="label">Duration</div>' +
          '<div class="value">' + fmtDuration(summary.duration_ms) + '</div></div>' +
          '<div class="summary-cell"><div class="label">Steps</div>' +
          '<div class="value">' + esc((summary.completed_steps || 0) + '/' + (summary.total_steps || 0)) + '</div></div>' +
          '</div></div>';

        const wfName = summary.workflow_name || 'unknown';
        const shortId = runId.slice(0, 8);
        document.title = esc(wfName) + ' (' + shortId + ') \u2014 Kairos Dashboard';

        const eventsHtml = renderStepGroups(events);

        const navBar =
          '<div class="detail-nav">' +
          '<button class="back-btn" id="back-btn">\u2190 Back to runs</button>' +
          '<span class="detail-breadcrumb">' +
          '<span class="detail-wf-name">' + esc(wfName) + '</span>' +
          '<span class="detail-sep">\u00b7</span>' +
          '<span class="detail-run-id">' + esc(shortId) + '</span>' +
          '</span>' +
          '</div>';

        app.innerHTML =
          navBar +
          summaryHtml +
          '<div class="panel">' +
          '<div class="panel-header">Events (' + events.length + ')</div>' +
          eventsHtml +
          '</div>';

        document.getElementById('back-btn').addEventListener('click', function () {
          navigate('run-list', {});
          if (wasRefreshing) startAutoRefresh(getRefreshInterval());
        });
      })
      .catch(function (err) {
        app.innerHTML =
          '<div class="empty">Error loading run detail: ' + esc(String(err)) + '</div>' +
          '<button class="back-btn" id="back-btn-err">\u2190 Back</button>';
        const btn = document.getElementById('back-btn-err');
        if (btn) btn.addEventListener('click', function () { navigate('run-list', {}); });
      });
  }

  // ============================================================
  // === Filtering (Enhancement 3) ===
  // ============================================================

  function attachFilterListeners() {
    const statusSel = document.getElementById('filter-status');
    const workflowSel = document.getElementById('filter-workflow');
    const searchInput = document.getElementById('filter-search');
    const clearBtn = document.getElementById('filter-clear');

    const debouncedSearch = debounce(function (val) {
      filters.search = val;
      refreshRunListView();
    }, 200);

    if (statusSel) {
      statusSel.addEventListener('change', function () {
        filters.status = statusSel.value;
        refreshRunListView();
      });
    }
    if (workflowSel) {
      workflowSel.addEventListener('change', function () {
        filters.workflow = workflowSel.value;
        refreshRunListView();
      });
    }
    if (searchInput) {
      searchInput.addEventListener('input', function () {
        debouncedSearch(searchInput.value);
      });
    }
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        filters = { status: 'all', workflow: 'all', search: '' };
        refreshRunListView();
      });
    }
  }

  function refreshRunListView() {
    const filtered = applyFilters(runs, filters);

    // Update only the table body and count — do NOT rebuild the filter bar
    // (rebuilding destroys the focused input and loses cursor position).
    const tbody = document.querySelector('#app tbody');
    const countEl = document.querySelector('#app .filter-count');
    const clearEl = document.getElementById('filter-clear');

    if (tbody) {
      let rows = '';
      for (const run of filtered) {
        rows +=
          '<tr class="clickable" data-run-id="' + esc(run.run_id) + '" tabindex="0" role="button">' +
          '<td><span class="mono">' + esc((run.run_id || '').slice(0, 8)) + '</span></td>' +
          '<td>' + esc(run.workflow_name || '\u2014') + '</td>' +
          '<td>' + statusBadge(run.status || 'unknown') + '</td>' +
          '<td><span class="mono data-metric">' + esc((run.completed_steps || 0) + '/' + (run.total_steps || 0)) + '</span></td>' +
          '<td><span class="mono data-metric">' + fmtDuration(run.duration_ms) + '</span></td>' +
          '<td><span class="mono ts-cell">' + fmtTs(run.started_at) + '</span></td>' +
          '</tr>';
      }
      if (filtered.length === 0) {
        // Replace entire table area with structured empty message
        tbody.parentElement.outerHTML =
          '<div class="empty-structured">' +
          '<div class="empty-heading">No matching runs</div>' +
          '<div class="empty-text">No runs match the current filters.</div>' +
          '<div class="empty-action"><button id="filter-clear-empty">Clear all filters</button></div>' +
          '</div>';
      } else {
        tbody.innerHTML = rows;
      }
    }

    var isFiltered = filtered.length < runs.length;

    if (countEl) {
      countEl.textContent = 'Showing ' + filtered.length + ' of ' + runs.length + ' runs';
    }

    // Toggle filter bar active state
    var bar = document.querySelector('#app .filter-bar');
    if (bar) {
      if (isFiltered) {
        bar.classList.add('filters-active');
      } else {
        bar.classList.remove('filters-active');
      }
    }

    // Update badges
    var badgesContainer = document.querySelector('#app .filter-badges');
    if (badgesContainer) {
      var badges = '';
      if (filters.status !== 'all') {
        badges += '<span class="filter-badge">' +
          'Status: ' + esc(filters.status.charAt(0).toUpperCase() + filters.status.slice(1)) +
          ' <button class="filter-badge-remove" data-filter-clear="status" aria-label="Remove status filter">\u00d7</button>' +
          '</span>';
      }
      if (filters.workflow !== 'all') {
        badges += '<span class="filter-badge">' +
          esc(filters.workflow) +
          ' <button class="filter-badge-remove" data-filter-clear="workflow" aria-label="Remove workflow filter">\u00d7</button>' +
          '</span>';
      }
      if (filters.search) {
        badges += '<span class="filter-badge">' +
          '\u201c' + esc(filters.search) + '\u201d' +
          ' <button class="filter-badge-remove" data-filter-clear="search" aria-label="Remove search filter">\u00d7</button>' +
          '</span>';
      }
      badgesContainer.innerHTML = badges;
    }

    // Show/hide clear button
    if (clearEl) {
      clearEl.style.display = isFiltered ? '' : 'none';
    }
  }

  // ============================================================
  // === Auto-Refresh (Enhancement 4) ===
  // ============================================================

  function getRefreshInterval() {
    const sel = document.getElementById('refresh-interval');
    return sel ? parseInt(sel.value, 10) : DEFAULT_INTERVAL_MS;
  }

  function startAutoRefresh(intervalMs) {
    autoRefreshEnabled = true;
    stopAutoRefresh(); // clear existing timer

    const dot = document.getElementById('refresh-dot');
    const label = document.getElementById('refresh-label');
    const btn = document.getElementById('refresh-toggle');
    if (dot) dot.classList.add('active');
    if (label) label.textContent = 'Live';
    if (btn) {
      btn.classList.add('active');
      btn.setAttribute('aria-pressed', 'true');
    }

    autoRefreshTimer = setInterval(function () {
      if (currentView !== 'run-list') return;
      fetchRuns().then(function (newRuns) {
        runs = newRuns;
        const app = document.getElementById('app');
        if (app && currentView === 'run-list') {
          app.innerHTML = renderRunTable(runs, filters);
          attachFilterListeners();
          document.getElementById('run-count').textContent =
            runs.length + ' run' + (runs.length !== 1 ? 's' : '');
        }
      }).catch(function () {
        var bar = document.getElementById('status-bar');
        if (bar) bar.textContent = 'Auto-refresh: connection error (data may be stale)';
      });
    }, intervalMs);
  }

  function stopAutoRefresh() {
    if (autoRefreshTimer) {
      clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
    const dot = document.getElementById('refresh-dot');
    const label = document.getElementById('refresh-label');
    const btn = document.getElementById('refresh-toggle');
    if (dot) dot.classList.remove('active');
    if (label) label.textContent = 'Auto-refresh';
    if (btn) {
      btn.classList.remove('active');
      btn.setAttribute('aria-pressed', 'false');
    }
  }

  function toggleAutoRefresh() {
    if (autoRefreshEnabled) {
      autoRefreshEnabled = false;
      stopAutoRefresh();
    } else {
      startAutoRefresh(getRefreshInterval());
    }
  }

  // ============================================================
  // === Router ===
  // ============================================================

  function navigate(view, params) {
    if (view === 'run-list') {
      loadRuns();
    } else if (view === 'run-detail' && params.runId) {
      showRunDetail(params.runId);
    }
  }

  // ============================================================
  // === Event Delegation ===
  // ============================================================

  /**
   * Single delegated click handler on #app.
   * Handles: expandable event rows, step group headers, run table rows.
   */
  function handleAppInteraction(e) {
    // Expandable event row
    const eventRow = e.target.closest('.event-row');
    if (eventRow) {
      toggleEventExpand(eventRow);
      return;
    }

    // Step group header — collapse/expand
    const groupHeader = e.target.closest('.step-group-header');
    if (groupHeader) {
      toggleStepGroup(groupHeader);
      return;
    }

    // Clear filters button inside empty state
    if (e.target.id === 'filter-clear-empty') {
      filters = { status: 'all', workflow: 'all', search: '' };
      showRunList(runs);
      return;
    }

    // Individual filter badge removal
    var badgeBtn = e.target.closest('.filter-badge-remove');
    if (badgeBtn) {
      var which = badgeBtn.dataset.filterClear;
      if (which === 'status') { filters.status = 'all'; var s = document.getElementById('filter-status'); if (s) s.value = 'all'; }
      if (which === 'workflow') { filters.workflow = 'all'; var w = document.getElementById('filter-workflow'); if (w) w.value = 'all'; }
      if (which === 'search') { filters.search = ''; var i = document.getElementById('filter-search'); if (i) i.value = ''; }
      refreshRunListView();
      return;
    }

    // Run table row
    const runRow = e.target.closest('tr.clickable');
    if (runRow) {
      const runId = runRow.dataset.runId;
      if (runId) navigate('run-detail', { runId: runId });
      return;
    }
  }

  // Click handler
  document.getElementById('app').addEventListener('click', handleAppInteraction);

  // Keyboard handler — Enter/Space activates the same as click
  document.getElementById('app').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') {
      const target = e.target;
      if (target.closest('.event-row') || target.closest('.step-group-header') || target.closest('tr.clickable')) {
        e.preventDefault();
        handleAppInteraction(e);
      }
    }
  });

  function toggleEventExpand(row) {
    const expandId = row.dataset.expandId;
    if (!expandId) return;
    const expandEl = document.getElementById(expandId);
    if (!expandEl) return;

    const chevron = row.querySelector('.evt-chevron');
    const isOpen = expandEl.classList.contains('visible');

    if (isOpen) {
      expandEl.classList.remove('visible');
      if (chevron) chevron.classList.remove('expanded');
    } else {
      expandEl.classList.add('visible');
      if (chevron) chevron.classList.add('expanded');
    }
  }

  function toggleStepGroup(header) {
    const stepId = header.dataset.stepId;
    const chevron = header.querySelector('.group-chevron');
    // Find the sibling events list
    const group = header.closest('.step-group');
    if (!group) return;
    const eventsList = group.querySelector('.step-group-events');
    if (!eventsList) return;

    const isOpen = eventsList.classList.contains('visible');
    if (isOpen) {
      eventsList.classList.remove('visible');
      if (chevron) chevron.classList.remove('expanded');
    } else {
      eventsList.classList.add('visible');
      if (chevron) chevron.classList.add('expanded');
    }
  }

  // ============================================================
  // === Initial Load ===
  // ============================================================

  function loadRuns() {
    const app = document.getElementById('app');
    app.innerHTML = '<div class="empty">Loading\u2026</div>';
    fetchRuns()
      .then(function (runList) {
        showRunList(runList);
      })
      .catch(function (err) {
        app.innerHTML =
          '<div class="empty text-error">Error loading runs: ' +
          esc(String(err)) + '</div>';
        document.getElementById('status-bar').textContent = 'Error: ' + String(err);
      });
  }

  // ============================================================
  // === Auto-refresh toggle button ===
  // ============================================================

  const refreshToggle = document.getElementById('refresh-toggle');
  if (refreshToggle) {
    refreshToggle.addEventListener('click', function () {
      toggleAutoRefresh();
    });
  }

  const refreshInterval = document.getElementById('refresh-interval');
  if (refreshInterval) {
    refreshInterval.addEventListener('change', function () {
      if (autoRefreshEnabled) {
        startAutoRefresh(getRefreshInterval());
      }
    });
  }

  // ============================================================
  // === Boot ===
  // ============================================================

  document.getElementById('status-bar').textContent =
    TOKEN ? 'Authenticated \u2014 ' + window.location.host : 'No auth \u2014 ' + window.location.host;

  loadRuns();

})();
