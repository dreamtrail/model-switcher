"""Statistics dashboard HTML with page-specific styling and JavaScript."""

from src.page_css import (
    LIGHT_PALETTE_START_CSS,
    PAGE_BASE_REST_CSS,
    HEADINGS_CSS,
    STATUS_DOTS_CSS,
    CARDS_AND_KPI_GRID_CSS,
    FOUR_COLUMN_KPIS_CSS,
    KPI_TILES_CSS,
    KPI_SUBTEXT_CSS,
    KPI_SUBDOT_CSS,
    SECTIONS_CSS,
    NARROW_LAYOUT_CSS,
)


STATS_HTML = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Switcher — Statistics</title>
<style>
"""
    + LIGHT_PALETTE_START_CSS
    + PAGE_BASE_REST_CSS
    + """  .top {
    display: flex; flex-wrap: wrap; align-items: flex-end;
    justify-content: space-between; gap: 0.5rem 1.5rem; margin-bottom: 1.25rem;
  }
"""
    + HEADINGS_CSS
    + """  .live {
    display: inline-flex; align-items: center; gap: 0.45rem;
    color: var(--ink-2); font-size: 0.8125rem; white-space: nowrap;
  }
"""
    + STATUS_DOTS_CSS
    + """  .live .dot.good { box-shadow: 0 0 0 3px var(--good-tint); }
"""
    + CARDS_AND_KPI_GRID_CSS
    + """  /* Seven tiles; the loaded-model tile spans two columns so 2/4/8-column
     grids fill every row without an orphan tile. */
  .kpis .tile.wide { grid-column: span 2; }
"""
    + FOUR_COLUMN_KPIS_CSS
    + """  @media (min-width: 72rem) { .kpis { grid-template-columns: repeat(8, minmax(0, 1fr)); } }
"""
    + KPI_TILES_CSS
    + """  .tile .value.idle { color: var(--muted); }
  .tile .value.name {
    display: -webkit-box; -webkit-box-orient: vertical;
    -webkit-line-clamp: 2; line-clamp: 2;
    min-height: 1.84rem; font-size: 1.15rem; line-height: 1.6;
    letter-spacing: 0; white-space: normal; overflow-wrap: anywhere;
  }
  .tile .value.name.long {
    font-size: 1.05rem; line-height: 1.3; word-break: break-all;
  }
  .tile .unit {
    margin-left: 0.25rem; color: var(--muted); font-size: 0.8rem;
    font-weight: 500; letter-spacing: 0;
  }
"""
    + KPI_SUBTEXT_CSS
    + """  .tile .sub .line {
    display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
"""
    + KPI_SUBDOT_CSS
    + """  .tile .sub .serving { color: var(--ink-2); }
"""
    + SECTIONS_CSS
    + """  .table-wrap { overflow-x: auto; padding-bottom: 0.35rem; }
  table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-variant-numeric: tabular-nums; font-size: 0.875rem;
  }
  th, td { padding: 0.6rem 0.75rem; text-align: right; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; padding-left: 1.25rem; }
  th:last-child, td:last-child { padding-right: 1.25rem; }
  th {
    color: var(--muted); font-size: 0.75rem; font-weight: 500;
    padding-top: 0.35rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--hairline);
  }
  tbody td { border-bottom: 1px solid var(--hairline); }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--wash); }
  tfoot td { border-top: 1px solid var(--baseline); color: var(--ink-2); }
  tfoot td:first-child { color: var(--ink); font-weight: 600; }
  tbody:empty + tfoot td { border-top: none; }
  .model-name {
    display: inline-block; max-width: 12rem; vertical-align: bottom;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-weight: 500;
  }
  tr.loaded .model-name { font-weight: 600; }
  .pill {
    display: inline-flex; align-items: center; gap: 0.3rem; margin-left: 0.5rem;
    padding: 0.05rem 0.5rem 0.05rem 0.4rem; border-radius: 999px;
    background: var(--good-tint); color: var(--ink-2);
    font-size: 0.7rem; font-weight: 500; vertical-align: 0.1em;
  }
  .pill .dot { width: 0.4rem; height: 0.4rem; }
  .muted { color: var(--muted); }
  .chips { display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0 1.25rem 1.1rem; }
  .chip {
    display: inline-flex; align-items: center; gap: 0.45rem;
    padding: 0.25rem 0.7rem 0.25rem 0.6rem; border-radius: 999px;
    background: var(--critical-tint); font-size: 0.8125rem;
  }
  .chip .count { color: var(--ink-2); font-weight: 600; font-variant-numeric: tabular-nums; }
  footer { margin-top: 1.25rem; color: var(--muted); font-size: 0.78rem; }
  footer a, noscript a, .subtitle a { color: inherit; text-underline-offset: 2px; }
  footer a:hover, .subtitle a:hover { color: var(--accent); }
