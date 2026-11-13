"""On-demand, loopback-only quota dashboard, and a page that tunes the router's presets (/presets)."""

import argparse
from datetime import datetime, timedelta, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser

# Isolated Python omits the script directory; use the adjacent router.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import preset_edit
import route


SERIES = (
    ("agy-gemini", "agy Gemini", "series-1"),
    ("agy-claude-gpt", "agy Claude and GPT", "series-2"),
    ("codex-chatgpt", "Codex ChatGPT", "series-3"),
    ("ollama-local", "Ollama local", "series-4"),
    ("other", "Other", "series-other"),
)


def row_timestamp(row):
    """Accept both journal clocks without letting a broken line hide history."""
    try:
        value = float(row["ts"])
        if math.isfinite(value):
            return value
    except (KeyError, TypeError, ValueError):
        pass
    parsed = route._parse_iso_utc(row.get("time"))
    return parsed.timestamp() if parsed else 0.0


def token_total(row):
    tokens = row.get("tokens") or {}
    if not isinstance(tokens, dict):
        return 0
    total = tokens.get("total")
    if total is None:
        total = (tokens.get("in") or 0) + (tokens.get("out") or 0)
    try:
        return max(0, int(total or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def journal_series(row):
    provider = str(row.get("provider") or "").lower()
    pool = str(row.get("pool") or "").lower()
    model = str(row.get("model") or row.get("name") or "").lower()
    if provider == "ollama":
        return "ollama-local"
    if provider == "codex" and (not pool or pool.startswith("chatgpt") or pool == "codex"):
        return "codex-chatgpt"
    if provider == "agy":
        if pool in ("gemini", "gemini models") or (not pool and model.startswith("gemini-")):
            return "agy-gemini"
        if pool in ("claude and gpt models", "claude/gpt", "claude and gpt") or (
            not pool and model.startswith(("claude-", "gpt-oss-"))
        ):
            return "agy-claude-gpt"
    return "other"


RECENT_KEYS = ("machine", "time", "ts", "script", "role", "name", "model", "provider", "pool", "status", "tokens", "seconds",
               "type", "event", "verdict", "why", "crossed", "context")


def finite(value):
    """JSON with allow_nan=False refuses NaN and Infinity; a journal row may carry them (json.loads accepts both)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    return value


def journal_views(rows, now):
    # UTC days match the journal's timestamps and remain stable across DST.
    today = datetime.fromtimestamp(now, timezone.utc).date()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(13, -1, -1)]
    days = {day: {key: 0 for key, _, _ in SERIES} for day in dates}
    valid = [row for row in rows if isinstance(row, dict)]
    for row in valid:
        stamp = row_timestamp(row)
        if stamp > now or "type" in row:
            continue
        try:
            day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        if day in days:
            days[day][journal_series(row)] += token_total(row)
    recent = []
    newest = sorted(enumerate(valid), key=lambda item: (row_timestamp(item[1]), item[0]), reverse=True)
    for _, row in newest[:20]:
        # Only what the table shows: no log paths, account folders or notes (the page is served to any local browser).
        entry = {k: row[k] for k in RECENT_KEYS if k in row}
        entry["total_tokens"] = token_total(entry)
        if entry.get("type") == "verdict":
            # (A verdict is a judgement on a run, not a call: no tokens of its own, so "—" rather than 0.)
            entry["status"] = entry.get("verdict", entry.get("status", "verdict"))
            entry["script"] = "verdict"
            entry["total_tokens"] = None
        if entry.get("type") == "handoff":
            entry["status"] = "handoff " + entry.get("event", "")
            entry["total_tokens"] = None
        recent.append(entry)
    return {
        "timezone": "UTC",
        "series": [{"id": key, "label": label, "color": color} for key, label, color in SERIES],
        "days": [{"day": day, "tokens": days[day]} for day in dates],
    }, recent


def window_status(window, rules):
    floors = rules.get("floors") or {"weekly": 0.10, "5h": 0.10}
    kind = window.get("window") or "window"
    floor = float(floors.get(kind, floors.get("weekly", 0.10)))
    if float(window.get("remaining_fraction", 0.0)) < floor:
        return "critical"
    if "week" in kind.lower() and window.get("pace") is not None:
        if float(window["pace"]) > float(rules.get("pace_skip", 2.0)):
            return "warning"
    return "good"


def gather_state(fake=None, journal_path=None, now=None):
    now = float(time.time() if now is None else now)
    rules = route.load_roles()
    fake = route.resolve_fake(fake)
    agy, ollama = route.load_config() if fake is None else (None, None)
    cloud = route.cloud_state(fake=fake, agy_path=agy, rules=rules, now=now, ttl=60)
    local = route.local_state(fake=fake, ollama_url=ollama, rules=rules)
    for pool in cloud.get("pools", []):
        for window in pool.get("windows", []):
            window["status"] = window_status(window, rules)
            # Python formatting preserves --status rounding at decimal ties.
            window["remaining_percent"] = f"{float(window.get('remaining_fraction', 0)) * 100:.1f}%"
    chart, recent = journal_views(route._read_journal_rows(journal_path=journal_path, fake=fake), now)
    return {
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "cloud": cloud,
        "local": local,
        "chart": chart,
        "journal": recent,
        "gpu_display": {
            "vram": f"{local['used_gb']:.1f} / {local['total_gb']:.1f} GB VRAM",
            "free": f"{local['free_gb']:.1f} GB free",
            "loaded": [
                f"{item.get('name', '?')} ({route._to_gb(item.get('size_vram') or 0):.1f} GB)"
                for item in local.get("loaded", [])
            ],
        },
    }


class StateStore:
    def __init__(self, fake=None, journal_path=None, ttl=60):
        self.fake = fake
        self.journal_path = journal_path
        self.ttl = ttl
        self._lock = threading.Lock()
        self._state = None
        self._gathered_at = 0.0

    def get(self):
        # Share a completed gather across tabs, including concurrent first loads.
        with self._lock:
            if self._state is None or time.monotonic() - self._gathered_at >= self.ttl:
                self._state = gather_state(fake=self.fake, journal_path=self.journal_path)
                self._gathered_at = time.monotonic()
            return self._state


STYLE = r''':root {
  color-scheme: light;
  --surface: #fcfcfb; --background: #f3f3f0; --text: #0b0b0b; --secondary: #52514e;
  --border: #ddddd7; --grid: #eaeae5; --track: #e5e5df;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --series-other: #82827d; --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19; --background: #121211; --text: #f0efec; --secondary: #c3c2b7;
    --border: #3b3b35; --grid: #2b2b27; --track: #383832;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
    --series-other: #92928b;
    --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  }
}
* { box-sizing: border-box; }
a { color: var(--series-1); }
body { margin: 0; background: var(--background); color: var(--text); font: 15px/1.5 system-ui, sans-serif; }
main { max-width: 1320px; margin: auto; padding: 28px 22px 48px; }
h1 { margin: 0; font-size: 28px; letter-spacing: -.6px; }
h2 { margin: 0 0 12px; font-size: 19px; }
h3 { margin: 0; font-size: 16px; }
p { margin: 8px 0; }
.muted, .meta { color: var(--secondary); }
.meta { margin: 5px 0 22px; }
#notice:empty, #error:empty { display: none; }
#error { border-left: 3px solid var(--serious); padding: 10px; background: var(--surface); }
#pools { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 285px), 1fr)); gap: 14px; }
.card, .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; min-width: 0; }
.panel { margin-top: 18px; }
.heading { display: flex; gap: 12px; justify-content: space-between; align-items: flex-start; }
.status { display: inline-flex; gap: 5px; align-items: center; white-space: nowrap; font-size: 12px; font-weight: 650; }
.status svg { width: 15px; height: 15px; stroke: var(--status-color); fill: none; stroke-width: 2; flex-shrink: 0; }
.good { --status-color: var(--good); }
.warning { --status-color: var(--warning); }
.critical { --status-color: var(--critical); }
.big { font-size: 38px; font-weight: 650; letter-spacing: -1px; margin: 8px 0 4px; }
meter { display: block; width: 100%; height: 7px; border: 0; border-radius: 4px; background: var(--track); }
meter::-webkit-meter-bar { border: 0; background: var(--track); }
meter::-webkit-meter-optimum-value { background: var(--status-color, var(--good)); }
meter::-moz-meter-bar { background: var(--status-color, var(--good)); }
.details { font-size: 13px; overflow-wrap: anywhere; }
.reservations { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 8px; }
ul { padding-left: 20px; margin: 8px 0; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 0 0 12px; color: var(--secondary); font-size: 13px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; background: var(--swatch); }
#chart { display: block; width: 100%; height: auto; min-width: 540px; }
.chart-scroll, .table-scroll { overflow-x: auto; }
.chart-text { fill: var(--secondary); font-size: 12px; }
.grid { stroke: var(--grid); stroke-width: 1; }
.segment { stroke: var(--surface); stroke-width: 2; }
.segment:focus { outline: none; stroke: var(--text); }
#tooltip { position: fixed; z-index: 10; max-width: min(300px, 90vw); pointer-events: none; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { color: var(--secondary); font-weight: 600; text-align: left; }
td, th { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
td { overflow-wrap: anywhere; max-width: 260px; }
td:first-child { white-space: nowrap; }
.number { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 600px) { main { padding: 18px 12px 32px; } .card, .panel { padding: 14px; } h1 { font-size: 24px; } }
'''


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Local limits</title>
<style>
__STYLE__</style>
</head>
<body>
<main>
<header><h1>Local limits</h1><p class="meta"><span id="stamp">Waiting for data</span> · refreshes every 60 s<span id="mode"></span><span id="nav" hidden> · <a id="to-presets">Presets</a></span></p>
<p id="notice" class="muted"></p><p id="error" role="status"></p></header>
<section id="pools" aria-label="Quota pools and windows"></section>
<section class="panel" id="gpu" aria-label="GPU and Ollama"><h2>GPU and Ollama</h2><p class="muted">Waiting for data</p></section>
<section class="panel"><h2>Tokens per day <span class="muted">· last 14 days · UTC</span></h2>
<div id="legend" class="legend"></div><div class="chart-scroll"><svg id="chart" viewBox="0 0 980 300" role="img" aria-label="Daily tokens stacked by provider and pool"></svg></div></section>
<section class="panel"><h2>Recent journal <span class="muted">· last 20 lines</span></h2>
<div class="table-scroll"><table><thead><tr><th>Time</th><th>Script</th><th>Role</th><th>Model</th><th>Provider</th><th class="number">Tokens</th><th class="number">Seconds</th><th>Status</th></tr></thead><tbody id="journal"></tbody></table></div></section>
<noscript>This dashboard needs JavaScript to display its local data.</noscript>
</main><div id="tooltip" role="tooltip" hidden></div>
<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script>
'use strict';
const $ = id => document.getElementById(id);
const num = value => (value === null || value === undefined) ? '—' : Number(value).toLocaleString('en-US');
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function svg(tag, attrs = {}, text) {
  const node = document.createElementNS($('chart').namespaceURI, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}
function badge(status, word) {
  const node = el('span', null, 'status ' + status);
  const icon = svg('svg', {viewBox: '0 0 16 16', 'aria-hidden': 'true'});
  icon.append(svg('circle', {cx: 8, cy: 8, r: 6}));
  icon.append(svg('path', {d: status === 'good' ? 'M4.5 8l2.3 2.3 4.7-4.7' : 'M8 4v5m0 2v1'}));
  node.append(icon, document.createTextNode(word || status[0].toUpperCase() + status.slice(1)));
  return node;
}
function meter(value, label) {
  const node = el('meter'); node.min = 0; node.max = 1;
  node.value = Math.max(0, Math.min(1, value)); node.setAttribute('aria-label', label);
  return node;
}
function poolCard(pool, win) {
  const card = el('article', null, 'card ' + win.status);
  const heading = el('div', null, 'heading');
  const title = el('div'); title.append(el('h3', pool.label || pool.id));
  if (pool.checked) title.append(el('div', pool.machines.join(', ') + ' · same account (resets match)', 'muted details'));
  title.append(el('div', win.label || win.window, 'muted details'));
  heading.append(title, badge(win.status)); card.append(heading);
  card.append(el('div', win.remaining_percent, 'big'), meter(win.remaining_fraction, 'Quota remaining'));
  card.append(el('p', 'Resets in ' + (win.resets_in || '?'), 'details'));
  card.append(el('p', win.pace_str || '?', 'details'));
  card.append(el('p', 'Covers: ' + ((win.covers && win.covers.length ? win.covers : pool.covers) || []).join(', '), 'muted details'));
  if (pool.stale) card.append(el('p', 'Stale reading', 'muted details'));
  if (pool.account_note) card.append(el('p', pool.account_note, 'muted details'));
  if (pool.checked) card.append(el('p', num(win.used_percent) + ' % used', 'details'),
    el('p', 'Data: ' + (win.age_s == null ? 'unknown age' : Math.floor(win.age_s / 60) + ' min old') + ' · ' + win.machine, 'muted details'));
  const reservations = el('div', null, 'reservations details');
  const live = win.reservations || pool.reservations || [];
  if (!live.length) reservations.append(el('span', 'No live reservations', 'muted'));
  for (const item of live) {
    if (item.machine) reservations.append(el('span', item.machine, 'muted'));
    const shares = Object.entries(item.share || {}).map(([key, val]) => key + ' ' + (Number(val) * 100).toFixed(1) + '%').join(', ');
    reservations.append(el('p', (item.script || 'direct') + ' · ' + (item.model || '?') + ' · ~' + num(item.tokens) + ' tokens · ' + (shares || 'share unknown')));
  }
  card.append(reservations); return card;
}
function renderGPU(state) {
  const loc = state.local, display = state.gpu_display, box = $('gpu'); box.replaceChildren();
  const heading = el('div', null, 'heading'); heading.append(el('h2', 'GPU and Ollama'), badge(loc.free ? 'good' : 'warning', loc.free ? 'FREE' : 'BUSY'));
  box.append(heading, el('p', display.vram + ' · ' + display.free));
  const bar = meter(loc.total_gb ? loc.used_gb / loc.total_gb : 0, 'VRAM used');
  bar.className = loc.free ? 'good' : 'warning'; box.append(bar);
  box.append(el('p', loc.utilization + '% utilisation', 'muted details'));
  if (loc.reasons && loc.reasons.length) { const reasons = el('ul'); for (const reason of loc.reasons) reasons.append(el('li', reason)); box.append(reasons); }
  box.append(el('p', 'Ollama ' + (loc.ollama_up ? 'up' : 'down') + ' · loaded: ' + (display.loaded.join(', ') || 'none'), 'details'));
}
function claudePanel(account) {
  const panel = el('section', null, 'panel'); panel.style.gridColumn = '1 / -1';
  panel.append(el('h2', account.label));
  const cards = el('div'); cards.style.display = 'grid';
  cards.style.gridTemplateColumns = 'repeat(auto-fit, minmax(240px, 1fr))'; cards.style.gap = '16px';
  for (const win of account.windows) {
    const card = el('article', null, 'card');
    card.append(el('h3', win.label), el('div', win.used_percent + ' % used', 'big'),
      meter(win.used_percent / 100, 'Quota used'), el('p', win.left_percent + ' % left'),
      el('p', 'Resets in ' + (win.resets_in || '?'), 'details'), el('p', win.pace_str || '?', 'details'),
      el('p', 'Data: ' + (win.age_s == null ? 'unknown age' : Math.floor(win.age_s / 60) + ' min old') +
        ' · ' + win.source + ' · ' + win.machine, 'muted details'));
    if (win.stale) card.append(el('p', 'Stale', 'muted details'));
    cards.append(card);
  }
  panel.append(cards);
  if (account.extra_usage) panel.append(el('p', Object.entries(account.extra_usage).map(([key, value]) =>
    key + ': ' + (typeof value === 'object' ? JSON.stringify(value) : String(value))).join(' · '), 'details'));
  return panel;
}
function renderChart(chart) {
  const root = $('chart'), legend = $('legend'), tip = $('tooltip');
  root.replaceChildren(); legend.replaceChildren(); tip.hidden = true;
  const series = chart.series.filter(item => item.id !== 'other' || chart.days.some(day => day.tokens.other > 0));
  for (const item of series) {
    const entry = el('span'), swatch = el('i', null, 'swatch'); swatch.style.setProperty('--swatch', 'var(--' + item.color + ')');
    entry.append(swatch, document.createTextNode(item.label)); legend.append(entry);
  }
  const left = 72, top = 18, height = 230, width = 890, step = width / 14, barWidth = 38;
  const peak = Math.max(1, ...chart.days.map(day => Object.values(day.tokens).reduce((sum, value) => sum + value, 0)));
  const magnitude = 10 ** Math.floor(Math.log10(peak));
  const ceiling = Math.ceil(peak / magnitude) * magnitude;
  for (let tick = 0; tick <= 4; tick++) {
    const y = top + height - height * tick / 4;
    root.append(svg('line', {x1: left, y1: y, x2: left + width, y2: y, class: 'grid'}));
    root.append(svg('text', {x: left - 10, y: y + 4, 'text-anchor': 'end', class: 'chart-text'}, num(Math.round(ceiling * tick / 4))));
  }
  chart.days.forEach((day, index) => {
    const x = left + index * step + (step - barWidth) / 2; let bottom = top + height;
    for (const item of series) {
      const tokens = day.tokens[item.id] || 0; if (!tokens) continue;
      const h = tokens / ceiling * height, y = bottom - h;
      const rounded = Math.min(4, h / 2);
      const path = svg('path', {d: `M${x},${bottom}V${y + rounded}Q${x},${y} ${x + rounded},${y}H${x + barWidth - rounded}Q${x + barWidth},${y} ${x + barWidth},${y + rounded}V${bottom}Z`, fill: 'var(--' + item.color + ')', class: 'segment', tabindex: 0});
      const label = day.day + ' · ' + item.label + ' · ' + num(tokens) + ' tokens';
      path.setAttribute('aria-label', label); path.append(svg('title', {}, label));
      function show(event) {
        tip.textContent = label; tip.hidden = false;
        const bounds = path.getBoundingClientRect();
        const px = event.clientX === undefined ? bounds.left : event.clientX;
        const py = event.clientY === undefined ? bounds.top : event.clientY;
        tip.style.left = Math.max(8, Math.min(window.innerWidth - tip.offsetWidth - 8, px + 12)) + 'px';
        tip.style.top = Math.max(8, Math.min(window.innerHeight - tip.offsetHeight - 8, py + 12)) + 'px';
      }
      path.addEventListener('pointermove', show); path.addEventListener('focus', show);
      path.addEventListener('pointerleave', () => tip.hidden = true); path.addEventListener('blur', () => tip.hidden = true);
      root.append(path); bottom = y;
    }
    root.append(svg('text', {x: x + barWidth / 2, y: top + height + 24, 'text-anchor': 'middle', class: 'chart-text'}, day.day.slice(5)));
  });
}
function render(state) {
  $('stamp').textContent = 'Data: ' + new Date(state.generated_at).toLocaleString();
  const cloud = state.cloud, sub = cloud.claude_subscription || {}, notes = [];
  if (sub.status === 'stale' || Number(sub.age_s || 0) > 21600) notes.push('Claude subscription file is stale (> 6 h); read at ' + (sub.read_at || 'unknown'));
  if (sub.status === 'missing') notes.push('Claude subscription file is missing.');
  if (!cloud.ok) notes.push('agy usage: ' + (cloud.error || 'unavailable'));
  if (cloud.codex_error) notes.push('Codex usage: ' + cloud.codex_error);
  $('notice').textContent = notes.join(' · ');
  $('pools').replaceChildren();
  if (state.server_mode) {
    const selector = $('machines'); selector.replaceChildren();
    for (const name of ['all', ...state.machine_names]) {
      const button = el('button', name); button.type = 'button';
      button.setAttribute('aria-pressed', String(name === selectedMachine));
      button.addEventListener('click', () => { selectedMachine = name; refreshStats(); }); selector.append(button);
    }
    for (const account of state.claude || []) $('pools').append(claudePanel(account));
    const accounts = (state.accounts || []).filter(account => account.checked);
    if (accounts.length) {
      const heading = el('h2', 'Accounts'); heading.style.gridColumn = '1 / -1'; $('pools').append(heading);
      for (const account of accounts) for (const win of account.windows) $('pools').append(poolCard(account, win));
    }
    for (const machine of state.machines) {
      const heading = el('div'); heading.style.gridColumn = '1 / -1';
      heading.append(el('h2', machine.machine), el('p', 'last push ' + Math.floor(machine.age_s / 60) + ' min ago' + (machine.stale ? ' · stale: no push' : ''), 'muted'));
      for (const [name, age] of Object.entries(machine.snapshot_ages)) heading.append(el('p', name + ': ' + Math.floor(age / 60) + ' min old', 'muted details'));
      for (const error of machine.errors) heading.append(el('p', error, 'muted'));
      $('pools').append(heading);
      for (const pool of machine.pools) for (const win of pool.windows || []) $('pools').append(poolCard(pool, win));
    }
  } else for (const pool of cloud.pools || []) for (const win of pool.windows || []) $('pools').append(poolCard(pool, win));
  if (!$('pools').childElementCount) $('pools').append(el('p', 'No quota windows available.', 'muted'));
  if (!state.server_mode) renderGPU(state); renderChart(state.chart);
  const body = $('journal'); body.replaceChildren();
  for (const item of state.journal) {
    const row = el('tr');
    const values = [item.time || (item.ts ? new Date(item.ts * 1000).toISOString() : '—'), item.script, item.role, item.model || item.name, item.provider, num(item.total_tokens), item.seconds, item.status];
    if (state.server_mode) row.append(el('td', item.machine));
    values.forEach((value, index) => row.append(el('td', value === undefined || value === null ? '—' : (typeof value === 'object' ? JSON.stringify(value) : value), index === 5 || index === 6 ? 'number' : '')));
    body.append(row);
  }
  if (!state.journal.length) { const row = el('tr'), cell = el('td', 'No journal entries.'); cell.colSpan = 8; row.append(cell); body.append(row); }
}
const snapshot = JSON.parse($('snapshot').textContent);
let selectedMachine = 'all', refreshStats = () => {};
/* The token rides in the address's fragment, which never reaches the server; the link to the presets page carries it on. */
if (snapshot !== null) { $('mode').textContent = ' · snapshot (automatic refresh disabled)'; render(snapshot); }
else {
  $('to-presets').href = '/presets' + location.hash; $('nav').hidden = false;
  let loading = false, lastAttempt = 0;
  async function refresh() {
    if (document.hidden || loading || Date.now() - lastAttempt < 60000) return;
    loading = true; lastAttempt = Date.now();
    try {
      const response = await fetch('/api/state', {cache: 'no-store'});
      if (!response.ok) throw new Error('Local server returned ' + response.status);
      render(await response.json()); $('error').textContent = '';
    } catch (error) { $('error').textContent = 'Refresh failed: ' + error.message + '. Retrying in 60 s.'; }
    finally { loading = false; }
  }
  refreshStats = () => { lastAttempt = 0; refresh(); };
  refresh(); setInterval(refresh, 60000); document.addEventListener('visibilitychange', refresh);
}
</script>
</body></html>
'''


def page_html(state=None, server_mode=False):
    # Escaping '<' prevents journal text from closing the JSON script element.
    data = json.dumps(finite(state), ensure_ascii=True, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = PAGE.replace("__STYLE__", STYLE).replace("__SNAPSHOT__", data)
    if server_mode:
        page = page.replace("Local limits", "Switchboard stats")
        page = page.replace('<section id="pools"', '<div id="machines"></div><section id="pools"')
        page = page.replace('<th>Time</th>', '<th>Machine</th><th>Time</th>')
        page = page.replace('<section class="panel" id="gpu"', '<section hidden class="panel" id="gpu"')
        page = page.replace("$('nav').hidden = false;", "$('nav').hidden = true;")
        page = page.replace("fetch('/api/state',", "fetch('/api/state?machine=' + encodeURIComponent(selectedMachine),")
        page = page.replace("60000", "30000").replace("60 s", "30 s")
    return page


PRESETS_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Presets</title>
<style>
__STYLE__
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
button { font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button:hover:not(:disabled) { border-color: var(--secondary); }
button:disabled { opacity: .45; cursor: default; }
button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
.tabs button[aria-pressed="true"] { background: var(--text); color: var(--surface); border-color: var(--text); }
.primary { background: var(--series-1); border-color: var(--series-1); color: #fff; }
.small { padding: 2px 8px; font-size: 13px; }
select { font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 5px 8px; max-width: 100%; min-width: 0; }
#roles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); gap: 14px; }
.rows { list-style: none; padding: 0; margin: 10px 0; }
.rows li { display: flex; gap: 8px; align-items: center; padding: 6px 8px; margin: 4px 0; border: 1px solid var(--border); border-radius: 8px; background: var(--background); }
.rows li.dragging { opacity: .5; }
.rows li.over { border-color: var(--series-1); }
.rows .label { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 13px; }
.rows .place { color: var(--secondary); font-variant-numeric: tabular-nums; width: 1.4em; }
.handle { cursor: grab; color: var(--secondary); user-select: none; }
.off li { opacity: .65; border-style: dashed; }
.add { display: flex; gap: 8px; align-items: center; }
.add select { flex: 1; }
.pick { font-size: 13px; overflow-wrap: anywhere; }
.pick strong { font-weight: 650; }
.slider { display: grid; grid-template-columns: 5.5em 1fr 6.5em; gap: 12px; align-items: center; margin: 10px 0; }
.slider input { width: 100%; }
.slider output { font-variant-numeric: tabular-nums; text-align: right; }
.changed { color: var(--series-2); font-weight: 650; }
#bar { position: sticky; bottom: 0; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: flex-end; margin-top: 18px; padding: 12px 0; background: var(--background); border-top: 1px solid var(--border); }
#bar #dirty { margin-right: auto; }
#message:empty { display: none; }
@media (max-width: 600px) { .slider { grid-template-columns: 4.5em 1fr 4.5em; gap: 8px; } }
</style>
</head>
<body>
<main>
<header><h1>Presets</h1><p class="meta"><a id="to-limits" href="/">Local limits</a> · <span id="where">Waiting for data</span></p>
<p id="notice" class="muted"></p><p id="error" role="alert"></p></header>
<div class="tabs" id="tabs" role="group" aria-label="Preset"></div>
<section id="view" aria-live="polite"></section>
<div id="bar"><span id="dirty" class="muted">No changes</span><span id="message" class="muted" role="status"></span>
<button id="revert" type="button" disabled>Revert</button><button id="save" type="button" class="primary" disabled>Save</button></div>
<noscript>This page needs JavaScript.</noscript>
</main>
<script>
'use strict';
const $ = id => document.getElementById(id);
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
const clone = value => JSON.parse(JSON.stringify(value));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const WEIGHTS = [['quality', 'Quality', 2], ['time', 'Time', 3], ['cost', 'Cost', 3]];
let token = '';
const fromHash = new URLSearchParams(location.hash.slice(1)).get('token');
if (fromHash) { token = fromHash; try { sessionStorage.setItem('switchboard-token', token); } catch (e) {} }
else { try { token = sessionStorage.getItem('switchboard-token') || ''; } catch (e) {} }
$('to-limits').href = '/' + (token ? '#token=' + encodeURIComponent(token) : '');
let saved = null, draft = null, picks = null, current = null, previewTimer = null, previewSeq = 0;

async function call(path, body) {
  const options = body === undefined ? {cache: 'no-store'} : {method: 'POST', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-Switchboard-Token': token}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (e) {}
  if (!response.ok) { const error = new Error((data && data.error) || ('Local server returned ' + response.status)); error.status = response.status; throw error; }
  return data;
}
function fromState(state) {
  const out = {presets: {}, roles: {}};
  for (const p of state.presets) if (p.weighted) out.presets[p.name] = clone(p.weights);
  for (const r of state.roles) out.roles[r.name] = {rows: r.rows.map(x => x.row), off: r.off.map(x => x.row)};
  return out;
}
function edits() {
  const base = fromState(saved), list = [];
  for (const [name, weights] of Object.entries(draft.presets)) {
    if (same(weights, base.presets[name])) continue;
    const changed = {};
    for (const [key] of WEIGHTS) if (weights[key] !== null && weights[key] !== undefined) changed[key] = weights[key];
    list.push({preset: name, weights: changed});
  }
  for (const [name, role] of Object.entries(draft.roles)) if (!same(role, base.roles[name])) list.push({role: name, rows: role.rows, off: role.off});
  return list;
}
function label(row) {
  const known = new Map();
  for (const r of saved.roles) for (const x of r.rows.concat(r.off)) known.set(JSON.stringify(x.row), x.label);
  if (known.has(JSON.stringify(row))) return known.get(JSON.stringify(row));
  return row.model + ' · ' + row.provider;
}
function changedFlag() {
  const list = edits();
  $('dirty').textContent = list.length ? list.length + ' unsaved change' + (list.length > 1 ? 's' : '') : 'No changes';
  $('dirty').className = list.length ? 'changed' : 'muted';
  $('save').disabled = !list.length || !token; $('revert').disabled = !list.length;
  return list;
}
function schedulePreview() {
  const list = changedFlag();
  clearTimeout(previewTimer);
  if (!list.length) { picks = saved.picks; renderPicks(); return; }
  if (!token) return;
  previewTimer = setTimeout(async () => {
    const seq = ++previewSeq;
    document.querySelectorAll('.pick-value').forEach(node => node.classList.add('muted'));
    try {
      const result = await call('/api/presets/preview', {edits: list});
      if (seq === previewSeq) { picks = result.picks; $('error').textContent = ''; renderPicks(); }
    } catch (error) { if (seq === previewSeq) $('error').textContent = 'Preview failed: ' + error.message; }
  }, 450);
}
function pickText(role, name) {
  const row = (picks || {})[role];
  if (!row) return '…';
  const model = row.picks[name];
  return model || ('none' + (row.note ? ' (' + row.note + ')' : ''));
}
function renderPicks() {
  document.querySelectorAll('[data-pick-role]').forEach(node => {
    node.textContent = pickText(node.dataset.pickRole, node.dataset.pickPreset); node.classList.remove('muted');
  });
}
function renderTabs() {
  const tabs = $('tabs'); tabs.replaceChildren();
  for (const p of saved.presets) {
    const button = el('button', p.name + (p.name === saved.active.preset ? ' (active)' : ''));
    button.type = 'button'; button.setAttribute('aria-pressed', String(p.name === current));
    button.addEventListener('click', () => { current = p.name; renderTabs(); renderView(); });
    tabs.append(button);
  }
}
function pickLine(role, name) {
  const line = el('p', null, 'pick'); line.append(document.createTextNode('Would pick now: '));
  const value = el('strong', pickText(role, name), 'pick-value');
  value.dataset.pickRole = role; value.dataset.pickPreset = name; line.append(value);
  return line;
}
function move(role, from, to) {
  const rows = draft.roles[role].rows;
  if (to < 0 || to >= rows.length || from === to) return;
  rows.splice(to, 0, rows.splice(from, 1)[0]); renderView(); schedulePreview();
}
function roleCard(role) {
  const card = el('article', null, 'card'), spec = draft.roles[role.name];
  const heading = el('div', null, 'heading'); heading.append(el('h3', role.name));
  if (role.own) heading.append(el('span', 'yours', 'muted details'));
  card.append(heading, pickLine(role.name, 'manual'));
  const list = el('ol', null, 'rows'); list.setAttribute('aria-label', role.name + ' rows in order');
  spec.rows.forEach((row, index) => {
    const item = el('li'); item.draggable = true;
    item.append(el('span', '⋮⋮', 'handle'), el('span', index + 1, 'place'), el('span', label(row), 'label'));
    const up = el('button', '↑', 'small'), down = el('button', '↓', 'small'), off = el('button', 'Off', 'small');
    up.type = down.type = off.type = 'button';
    up.setAttribute('aria-label', 'Move up'); down.setAttribute('aria-label', 'Move down'); off.setAttribute('aria-label', 'Switch off');
    up.disabled = index === 0; down.disabled = index === spec.rows.length - 1; off.disabled = spec.rows.length === 1;
    up.addEventListener('click', () => move(role.name, index, index - 1));
    down.addEventListener('click', () => move(role.name, index, index + 1));
    off.addEventListener('click', () => { spec.off.push(spec.rows.splice(index, 1)[0]); renderView(); schedulePreview(); });
    item.append(up, down, off);
    item.addEventListener('dragstart', event => { event.dataTransfer.setData('text/plain', role.name + '\n' + index); event.dataTransfer.effectAllowed = 'move'; item.classList.add('dragging'); });
    item.addEventListener('dragend', () => item.classList.remove('dragging'));
    item.addEventListener('dragover', event => { event.preventDefault(); item.classList.add('over'); });
    item.addEventListener('dragleave', () => item.classList.remove('over'));
    item.addEventListener('drop', event => {
      event.preventDefault(); item.classList.remove('over');
      const [from, at] = event.dataTransfer.getData('text/plain').split('\n');
      if (from === role.name) move(role.name, Number(at), index);
    });
    list.append(item);
  });
  card.append(list);
  if (spec.off.length) {
    card.append(el('p', 'Switched off', 'muted details'));
    const offList = el('ul', null, 'rows off');
    spec.off.forEach((row, index) => {
      const item = el('li'); item.append(el('span', label(row), 'label'));
      const on = el('button', 'On', 'small'); on.type = 'button'; on.setAttribute('aria-label', 'Switch back on');
      on.addEventListener('click', () => { spec.rows.push(spec.off.splice(index, 1)[0]); renderView(); schedulePreview(); });
      item.append(on); offList.append(item);
    });
    card.append(offList);
  }
  const named = new Set(spec.rows.concat(spec.off).filter(r => r && r.model).map(r => r.provider + '\n' + r.model));
  const free = saved.models.filter(m => !named.has(m.provider + '\n' + m.model));
  if (free.length) {
    const add = el('div', null, 'add'), select = el('select'), button = el('button', 'Add', 'small');
    select.setAttribute('aria-label', 'A model of this machine to add to ' + role.name);
    const hint = el('option', 'Add a model of this machine…'); hint.value = ''; select.append(hint);
    for (const m of free) { const option = el('option', m.label); option.value = JSON.stringify({provider: m.provider, model: m.model}); select.append(option); }
    button.type = 'button'; button.disabled = true;
    select.addEventListener('change', () => { button.disabled = !select.value; });
    button.addEventListener('click', () => { if (!select.value) return; spec.rows.push(JSON.parse(select.value)); renderView(); schedulePreview(); });
    add.append(select, button); card.append(add);
  }
  return card;
}
function weightedView(name) {
  const box = el('div'), panel = el('section', null, 'panel'), weights = draft.presets[name];
  const base = fromState(saved).presets[name];
  panel.append(el('h2', name), el('p', 'Weighted presets are shadow only for now: the router records what this one would pick and still calls the manual pick. Each sum is quality × Q − time × T − cost × C, per role; a weight left unset takes the role\'s own.', 'muted details'));
  for (const [key, title, max] of WEIGHTS) {
    const row = el('div', null, 'slider'), id = 'w-' + key;
    const caption = el('label', title); caption.htmlFor = id;
    const input = el('input'); input.type = 'range'; input.id = id; input.min = 0; input.step = 0.05;
    input.max = Math.max(max, Number(weights[key]) || 0);
    input.value = weights[key] === null || weights[key] === undefined ? 0 : weights[key];
    const out = el('output'); out.htmlFor = id;
    const show = () => {
      const value = weights[key];
      out.textContent = value === null || value === undefined ? "role's own" : Number(value).toFixed(2);
      out.className = same(value, base[key]) ? '' : 'changed';
    };
    input.addEventListener('input', () => { weights[key] = Math.round(Number(input.value) * 100) / 100; show(); schedulePreview(); });
    show(); row.append(caption, input, out); panel.append(row);
  }
  box.append(panel);
  const table = el('section', null, 'panel'); table.append(el('h2', 'Whom each role would pick now'));
  const scroll = el('div', null, 'table-scroll'), grid = el('table'), head = el('tr');
  for (const text of ['Role', 'manual', name]) head.append(el('th', text));
  const thead = el('thead'); thead.append(head); grid.append(thead);
  const body = el('tbody');
  for (const role of saved.roles) {
    const tr = el('tr'); tr.append(el('td', role.name));
    for (const preset of ['manual', name]) {
      const td = el('td'), value = el('span', pickText(role.name, preset), 'pick-value');
      value.dataset.pickRole = role.name; value.dataset.pickPreset = preset; td.append(value);
      if (preset !== 'manual' && !role.scored) td.append(el('span', ' · no score: repeats manual', 'muted'));
      tr.append(td);
    }
    body.append(tr);
  }
  grid.append(body); scroll.append(grid); table.append(scroll); box.append(table);
  return box;
}
function renderView() {
  const view = $('view'); view.replaceChildren();
  if (current === 'manual') {
    view.append(el('p', 'The manual preset is every role\'s rows in order: drag a row or use the arrows, switch one off (it stays, not called), or add a model of this machine. Claude subscription rows stay the last resort whatever their place.', 'muted details'));
    const grid = el('section'); grid.id = 'roles';
    for (const role of saved.roles) grid.append(roleCard(role));
    view.append(grid);
  } else {
    view.append(weightedView(current));
  }
  renderPicks();
}
function load(state, keepView) {
  saved = state; draft = fromState(state); picks = state.picks;
  if (!keepView || !saved.presets.some(p => p.name === current)) current = state.active.preset;
  $('where').textContent = state.path + (state.target !== state.path ? ' → ' + state.target : '') + ' · active: ' + state.active.preset + ' (' + state.active.source + ')';
  const notes = [];
  if (state.ignored) notes.push(state.ignored);
  if (state.active.error) notes.push(state.active.error);
  if (!token) notes.push('Open this page from the address the dashboard prints (it carries a token) to preview and save.');
  $('notice').textContent = notes.join(' · ');
  renderTabs(); renderView(); changedFlag();
}
async function reload(keepView) {
  try { load(await call('/api/presets'), keepView); $('error').textContent = ''; }
  catch (error) { $('error').textContent = 'Loading failed: ' + error.message; }
}
$('revert').addEventListener('click', () => { draft = fromState(saved); picks = saved.picks; renderView(); changedFlag(); $('message').textContent = ''; });
$('save').addEventListener('click', async () => {
  const list = edits(); if (!list.length) return;
  $('save').disabled = true; $('message').textContent = 'Saving…';
  try {
    const result = await call('/api/presets/save', {edits: list, version: saved.version});
    load(result.state, true);
    $('message').textContent = 'Saved' + (result.backup ? '; the file before: ' + result.backup : '');
  } catch (error) {
    $('message').textContent = '';
    $('error').textContent = (error.status === 409 ? 'Not saved: ' + error.message : 'Saving failed: ' + error.message);
    changedFlag();
  }
});
reload(false);
</script>
</body></html>
'''


def presets_html():
    return PRESETS_PAGE.replace("__STYLE__", STYLE)


LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 256 * 1024


def make_server(port=8765, store=None, editor=None, token=None):
    """The dashboard on 127.0.0.1. Reads need a loopback Host; a write (the presets page's preview and save) needs, as
    well, the token made at this start (the page has it from the address's fragment) and an Origin of this server,
    so another page open in the browser cannot change the router's config."""
    state_store = store if store is not None else StateStore()
    presets = editor if editor is not None else preset_edit.Editor()
    secret = token if token is not None else secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def loopback(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower().rstrip(".")
            if host not in LOOPBACK:
                # A page on another site that resolves its name to 127.0.0.1 (DNS rebinding) must not read the state.
                self.respond(403, "text/plain; charset=utf-8", b"Forbidden")
                return False
            return True

        def do_GET(self):
            path = self.path.partition("?")[0]
            if not self.loopback():
                return
            if path == "/":
                self.respond(200, "text/html; charset=utf-8", page_html().encode("utf-8"))
            elif path == "/presets":
                self.respond(200, "text/html; charset=utf-8", presets_html().encode("utf-8"))
            elif path == "/api/state":
                try:
                    body = json.dumps(finite(state_store.get()), ensure_ascii=True, allow_nan=False).encode("utf-8")
                except Exception:
                    self.respond(503, "application/json", b'{"error":"State gathering failed; retry in 60 seconds"}')
                    return
                self.respond(200, "application/json; charset=utf-8", body)
            elif path == "/api/presets":
                try:
                    self.json(200, presets.state())
                except (OSError, ValueError) as e:
                    self.json(503, {"error": f"Reading the roles failed: {e}"})
            else:
                self.respond(404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self):
            path = self.path.partition("?")[0]
            if not self.loopback():
                return
            origin = urllib.parse.urlsplit(self.headers.get("Origin") or "")
            try:
                origin_port = origin.port
            except ValueError:
                origin_port = None
            if origin.scheme != "http" or (origin.hostname or "") not in LOOPBACK or origin_port != self.server.server_port:
                self.json(403, {"error": "Forbidden: a write must come from this dashboard's own page"})
                return
            if not hmac.compare_digest((self.headers.get("X-Switchboard-Token") or "").encode(), secret.encode()):
                self.json(403, {"error": "Forbidden: the token is missing or old; open the address the dashboard printed"})
                return
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                self.json(415, {"error": "JSON only"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self.json(411, {"error": "Content-Length needed"})
                return
            if not 0 < length <= MAX_BODY:
                self.json(413, {"error": "Body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                edits = body.get("edits") if isinstance(body, dict) else None
            except (UnicodeDecodeError, ValueError):
                self.json(400, {"error": "Not JSON"})
                return
            try:
                if path == "/api/presets/preview":
                    self.json(200, presets.preview(edits))
                elif path == "/api/presets/save":
                    version = body.get("version")
                    result = presets.save(edits, version=version if isinstance(version, str) else None)
                    self.json(200, dict(result, state=presets.state()))
                else:
                    self.json(404, {"error": "Not found"})
            except preset_edit.Conflict as e:
                self.json(409, {"error": str(e)})
            except preset_edit.EditError as e:
                self.json(400, {"error": str(e)})
            except (OSError, ValueError) as e:
                self.json(500, {"error": f"Nothing written: {e}"})

        def json(self, status, value):
            body = json.dumps(finite(value), ensure_ascii=True, allow_nan=False).encode("utf-8")
            self.respond(status, "application/json; charset=utf-8", body)

        def respond(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            # Journal/provider data should not leak into terminal request logs.
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = secret
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--serve", action="store_true", help="serve shared, file-only stats (private network or tunnel only)")
    parser.add_argument("--data", default=os.environ.get("SWITCHBOARD_DASH_DATA"), help="shared stats directory")
    parser.add_argument("--host", default="127.0.0.1", help="shared server bind address")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--once", metavar="FILE", help="write an inlined snapshot and exit")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if args.serve:
        from stats_server import make_server as make_stats_server
        if not args.data or args.once:
            parser.error("--serve requires --data (or SWITCHBOARD_DASH_DATA), and cannot use --once")
        try:
            server = make_stats_server(args.data, os.environ.get("SWITCHBOARD_DASH_TOKEN"),
                                       os.environ.get("SWITCHBOARD_INGEST_TOKEN") or None, args.host, args.port)
        except ValueError as error:
            parser.error(str(error))
        with server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    if args.once:
        Path(args.once).write_text(page_html(gather_state()), encoding="utf-8")
        return 0
    with make_server(args.port, editor=preset_edit.Editor(context=route.resolve_context())) as server:
        # The token is in the fragment, which the browser keeps to itself: it never reaches a request log or a Referer.
        address = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
        print(f"Local limits: {address} (Ctrl+C to stop; presets at /presets)", flush=True)
        if not args.no_browser:
            webbrowser.open(address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
