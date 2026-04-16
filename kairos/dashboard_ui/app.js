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
  let openInspectorStepId = null;   // currently open inspector step_id, or null
  let currentRunEvents = [];        // events for currently viewed run detail
  let selectedRuns = [];            // run_id strings selected for diff (max 2)

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

  /** Inspect icon — magnifying glass, used on step group inspect button. */
  function iconInspect() {
    return '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5">' +
      '<circle cx="7" cy="7" r="4"/>' +
      '<path d="M10 10l4 4"/>' +
      '</svg>';
  }

  /** Download icon — used on export JSON/CSV buttons. */
  function iconDownload() {
    return '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2v8m0 0l-3-3m3 3l3-3M3 13h10"/></svg>';
  }

  /** Copy icon — used on copy API URL button. */
  function iconCopy() {
    return '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="5" width="8" height="8" rx="1.5"/><path d="M3 11V3h8"/></svg>';
  }

  // ============================================================
  // === SVG Helpers (Enhancement 5) ===
  // ============================================================

  const SVG_NS = 'http://www.w3.org/2000/svg';

  /** Create an SVG element with given attributes and optional children. */
  function svgEl(tag, attrs, children) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        el.setAttribute(k, attrs[k]);
      });
    }
    if (children) {
      children.forEach(function (c) { if (c) el.appendChild(c); });
    }
    return el;
  }

  /** Create a rounded SVG rect. */
  function svgRect(x, y, w, h, rx, fill, stroke, strokeWidth) {
    return svgEl('rect', {
      x: x, y: y, width: w, height: h, rx: rx,
      fill: fill || 'none',
      stroke: stroke || 'none',
      'stroke-width': strokeWidth || 1,
    });
  }

  /** Create an SVG text element. */
  function svgText(x, y, text, opts) {
    opts = opts || {};
    var tokens = getCssTokens();
    const el = svgEl('text', {
      x: x, y: y,
      'text-anchor': opts.anchor || 'middle',
      'dominant-baseline': opts.baseline || 'auto',
      'font-size': opts.fontSize || 12,
      'font-family': opts.fontFamily || tokens.fontMono,
      'font-weight': opts.fontWeight || 'normal',
      fill: opts.fill || tokens.textPrimary,
    });
    el.textContent = text;
    return el;
  }

  /** Create an SVG line. */
  function svgLine(x1, y1, x2, y2, stroke, dash) {
    var tokens = getCssTokens();
    var attrs = { x1: x1, y1: y1, x2: x2, y2: y2, stroke: stroke || tokens.edge, 'stroke-width': 2 };
    if (dash) attrs['stroke-dasharray'] = dash;
    return svgEl('line', attrs);
  }

  /** Create an SVG path element (used for bezier edges). */
  function svgPath(d, stroke, fill) {
    var tokens = getCssTokens();
    return svgEl('path', {
      d: d,
      stroke: stroke || tokens.edge,
      fill: fill || 'none',
      'stroke-width': 2,
      'stroke-linecap': 'round',
    });
  }

  /** Create an SVG <marker> element for arrowheads. */
  function svgArrowMarker(id, color) {
    var tokens = getCssTokens();
    var marker = svgEl('marker', {
      id: id,
      markerWidth: 8,
      markerHeight: 6,
      refX: 7,
      refY: 3,
      orient: 'auto',
    });
    var arrow = svgEl('polygon', {
      points: '0 0, 8 3, 0 6',
      fill: color || tokens.edge,
    });
    marker.appendChild(arrow);
    return marker;
  }

  /** Create an SVG <g> group, optionally with a transform. */
  function svgGroup(children, transform) {
    var attrs = {};
    if (transform) attrs.transform = transform;
    return svgEl('g', attrs, children || []);
  }

  // --- Graph layout constants ---
  var NODE_W = 160;
  var NODE_H = 56;
  var NODE_RX = 8;
  var LAYER_SPACING_Y = 136;
  var NODE_GAP_X = 40;
  var NODE_PAD_TOP = 40;

  // Cache CSS token values once at use time (DOM may not exist at parse time)
  var _cssTokenCache = null;

  function getCssTokens() {
    if (_cssTokenCache) return _cssTokenCache;
    var style = getComputedStyle(document.documentElement);
    function t(name) { return style.getPropertyValue(name).trim(); }
    _cssTokenCache = {
      nodeBg:       t('--graph-node-bg')        || '#1e293b',
      edge:         t('--graph-edge')            || '#475569',
      success:      t('--color-success')         || '#22c55e',
      error:        t('--color-error')           || '#ef4444',
      skipped:      t('--color-skipped')         || '#64748b',
      running:      t('--color-running')         || '#818cf8',
      bg600:        t('--bg-600')                || '#475569',
      textPrimary:  t('--text-primary')          || '#f8fafc',
      textMuted:    t('--text-muted')            || '#94a3b8',
      successText:  t('--color-success-text')    || '#86efac',
      errorText:    t('--color-error-text')      || '#fca5a5',
      skippedText:  t('--color-skipped-text')    || '#94a3b8',
      runningText:  t('--color-running-text')    || '#a5b4fc',
      fontMono:     t('--font-mono')             || 'monospace',
    };
    return _cssTokenCache;
  }

  function nodeStrokeColor(status, tokens) {
    if (status === 'complete' || status === 'completed') return tokens.success;
    if (status === 'failed')  return tokens.error;
    if (status === 'skipped') return tokens.skipped;
    if (status === 'running') return tokens.running;
    return tokens.bg600;
  }

  function nodeTextColor(status, tokens) {
    if (status === 'complete' || status === 'completed') return tokens.successText;
    if (status === 'failed')  return tokens.errorText;
    if (status === 'skipped') return tokens.skippedText;
    if (status === 'running') return tokens.runningText;
    return tokens.textMuted;
  }

  function nodeStatusIcon(status) {
    if (status === 'complete' || status === 'completed') return '\u2713';
    if (status === 'failed')  return '\u2717';
    if (status === 'skipped') return '\u2212';
    if (status === 'running') return '\u25b6';
    return '\u2022';
  }

  /**
   * Extract dependency data from run events.
   * Returns { steps: [{id, status, durationMs, dependencies, foreachCount}], edges: [{from,to}] }
   */
  function extractDependencyData(events) {
    var stepMap = {};
    var stepOrder = [];

    // Pass 1: find workflow_start for plan data
    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      if (ev.event_type === 'workflow_start' && ev.data && ev.data.plan) {
        var plan = ev.data.plan;
        var planSteps = plan.steps || [];
        for (var j = 0; j < planSteps.length; j++) {
          var ps = planSteps[j];
          var sid = ps.id || ps.step_id || ps.name || String(j);
          if (!stepMap[sid]) {
            stepMap[sid] = { id: sid, status: 'pending', durationMs: 0, dependencies: ps.depends_on || [], foreachCount: 0 };
            stepOrder.push(sid);
          }
        }
        break;
      }
    }

    // Pass 2: collect step events
    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      var sid = ev.step_id;
      if (!sid) continue;

      if (!stepMap[sid]) {
        var deps = (ev.data && ev.data.dependencies) ? ev.data.dependencies : [];
        stepMap[sid] = { id: sid, status: 'pending', durationMs: 0, dependencies: deps, foreachCount: 0 };
        stepOrder.push(sid);
      }

      var s = stepMap[sid];
      if (ev.event_type === 'step_complete' || ev.event_type === 'step_finish') {
        s.status = 'complete';
        if (ev.data && ev.data.duration_ms) s.durationMs = ev.data.duration_ms;
      } else if (ev.event_type === 'step_fail' || ev.event_type === 'step_error') {
        s.status = 'failed';
        if (ev.data && ev.data.duration_ms) s.durationMs = ev.data.duration_ms;
      } else if (ev.event_type === 'step_skip' || ev.event_type === 'step_skipped') {
        s.status = 'skipped';
      } else if (ev.event_type === 'step_start') {
        if (s.status === 'pending') s.status = 'running';
        if (ev.data && ev.data.dependencies && s.dependencies.length === 0) {
          s.dependencies = ev.data.dependencies;
        }
      }
    }

    var steps = stepOrder.map(function (sid) { return stepMap[sid]; });

    // Build edges
    var edges = [];
    steps.forEach(function (step) {
      (step.dependencies || []).forEach(function (dep) {
        if (stepMap[dep]) {
          edges.push({ from: dep, to: step.id });
        }
      });
    });

    return { steps: steps, edges: edges };
  }

  /**
   * Compute node positions using a simple layered (Sugiyama-lite) layout.
   * Returns { nodes: [{id, x, y, ...step}], width, height }
   */
  function computeGraphLayout(steps, edges) {
    if (!steps || steps.length === 0) return { nodes: [], width: 0, height: 0 };

    // Build adjacency: stepId -> [depends-on step IDs]
    var depMap = {};
    steps.forEach(function (s) { depMap[s.id] = s.dependencies || []; });

    // Assign layers by longest path from any root
    var layers = {};
    steps.forEach(function (s) { layers[s.id] = 0; });

    var changed = true;
    var maxIter = steps.length + 1;
    while (changed && maxIter-- > 0) {
      changed = false;
      steps.forEach(function (s) {
        (depMap[s.id] || []).forEach(function (dep) {
          if (layers[dep] !== undefined) {
            var needed = layers[dep] + 1;
            if (needed > layers[s.id]) {
              layers[s.id] = needed;
              changed = true;
            }
          }
        });
      });
    }
    if (maxIter <= 0) {
      console.warn('Kairos: graph layout iteration limit reached — possible circular dependencies');
    }

    // Group steps by layer
    var layerGroups = {};
    steps.forEach(function (s) {
      var lyr = layers[s.id] || 0;
      if (!layerGroups[lyr]) layerGroups[lyr] = [];
      layerGroups[lyr].push(s);
    });

    var maxLayer = 0;
    Object.keys(layerGroups).forEach(function (l) {
      if (Number(l) > maxLayer) maxLayer = Number(l);
    });

    // Calculate total width needed
    var maxNodesInLayer = 1;
    Object.keys(layerGroups).forEach(function (l) {
      if (layerGroups[l].length > maxNodesInLayer) maxNodesInLayer = layerGroups[l].length;
    });
    var graphWidth = maxNodesInLayer * NODE_W + (maxNodesInLayer - 1) * NODE_GAP_X;

    // Position nodes
    var nodes = [];
    Object.keys(layerGroups).forEach(function (l) {
      var lyr = Number(l);
      var group = layerGroups[l];
      var rowWidth = group.length * NODE_W + (group.length - 1) * NODE_GAP_X;
      var startX = (graphWidth - rowWidth) / 2;
      group.forEach(function (s, idx) {
        var nx = startX + idx * (NODE_W + NODE_GAP_X);
        var ny = NODE_PAD_TOP + lyr * LAYER_SPACING_Y;
        nodes.push(Object.assign({}, s, { x: nx, y: ny }));
      });
    });

    var totalHeight = NODE_PAD_TOP + maxLayer * LAYER_SPACING_Y + NODE_H + NODE_PAD_TOP;
    return { nodes: nodes, width: graphWidth, height: totalHeight };
  }

  /**
   * Render the graph container placeholder (HTML string).
   * The actual SVG is mounted by mountDependencyGraph() after innerHTML is set.
   */
  function renderGraphPlaceholder() {
    return '<div class="graph-container" id="dep-graph" role="img" aria-label="Step dependency graph"></div>';
  }

  /**
   * Build and append the SVG dependency graph into #dep-graph.
   * Must be called AFTER the container is added to the DOM (after innerHTML is set).
   */
  function mountDependencyGraph(events) {
    var container = document.getElementById('dep-graph');
    if (!container) return;

    var data = extractDependencyData(events);
    if (!data.steps || data.steps.length === 0) {
      container.innerHTML = '<div class="graph-empty">No step data available</div>';
      return;
    }

    var layout = computeGraphLayout(data.steps, data.edges);
    var tokens = getCssTokens();
    var containerWidth = container.clientWidth || 600;
    var svgWidth = Math.max(containerWidth - 32, layout.width + 80);
    var svgHeight = layout.height;
    var xOffset = (svgWidth - layout.width) / 2;

    // Build node position lookup for edge routing
    var nodePos = {};
    layout.nodes.forEach(function (n) {
      nodePos[n.id] = { x: n.x + xOffset, y: n.y };
    });

    // Create SVG root
    var svg = svgEl('svg', {
      width: svgWidth,
      height: svgHeight,
      viewBox: '0 0 ' + svgWidth + ' ' + svgHeight,
    });

    // <defs> for arrowhead marker
    var defs = svgEl('defs');
    defs.appendChild(svgArrowMarker('arrow-default', tokens.edge));
    svg.appendChild(defs);

    // Draw edges first (behind nodes)
    data.edges.forEach(function (edge) {
      var fromPos = nodePos[edge.from];
      var toPos = nodePos[edge.to];
      if (!fromPos || !toPos) return;

      var fromX = fromPos.x + NODE_W / 2;
      var fromY = fromPos.y + NODE_H;
      var toX = toPos.x + NODE_W / 2;
      var toY = toPos.y;
      var midY = (fromY + toY) / 2;

      // Cubic bezier: M fromX,fromY C fromX,midY toX,midY toX,toY
      var d = 'M ' + fromX + ' ' + fromY + ' C ' + fromX + ' ' + midY + ' ' + toX + ' ' + midY + ' ' + toX + ' ' + toY;
      var pathEl = svgPath(d, tokens.edge, 'none');
      pathEl.setAttribute('marker-end', 'url(#arrow-default)');
      svg.appendChild(pathEl);
    });

    // Draw nodes
    layout.nodes.forEach(function (node) {
      var nx = node.x + xOffset;
      var ny = node.y;
      var stroke = nodeStrokeColor(node.status, tokens);
      var textColor = nodeTextColor(node.status, tokens);

      // <g> group for the node — carries data-step-id for event delegation
      var g = svgEl('g', { class: 'graph-node', 'data-step-id': node.id, tabindex: '0', role: 'button', 'aria-label': node.id });

      // Background rect
      g.appendChild(svgRect(nx, ny, NODE_W, NODE_H, NODE_RX, tokens.nodeBg, stroke, 2));

      // Step name (line 1) — truncate if needed
      var nameText = node.id.length > 18 ? node.id.slice(0, 17) + '\u2026' : node.id;
      g.appendChild(svgText(nx + NODE_W / 2, ny + 20, nameText, {
        anchor: 'middle',
        baseline: 'middle',
        fontSize: 12,
        fontFamily: tokens.fontMono,
        fontWeight: 'bold',
        fill: tokens.textPrimary,
      }));

      // Status + duration (line 2)
      var icon = nodeStatusIcon(node.status);
      var durStr = node.durationMs ? fmtDuration(node.durationMs) : node.status;
      var line2 = icon + '  ' + durStr;
      g.appendChild(svgText(nx + NODE_W / 2, ny + 38, line2, {
        anchor: 'middle',
        baseline: 'middle',
        fontSize: 11,
        fontFamily: tokens.fontMono,
        fontWeight: 'normal',
        fill: textColor,
      }));

      // Foreach badge (top-right)
      if (node.foreachCount && node.foreachCount > 0) {
        var badgeX = nx + NODE_W - 24;
        var badgeY = ny - 8;
        g.appendChild(svgRect(badgeX, badgeY, 22, 14, 7, tokens.bg600, 'none', 0));
        g.appendChild(svgText(badgeX + 11, badgeY + 7, '\u00d7' + node.foreachCount, {
          anchor: 'middle',
          baseline: 'middle',
          fontSize: 10,
          fontFamily: tokens.fontMono,
          fill: tokens.textMuted,
        }));
      }

      svg.appendChild(g);
    });

    container.appendChild(svg);

    // Hover highlight: brighten stroke on mousemove/mouseleave (avoids child-element flicker from bubbling events)
    svg.addEventListener('mousemove', function (e) {
      var node = e.target.closest('.graph-node');
      svg.querySelectorAll('.graph-node.graph-node-hover').forEach(function (el) {
        if (el !== node) el.classList.remove('graph-node-hover');
      });
      if (node) node.classList.add('graph-node-hover');
    });
    svg.addEventListener('mouseleave', function () {
      svg.querySelectorAll('.graph-node.graph-node-hover').forEach(function (el) {
        el.classList.remove('graph-node-hover');
      });
    });
  }

  /**
   * Scroll to the step group with the given step ID in the timeline,
   * expand it if collapsed, and briefly highlight the header.
   */
  function scrollToStepGroup(stepId) {
    var header = document.querySelector('.step-group-header[data-step-id="' + CSS.escape(stepId) + '"]');
    if (header) {
      header.scrollIntoView({ behavior: 'smooth', block: 'center' });
      // Expand the group if collapsed
      var group = header.closest('.step-group');
      if (group) {
        var eventsEl = group.querySelector('.step-group-events');
        if (eventsEl && eventsEl.style.display === 'none') {
          header.click();
        }
      }
      // Brief highlight
      header.classList.add('graph-highlight');
      setTimeout(function () { header.classList.remove('graph-highlight'); }, 1500);
    }
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
      '<button class="compare-btn" id="compare-btn" style="display:' + (selectedRuns.length === 2 ? '' : 'none') + '">Compare 2 runs</button>' +
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
      const isChecked = selectedRuns.indexOf(run.run_id) !== -1;
      rows +=
        '<tr class="clickable" data-run-id="' + esc(run.run_id) + '" tabindex="0" role="button">' +
        '<td class="td-checkbox"><input type="checkbox" class="run-checkbox" data-run-id="' + esc(run.run_id) + '" aria-label="Select run ' + esc((run.run_id || '').slice(0, 8)) + ' for comparison"' + (isChecked ? ' checked' : '') + '></td>' +
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
      '<th class="th-checkbox"></th>' +
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
          '<button class="inspect-btn" data-inspect-step="' + esc(stepId) + '" aria-label="Inspect step ' + esc(stepId) + '">' + iconInspect() + ' Inspect</button>' +
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
  // === Inspector Panel (Enhancement 8) ===
  // ============================================================

  /**
   * Render the inspector panel for a given step.
   * Finds step_start (inputs), step_complete (output), and validation events.
   * @param {string} stepId
   * @param {Array} events — full event list for the current run
   * @returns {string} HTML for the inspector panel
   */
  function renderInspectorPanel(stepId, events) {
    const stepEvents = events.filter(function (e) { return e.step_id === stepId; });

    const startEvt = stepEvents.find(function (e) { return e.event_type === 'step_start'; });
    const completeEvt = stepEvents.find(function (e) { return e.event_type === 'step_complete'; });
    const validationEvts = stepEvents.filter(function (e) {
      return e.event_type === 'validation_pass' || e.event_type === 'validation_fail';
    });

    var verbosityMsg =
      '<div class="inspector-empty">Step data not captured at this verbosity level.' +
      ' Re-run with <code>--verbose</code> to include full step input/output.</div>';

    // Input tab content — the logger does not capture step inputs,
    // so we show the step_start event data as context instead.
    var inputData = startEvt && startEvt.data;
    var inputHtml = inputData
      ? '<pre>' + colorizeJson(inputData, 0, 0) + '</pre>'
      : '<div class="inspector-empty">No input data recorded for this step.</div>';

    // Output tab content — requires VERBOSE verbosity
    var outputData = completeEvt && completeEvt.data && completeEvt.data.output;
    var outputHtml = outputData !== undefined && outputData !== null
      ? '<pre>' + colorizeJson(outputData, 0, 0) + '</pre>'
      : (completeEvt
        ? verbosityMsg
        : '<div class="inspector-empty">Step did not complete.</div>');

    // Validation tab content — distinguish "no contract" from "not captured"
    var validationHtml;
    if (validationEvts.length > 0) {
      validationHtml = '<pre>' + colorizeJson(
        validationEvts.map(function (e) { return e.data || {}; }),
        0, 0
      ) + '</pre>';
    } else {
      // Check if the step has validation events at all — if not, it may
      // simply have no output contract configured.
      var hasContract = events.some(function (e) {
        return (e.event_type === 'validation_start' || e.event_type === 'validation_complete' ||
                e.event_type === 'validation_pass' || e.event_type === 'validation_fail') &&
               e.step_id === stepId;
      });
      validationHtml = hasContract
        ? verbosityMsg
        : '<div class="inspector-empty">No validation contract configured for this step.</div>';
    }

    return (
      '<div class="inspector-panel" data-inspector-step="' + esc(stepId) + '">' +
      '<div class="inspector-header">' +
      '<div class="inspector-tabs">' +
      '<button class="inspector-tab active" data-tab="input">Input</button>' +
      '<button class="inspector-tab" data-tab="output">Output</button>' +
      '<button class="inspector-tab" data-tab="validation">Validation</button>' +
      '</div>' +
      '<button class="inspector-close" aria-label="Close inspector">\u00d7</button>' +
      '</div>' +
      '<div class="inspector-body">' +
      '<div class="inspector-tab-content active" data-tab-content="input">' + inputHtml + '</div>' +
      '<div class="inspector-tab-content" data-tab-content="output">' + outputHtml + '</div>' +
      '<div class="inspector-tab-content" data-tab-content="validation">' + validationHtml + '</div>' +
      '</div>' +
      '</div>'
    );
  }

  /**
   * Toggle the inspector panel for a step group.
   * If the same step is already open, close it. Otherwise open a new one.
   * @param {string} stepId
   */
  function toggleInspector(stepId) {
    if (openInspectorStepId === stepId) {
      closeInspector();
      return;
    }
    closeInspector();
    openInspectorStepId = stepId;

    // Find the step-group element for this stepId and insert the panel after it
    const header = document.querySelector('.step-group-header[data-step-id="' + CSS.escape(stepId) + '"]');
    if (!header) return;
    const group = header.closest('.step-group');
    if (!group) return;

    const panelHtml = renderInspectorPanel(stepId, currentRunEvents);
    group.insertAdjacentHTML('beforeend', panelHtml);
  }

  /**
   * Close any currently open inspector panel.
   */
  function closeInspector() {
    const existing = document.querySelector('.inspector-panel');
    if (existing) existing.remove();
    openInspectorStepId = null;
  }

  /**
   * Switch the active tab inside the inspector panel.
   * @param {Element} tabEl — the clicked .inspector-tab button
   */
  function switchInspectorTab(tabEl) {
    const panel = tabEl.closest('.inspector-panel');
    if (!panel) return;
    const tabName = tabEl.dataset.tab;

    // Deactivate all tabs and contents
    panel.querySelectorAll('.inspector-tab').forEach(function (t) {
      t.classList.remove('active');
    });
    panel.querySelectorAll('.inspector-tab-content').forEach(function (c) {
      c.classList.remove('active');
    });

    // Activate selected tab and content
    tabEl.classList.add('active');
    const content = panel.querySelector('[data-tab-content="' + tabName + '"]');
    if (content) content.classList.add('active');
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
        currentRunEvents = events;

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
          '<div class="export-actions">' +
          '<button class="btn-export" id="export-json" aria-label="Download JSON">' + iconDownload() + ' JSON</button>' +
          '<button class="btn-export" id="export-csv" aria-label="Download CSV">' + iconDownload() + ' CSV</button>' +
          '<button class="btn-export" id="copy-api-url" aria-label="Copy API URL">' + iconCopy() + ' Copy URL</button>' +
          '</div>' +
          '</div>';

        app.innerHTML =
          navBar +
          summaryHtml +
          '<div class="panel">' +
          '<div class="panel-header">Step Dependency Graph</div>' +
          renderGraphPlaceholder() +
          '</div>' +
          '<div class="panel">' +
          '<div class="panel-header">Events (' + events.length + ')</div>' +
          eventsHtml +
          '</div>';

        // Mount SVG graph AFTER innerHTML is set (two-phase rendering)
        mountDependencyGraph(events);

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
        const isChecked = selectedRuns.indexOf(run.run_id) !== -1;
        rows +=
          '<tr class="clickable" data-run-id="' + esc(run.run_id) + '" tabindex="0" role="button">' +
          '<td class="td-checkbox"><input type="checkbox" class="run-checkbox" data-run-id="' + esc(run.run_id) + '" aria-label="Select run ' + esc((run.run_id || '').slice(0, 8)) + ' for comparison"' + (isChecked ? ' checked' : '') + '></td>' +
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
      selectedRuns = [];
      loadRuns();
    } else if (view === 'run-detail' && params.runId) {
      selectedRuns = [];
      showRunDetail(params.runId);
    } else if (view === 'diff' && params.idA && params.idB) {
      showDiffView(params.idA, params.idB);
    }
  }

  // ============================================================
  // === Event Delegation ===
  // ============================================================

  /**
   * Fetch a URL and trigger a file download via a Blob object URL.
   * Does not navigate away from the page if the request fails.
   *
   * @param {string} url - The URL to fetch (already includes auth token).
   */
  function downloadFile(url) {
    fetch(url).then(function (resp) {
      if (!resp.ok) return;
      var cd = resp.headers.get('Content-Disposition') || '';
      var match = cd.match(/filename="?([^"]+)"?/);
      var filename = match ? match[1] : 'download';
      return resp.blob().then(function (blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        setTimeout(function () {
          document.body.removeChild(a);
          URL.revokeObjectURL(a.href);
        }, 100);
      });
    });
  }

  /**
   * Single delegated click handler on #app.
   * Handles: expandable event rows, step group headers, run table rows.
   */
  function handleAppInteraction(e) {
    // Download JSON export
    if (e.target.closest('#export-json')) {
      if (!currentRunId) return;
      downloadFile(apiUrl('/api/runs/' + currentRunId + '/export/json'));
      return;
    }
    // Download CSV export
    if (e.target.closest('#export-csv')) {
      if (!currentRunId) return;
      downloadFile(apiUrl('/api/runs/' + currentRunId + '/export/csv'));
      return;
    }
    // Copy API URL to clipboard
    if (e.target.closest('#copy-api-url')) {
      if (!currentRunId) return;
      var url = window.location.origin + apiUrl('/api/runs/' + currentRunId);
      var btn = document.getElementById('copy-api-url');
      var orig = btn ? btn.innerHTML : '';
      navigator.clipboard.writeText(url).then(function () {
        if (btn) {
          btn.classList.add('btn-export-copied');
          btn.innerHTML = iconCopy() + ' Copied!';
          setTimeout(function () {
            btn.classList.remove('btn-export-copied');
            btn.innerHTML = orig;
          }, 2000);
        }
      }).catch(function () {
        if (btn) {
          btn.textContent = 'Failed';
          setTimeout(function () { btn.innerHTML = orig; }, 2000);
        }
      });
      return;
    }

    // Graph node click — scroll to step group
    const graphNode = e.target.closest('g[data-step-id]');
    if (graphNode && graphNode.closest('.graph-container')) {
      const stepId = graphNode.dataset.stepId;
      if (stepId) scrollToStepGroup(stepId);
      return;
    }

    // Inspector tab switch
    const inspectorTab = e.target.closest('.inspector-tab');
    if (inspectorTab) {
      switchInspectorTab(inspectorTab);
      return;
    }

    // Inspector close button
    const inspectorClose = e.target.closest('.inspector-close');
    if (inspectorClose) {
      closeInspector();
      return;
    }

    // Inspect button on step group header
    const inspectBtn = e.target.closest('.inspect-btn');
    if (inspectBtn) {
      e.stopPropagation();
      const stepId = inspectBtn.dataset.inspectStep;
      if (stepId) toggleInspector(stepId);
      return;
    }

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

    // Checkbox for run diff selection — must come before run row handler
    var checkbox = e.target.closest('.run-checkbox');
    if (checkbox) {
      e.stopPropagation();
      handleRunCheckbox(checkbox);
      return;
    }

    // Compare button
    if (e.target.id === 'compare-btn' || e.target.closest('#compare-btn')) {
      navigate('diff', { idA: selectedRuns[0], idB: selectedRuns[1] });
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
  // === Enhancement 7 — Diff Two Runs ===
  // ============================================================

  function handleRunCheckbox(checkbox) {
    var runId = checkbox.dataset.runId;
    if (!runId) return;
    if (checkbox.checked) {
      if (selectedRuns.length >= 2) {
        checkbox.checked = false;
        return; // max 2 selections
      }
      selectedRuns.push(runId);
    } else {
      selectedRuns = selectedRuns.filter(function (id) { return id !== runId; });
    }
    updateCompareButton();
  }

  function updateCompareButton() {
    var btn = document.getElementById('compare-btn');
    if (!btn) return;
    btn.style.display = selectedRuns.length === 2 ? '' : 'none';
  }

  /**
   * Extract a list of step summaries from a run's event array.
   * @param {Array} events
   * @returns {Array<{id: string, status: string, durationMs: number}>}
   */
  function extractStepList(events) {
    var stepMap = {};
    events.forEach(function (ev) {
      if (ev.event_type === 'step_start' && ev.step_id) {
        stepMap[ev.step_id] = { id: ev.step_id, status: 'running', durationMs: 0 };
      }
      if (ev.event_type === 'step_complete' && ev.step_id && stepMap[ev.step_id]) {
        stepMap[ev.step_id].status = 'complete';
        stepMap[ev.step_id].durationMs = (ev.data && ev.data.duration_ms) || 0;
      }
      if (ev.event_type === 'step_fail' && ev.step_id && stepMap[ev.step_id]) {
        stepMap[ev.step_id].status = 'failed';
        stepMap[ev.step_id].durationMs = (ev.data && ev.data.duration_ms) || 0;
      }
      if (ev.event_type === 'step_skip' && ev.step_id) {
        stepMap[ev.step_id] = { id: ev.step_id, status: 'skipped', durationMs: 0 };
      }
    });
    return Object.values(stepMap);
  }

  /**
   * Render the diff view comparing two run detail objects.
   * @param {Object} runA
   * @param {Object} runB
   * @returns {string} HTML
   */
  function renderDiffView(runA, runB) {
    var summaryA = runA.summary || {};
    var summaryB = runB.summary || {};
    var stepsA = extractStepList(runA.events || []);
    var stepsB = extractStepList(runB.events || []);

    // Build step maps for O(1) lookup
    var stepMapA = {};
    stepsA.forEach(function (s) { stepMapA[s.id] = s; });
    var stepMapB = {};
    stepsB.forEach(function (s) { stepMapB[s.id] = s; });

    // Union of all step IDs
    var allStepIds = [];
    var seen = {};
    stepsA.forEach(function (s) { if (!seen[s.id]) { allStepIds.push(s.id); seen[s.id] = true; } });
    stepsB.forEach(function (s) { if (!seen[s.id]) { allStepIds.push(s.id); seen[s.id] = true; } });

    // Summary section for each column
    function renderSummaryCol(summary, otherSummary) {
      var statusDiff = summary.status !== otherSummary.status;
      var durDiff = summary.duration_ms !== otherSummary.duration_ms;
      var stepsDiff = (summary.completed_steps !== otherSummary.completed_steps) ||
                      (summary.total_steps !== otherSummary.total_steps);
      var rows = '';
      rows += '<div class="diff-summary-row' + (statusDiff ? ' diff-changed' : '') + '">' +
        '<span class="diff-label">Status</span>' +
        '<span class="diff-value">' + statusBadge(summary.status || 'unknown') + '</span>' +
        '</div>';
      rows += '<div class="diff-summary-row' + (durDiff ? ' diff-changed' : '') + '">' +
        '<span class="diff-label">Duration</span>' +
        '<span class="diff-value">' + esc(fmtDuration(summary.duration_ms)) + '</span>' +
        '</div>';
      rows += '<div class="diff-summary-row' + (stepsDiff ? ' diff-changed' : '') + '">' +
        '<span class="diff-label">Steps</span>' +
        '<span class="diff-value">' + esc((summary.completed_steps || 0) + '/' + (summary.total_steps || 0)) + '</span>' +
        '</div>';
      return rows;
    }

    // Step rows for each column
    function renderStepCol(stepId, ownMap, otherMap) {
      var step = ownMap[stepId];
      var other = otherMap[stepId];
      if (!step) {
        return '<div class="diff-step-row diff-missing">' +
          '<span class="diff-step-name">' + esc(stepId) + '</span>' +
          '<span class="diff-step-duration">\u2014</span>' +
          '</div>';
      }
      var statusChanged = !other || step.status !== other.status;
      var arrow = '';
      if (statusChanged && other) {
        if (step.status === 'complete' && other.status === 'failed') {
          arrow = '<span class="diff-arrow-improve">\u2191</span>';
        } else if (step.status === 'failed' && other.status === 'complete') {
          arrow = '<span class="diff-arrow-regress">\u2193</span>';
        }
      }
      var delta = '';
      if (other && step.durationMs !== other.durationMs) {
        var diff = step.durationMs - other.durationMs;
        if (Math.abs(diff) >= 1) {
          var cls = diff < 0 ? 'diff-delta-better' : 'diff-delta-worse';
          var sign = diff < 0 ? '' : '+';
          delta = '<span class="diff-delta ' + cls + '">' + esc(sign + Math.round(diff) + 'ms') + '</span>';
        }
      }
      return '<div class="diff-step-row' + (statusChanged ? ' diff-changed' : '') + '">' +
        '<span class="diff-step-name">' + esc(stepId) + '</span>' +
        arrow +
        '<span class="diff-step-duration">' + esc(fmtDuration(step.durationMs)) + delta + '</span>' +
        '</div>';
    }

    var idA = summaryA.run_id || runA.run_id || '';
    var idB = summaryB.run_id || runB.run_id || '';

    var colAHeader = '<div class="diff-column-header">' +
      '<span class="mono">' + esc((idA || '').slice(0, 8) || 'Run A') + '</span>' +
      statusBadge(summaryA.status || 'unknown') +
      '</div>';
    var colBHeader = '<div class="diff-column-header">' +
      '<span class="mono">' + esc((idB || '').slice(0, 8) || 'Run B') + '</span>' +
      statusBadge(summaryB.status || 'unknown') +
      '</div>';

    var colASummary = renderSummaryCol(summaryA, summaryB);
    var colBSummary = renderSummaryCol(summaryB, summaryA);

    var colASteps = allStepIds.map(function (id) { return renderStepCol(id, stepMapA, stepMapB); }).join('');
    var colBSteps = allStepIds.map(function (id) { return renderStepCol(id, stepMapB, stepMapA); }).join('');

    var colA = '<div class="diff-column">' + colAHeader + colASummary + colASteps + '</div>';
    var colB = '<div class="diff-column">' + colBHeader + colBSummary + colBSteps + '</div>';

    return '<div class="panel"><div class="diff-view">' + colA + colB + '</div></div>';
  }

  /**
   * Show the diff view for two run IDs.
   * @param {string} idA
   * @param {string} idB
   */
  function showDiffView(idA, idB) {
    currentView = 'diff';
    document.title = 'Kairos \u2014 Compare Runs';
    var app = document.getElementById('app');
    app.innerHTML = '<div class="loading">Loading comparison\u2026</div>';

    var navBar = '<div class="detail-nav">' +
      '<button class="back-btn" id="back-btn">\u2190 Back to runs</button>' +
      '<span class="detail-breadcrumb">Comparing ' + esc(idA.slice(0, 8)) + ' vs ' + esc(idB.slice(0, 8)) + '</span>' +
      '</div>';

    Promise.all([fetchRunDetail(idA), fetchRunDetail(idB)])
      .then(function (results) {
        var runA = results[0];
        var runB = results[1];
        app.innerHTML = navBar + renderDiffView(runA, runB);
        var backBtn = document.getElementById('back-btn');
        if (backBtn) {
          backBtn.addEventListener('click', function () { navigate('run-list', {}); });
        }
      })
      .catch(function (err) {
        app.innerHTML = navBar +
          '<div class="loading">Failed to load run data for comparison: ' + esc(String(err)) + '</div>';
        var backBtn = document.getElementById('back-btn');
        if (backBtn) {
          backBtn.addEventListener('click', function () { navigate('run-list', {}); });
        }
      });
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