"""
    + NARROW_LAYOUT_CSS
    + """</style>
</head>
<body>
<main>
<header class="top">
  <div>
    <h1>Model Switcher</h1>
    <p class="subtitle">Statistics · <a href="/control">Control panel</a></p>
  </div>
  <div class="live" role="status">
    <span id="live-dot" class="dot"></span><span id="live-text">Connecting…</span>
  </div>
</header>
<noscript><p>JavaScript is disabled — raw metrics are available at
<a href="/stats/json">/stats/json</a>.</p></noscript>
<section class="kpis" aria-label="Summary">
  <div class="card tile wide">
    <div class="label">Loaded model</div>
    <div id="kpi-model" class="value name idle">–</div>
    <div class="sub"><span id="kpi-model-dot" class="dot good" hidden></span><span id="kpi-model-sub">–</span></div>
  </div>
  <div class="card tile">
    <div class="label">Generation</div>
    <div class="value"><span id="kpi-gen">–</span><span id="kpi-gen-unit" class="unit" hidden>tok/s</span></div>
    <div class="sub">average</div>
  </div>
  <div class="card tile" title="Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)">
    <div class="label">Live gen t/s ≈</div>
    <div id="kpi-live" class="value">–</div>
    <div class="sub">estimate · 15 s</div>
  </div>
  <div class="card tile">
    <div class="label">Requests</div>
    <div id="kpi-reqs" class="value">–</div>
    <div id="kpi-reqs-sub" class="sub">–</div>
  </div>
  <div class="card tile">
    <div class="label">Active now</div>
    <div id="kpi-active" class="value">–</div>
    <div id="kpi-active-sub" class="sub">–</div>
  </div>
  <div id="kpi-loads-tile" class="card tile" title="Model loads: process start until the health check passes.">
    <div class="label">Loads</div>
    <div id="kpi-loads" class="value">–</div>
    <div id="kpi-loads-sub" class="sub">–</div>
  </div>
  <div class="card tile" title="Time with at least one request in progress (including waiting for a model to load); overlapping requests count once">
    <div class="label">Uptime</div>
    <div id="kpi-uptime" class="value">–</div>
    <div id="kpi-uptime-sub" class="sub">since start</div>
  </div>
</section>
<section class="card section">
  <h2>Models</h2>
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>Model</th><th>Reqs</th><th>Errors</th><th title="Generation speed; uses the server's own decode time when available, else first to last streamed chunk">Gen t/s</th>
      <th title="Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)">Live t/s ≈</th>
      <th title="Combined speed while busy @ average concurrent requests">Gen total</th>
      <th title="Prompt processing speed; uses the server's own prefill time when available, else time to first token">Prompt t/s</th>
      <th title="Combined speed while busy @ average concurrent requests">Prompt total</th>
      <th title="Share of request time spent on prompt processing (the rest is generation); counts only requests where both times are known">Prompt share</th>
      <th>TTFT p50 / p95</th><th>Cache</th><th>Gen tok</th><th>Prefill tok</th>
    </tr></thead>
    <tbody id="models"></tbody>
    <tfoot id="totals"></tfoot>
  </table>
  </div>
</section>
<section id="errors-card" class="card section" hidden>
  <h2>Errors</h2>
  <div id="errors" class="chips"></div>
</section>
<footer>Raw data: <a href="/stats/json">/stats/json</a></footer>
</main>
<script>
"use strict";

const DASH = "–";

function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function obj(v) {
  return (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
}

function fmtInt(v) {
  return isNum(v) ? Math.round(v).toLocaleString("en-US") : DASH;
}

function fmtTps(v) {
  return isNum(v) ? v.toFixed(1) : DASH;
}

function fmtCompact(v) {
  if (!isNum(v)) return DASH;
  const n = Math.round(v);
  const a = Math.abs(n);
  if (a < 10000) return n.toLocaleString("en-US");
  if (a < 999950) return (n / 1e3).toFixed(1) + "K";
  if (a < 999995000) return (n / 1e6).toFixed(2) + "M";
  return (n / 1e9).toFixed(2) + "B";
}

function fmtLive(live) {
  const tps = obj(live).tps;
  return isNum(tps) && tps > 0 ? fmtTps(tps) : DASH;
}

function fmtPct(v) {
  return isNum(v) ? Math.round(v * 100) + "%" : DASH;
}

function fmtAggregate(v, fmtRate) {
  if (!v || typeof v !== "object" || Array.isArray(v)) return DASH;
  if (!(isNum(v.tps) && v.tps > 0)) return DASH;
  if (!(isNum(v.concurrency) && v.concurrency > 0)) return DASH;
  return fmtRate(v.tps) + " @" + v.concurrency.toFixed(1);
}

function fmtTtft(t) {
  t = obj(t);
  if (!isNum(t.p50) && !isNum(t.p95)) return DASH;
  return fmtInt(t.p50) + " / " + fmtInt(t.p95);
}

function fmtUptime(s) {
  if (!isNum(s) || s < 0) return DASH;
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (d) return d + "d " + h + "h";
  if (h) return h + "h " + m + "m";
  if (m) return m + "m " + sec + "s";
  return sec + "s";
}

function busyLines(u) {
  u = obj(u);
  const busy = u.busy_seconds;
  const share = u.busy_share;
  if (!isNum(busy) || busy < 0) return null;
  const lines = ["busy " + fmtUptime(busy)];
  if (isNum(share) && share >= 0 && share <= 1) {
    lines.push(fmtPct(share) + " of uptime");
  }
  return lines;
}

function fmtSeconds(ms) {
  return isNum(ms) ? (ms / 1000).toFixed(1) + " s" : DASH;
}

function countSum(counts) {
  counts = obj(counts);
  let total = 0;
  for (const v of Object.values(counts)) {
    if (isNum(v) && v > 0) total += v;
  }
  return total;
}

function loadLines(loads) {
  loads = obj(loads);
  const lines = ["avg " + fmtSeconds(loads.avg_ms)];
  const failed = countSum(loads.failed);
  // Failures win the second line; the tooltip carries both numbers.
  if (failed > 0) {
    lines.push(fmtInt(failed) + " failed");
  } else if (isNum(loads.swaps) && loads.swaps > 0) {
    lines.push(fmtInt(loads.swaps) + (loads.swaps === 1 ? " swap" : " swaps"));
  }
  return lines;
}

function loadTooltip(loads) {
  loads = obj(loads);
  const parts = ["Model loads: process start until the health check passes."];
  if (isNum(loads.swaps) && loads.swaps > 0) {
    parts.push("Swaps: " + fmtInt(loads.swaps) + ".");
  }
  const failed = countSum(loads.failed);
  if (failed > 0) parts.push("Failed: " + fmtInt(failed) + ".");
  const stop = obj(loads.stop);
  if (isNum(stop.count) && stop.count > 0 && isNum(stop.avg_ms)) {
    parts.push("Avg stop of the previous model: " + fmtSeconds(stop.avg_ms) +
      " (not included in the average).");
  }
  return parts.join(" ");
}

function byId(id) {
  return document.getElementById(id);
}

function setText(id, text) {
  const el = byId(id);
  if (el) el.textContent = text;
  return el;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function setCount(id, v) {
  const node = setText(id, fmtCompact(v));
  if (node) node.title = isNum(v) ? fmtInt(v) : "";
  return node;
}

const LONG_NAME = 12;

function currentName(data) {
  return data.current_model == null ? null : String(data.current_model);
}

function renderTiles(data) {
  const current = currentName(data);
  const req = obj(data.requests);
  const conc = obj(data.concurrency);
  const loads = obj(data.loads);
  const thr = obj(data.throughput);

  const model = setText("kpi-model", current == null ? "None" : current);
  if (model) {
    model.className = current == null ? "value name idle" :
      (current.length > LONG_NAME ? "value name long" : "value name");
    model.title = current == null ? "" : current;
  }
  const dot = byId("kpi-model-dot");
  if (dot) dot.hidden = current == null;
  const modelSub = setText("kpi-model-sub", current == null ? "Idle" : "Serving");
  if (modelSub) modelSub.className = current == null ? "" : "serving";

  const genRates = obj(obj(req.rates).generation_tps);
  const gen = fmtTps(genRates.avg);
  setText("kpi-gen", gen);
  const unit = byId("kpi-gen-unit");
  if (unit) unit.hidden = gen === DASH;
  const live = setText("kpi-live", fmtLive(genRates.live));
  if (live) live.className = live.textContent === DASH ? "value idle" : "value";

  setCount("kpi-reqs", req.total);
  const reqSub = byId("kpi-reqs-sub");
  if (reqSub) {
    reqSub.textContent = "";
    reqSub.appendChild(el("span", "line",
      fmtInt(req.failure) + (req.failure === 1 ? " error" : " errors")));
    reqSub.appendChild(el("span", "line", fmtInt(thr.last_60s) + " in last min"));
  }

  setCount("kpi-active", conc.active);
  setText("kpi-active-sub", "peak " + fmtInt(conc.peak));

  setCount("kpi-loads", loads.count);
  const loadsSub = byId("kpi-loads-sub");
  if (loadsSub) {
    loadsSub.textContent = "";
    for (const line of loadLines(loads)) {
      loadsSub.appendChild(el("span", "line", line));
    }
  }
  const loadsTile = byId("kpi-loads-tile");
  if (loadsTile) loadsTile.title = loadTooltip(loads);

  setText("kpi-uptime", fmtUptime(data.uptime_seconds));
  const uptimeSub = byId("kpi-uptime-sub");
  if (uptimeSub) {
    const lines = busyLines(data.utilization);
    uptimeSub.textContent = lines ? "" : "since start";
    for (const line of lines || []) {
      uptimeSub.appendChild(el("span", "line", line));
    }
  }
}

function rowCells(name, req) {
  const r = obj(req);
  const rates = obj(r.rates);
  const gt = obj(rates.generation_tps);
  const pt = obj(rates.prompt_tps);
  const tokens = obj(r.tokens);
  return [
    name,
    fmtInt(r.total),
    r.failure,
    fmtTps(gt.avg),
    fmtLive(gt.live),
    fmtAggregate(gt.avg_aggregate, fmtTps),
    fmtInt(pt.avg),
    fmtAggregate(pt.avg_aggregate, fmtInt),
    r.time_split,
    fmtTtft(r.ttft_ms),
    tokens.cache_hit_rate,
    tokens.completion,
    tokens.prefill,
  ];
}

function nameCell(name, loaded) {
  const td = el("td");
  const label = el("span", "model-name", name);
  label.title = name;
  td.appendChild(label);
  if (loaded) {
    const pill = el("span", "pill");
    pill.appendChild(el("span", "dot good"));
    pill.appendChild(el("span", "", "Loaded"));
    td.appendChild(pill);
  }
  return td;
}

function errorsCell(failure) {
  const td = el("td");
  if (isNum(failure) && failure > 0) {
    td.appendChild(el("span", "", fmtInt(failure)));
  } else {
    td.appendChild(el("span", "muted", fmtInt(failure)));
  }
  return td;
}

function ttftCell(text) {
  const td = el("td");
  if (text === DASH) {
    td.appendChild(el("span", "muted", DASH));
  } else {
    td.appendChild(el("span", "", text));
    td.appendChild(el("span", "muted", " ms"));
  }
  return td;
}

function plainCell(text) {
  return el("td", text === DASH ? "muted" : "", text);
}

function fmtSecs(v) {
  return v.toLocaleString("en-US",
    {minimumFractionDigits: 1, maximumFractionDigits: 1}) + " s";
}

function shareCell(split) {
  const s = obj(split);
  const td = plainCell(fmtPct(s.prefill_share));
  if (td.textContent !== DASH && isNum(s.prefill_s) && isNum(s.decode_s) &&
      isNum(s.requests)) {
    td.title = "Prompt " + fmtSecs(s.prefill_s) + " · Gen " +
      fmtSecs(s.decode_s) + " over " + fmtInt(s.requests) +
      (s.requests === 1 ? " request" : " requests");
  }
  return td;
}

function countCell(v) {
  const td = plainCell(fmtCompact(v));
  td.title = isNum(v) ? fmtInt(v) : "";
  return td;
}

function buildRow(cells, loaded, className) {
  const tr = el("tr", className);
  tr.appendChild(nameCell(cells[0], loaded));
  tr.appendChild(plainCell(cells[1]));
  tr.appendChild(errorsCell(cells[2]));
  for (const text of cells.slice(3, 8)) tr.appendChild(plainCell(text));
  tr.appendChild(shareCell(cells[8]));
  tr.appendChild(ttftCell(cells[9]));
  tr.appendChild(plainCell(fmtPct(cells[10])));
  tr.appendChild(countCell(cells[11]));
  tr.appendChild(countCell(cells[12]));
  return tr;
}

function orderedModels(models, current) {
  const total = (m) => {
    const t = obj(obj(m).requests).total;
    return isNum(t) ? t : 0;
  };
  return Object.entries(obj(models)).sort(function (a, b) {
    const aCur = current != null && a[0] === current;
    const bCur = current != null && b[0] === current;
    if (aCur !== bCur) return aCur ? -1 : 1;
    const diff = total(b[1]) - total(a[1]);
    if (diff) return diff;
    return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0);
  });
}

function renderTable(data) {
  const tbody = byId("models");
  const current = currentName(data);
  if (tbody) {
    tbody.textContent = "";
    for (const [name, m] of orderedModels(data.models, current)) {
      const loaded = name === current;
      tbody.appendChild(buildRow(rowCells(String(name), obj(m).requests),
        loaded, loaded ? "loaded" : ""));
    }
  }
  const tfoot = byId("totals");
  if (tfoot) {
    tfoot.textContent = "";
    tfoot.appendChild(buildRow(rowCells("All models", data.requests),
      false, "all"));
  }
}

function renderErrors(errors) {
  const box = byId("errors");
  const card = byId("errors-card");
  const entries = Object.entries(obj(errors))
    .filter(function (e) { return isNum(e[1]) && e[1] > 0; })
    .sort(function (a, b) {
      return (b[1] - a[1]) || (a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0));
    });
  if (box) {
    box.textContent = "";
    for (const [kind, count] of entries) {
      const chip = el("span", "chip");
      chip.appendChild(el("span", "dot critical"));
      chip.appendChild(el("span", "kind", String(kind)));
      chip.appendChild(el("span", "count", fmtInt(count)));
      box.appendChild(chip);
    }
  }
  if (card) card.hidden = !entries.length;
}

let lastOkAt = null;
let fetchFailed = false;

function renderLive() {
  const dot = byId("live-dot");
  let text;
  let tone;
  if (fetchFailed) {
    tone = "serious";
    text = "Connection lost · " +
      (lastOkAt == null ? "retrying" : "showing last data");
  } else if (lastOkAt == null) {
    tone = "";
    text = "Connecting…";
  } else {
    tone = "good";
    const secs = Math.max(0, Math.floor((Date.now() - lastOkAt) / 1000));
    text = "Live · updated " + secs + "s ago";
  }
  if (dot) dot.className = tone ? "dot " + tone : "dot";
  setText("live-text", text);
}

function render(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return;
  renderTiles(data);
  renderTable(data);
  renderErrors(obj(data.requests).errors);
}

function refresh() {
  fetch("/stats/json").then(function (resp) {
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }).then(function (data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("unexpected payload");
    }
    render(data);
    lastOkAt = Date.now();
    fetchFailed = false;
    renderLive();
  }).catch(function () {
    /* keep last render */
    fetchFailed = true;
    renderLive();
  });
}

refresh();
setInterval(renderLive, 1000);
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
)
