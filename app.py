#!/usr/bin/env python3
"""
ORBITCAST — Web Dashboard + API
Run with: python app.py
"""

import os
import json
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)

SEEN_FILE = Path("seen_events.json")
EVENTS_FILE = Path("events_cache.json")
LAST_RUN_FILE = Path("last_run.json")

# ─────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────

def load_events_cache():
    if EVENTS_FILE.exists():
        return json.loads(EVENTS_FILE.read_text())
    return {"events": [], "summary": [], "last_run": None}

def load_last_run():
    if LAST_RUN_FILE.exists():
        return json.loads(LAST_RUN_FILE.read_text())
    return {}

def save_events_cache(events, summary):
    data = {
        "events": events,
        "summary": summary,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }
    EVENTS_FILE.write_text(json.dumps(data, indent=2))
    LAST_RUN_FILE.write_text(json.dumps({"last_run": data["last_run"]}))

# ─────────────────────────────────────────────
# BACKGROUND SCRAPE RUNNER
# ─────────────────────────────────────────────

_scrape_lock = threading.Lock()
_scraping = False

def run_scrape_background():
    global _scraping
    with _scrape_lock:
        if _scraping:
            return
        _scraping = True
    try:
        from scraper import SOURCES

        all_events = []
        summary_data = []
        results_lock = threading.Lock()

        def scrape_one(src):
            try:
                events = src["fn"]()
                enriched = [{**ev, "emoji": src["emoji"], "category": src["category"]} for ev in events]
                summary = {"source": src["name"], "emoji": src["emoji"], "category": src["category"], "count": len(events)}
                return enriched, summary
            except Exception as e:
                log.error(f"Scraper {src['name']} failed: {e}")
                return [], {"source": src["name"], "emoji": src["emoji"], "category": src["category"], "count": 0}

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(scrape_one, src): src for src in SOURCES}
            for future in as_completed(futures):
                events, summary = future.result()
                with results_lock:
                    all_events.extend(events)
                    summary_data.append(summary)

        summary_data.sort(key=lambda x: x["source"])
        save_events_cache(all_events, summary_data)
        log.info(f"Background scrape done: {len(all_events)} events")
    except Exception as e:
        log.error(f"Background scrape failed: {e}")
    finally:
        _scraping = False

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/api/events")
def api_events():
    cache = load_events_cache()
    category = request.args.get("category", "").strip()
    source = request.args.get("source", "").strip()
    events = cache.get("events", [])
    if category:
        events = [e for e in events if e.get("category", "").lower() == category.lower()]
    if source:
        events = [e for e in events if e.get("source", "").lower() == source.lower()]
    return jsonify({
        "events": events,
        "total": len(events),
        "last_run": cache.get("last_run"),
        "scraping": _scraping,
    })

@app.route("/api/summary")
def api_summary():
    cache = load_events_cache()
    return jsonify({
        "summary": cache.get("summary", []),
        "last_run": cache.get("last_run"),
        "scraping": _scraping,
    })

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "Scraping in background..."})

@app.route("/api/status")
def api_status():
    return jsonify({"scraping": _scraping, "last_run": load_last_run().get("last_run")})

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ─────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ORBITCAST</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #111118;
    --surface2: #1a1a24;
    --border: #2a2a3a;
    --accent: #00ff9d;
    --accent2: #7b5ea7;
    --text: #e8e8f0;
    --muted: #6b6b88;
    --danger: #ff4757;
    --warn: #ffa502;
    --intel: #00d2ff;
    --defence: #ff6b35;
    --cyber: #ff4757;
    --tech: #7b5ea7;
    --edu: #00ff9d;
    --builder: #ffd700;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Scanline overlay */
  body::before {
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,255,157,0.015) 2px,
      rgba(0,255,157,0.015) 4px
    );
    pointer-events: none;
    z-index: 1000;
  }

  header {
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface);
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .logo {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.2rem;
    font-weight: 600;
    color: var(--accent);
    letter-spacing: 0.1em;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .logo-dot {
    width: 8px; height: 8px;
    background: var(--accent);
    border-radius: 50%;
    animation: pulse 2s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .last-run {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
  }
  .elapsed {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem;
    color: #3a3a55;
    margin-left: 6px;
  }

  .btn-refresh {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    font-weight: 500;
    background: transparent;
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 7px 16px;
    cursor: pointer;
    letter-spacing: 0.08em;
    transition: all 0.2s;
  }

  .btn-refresh:hover {
    background: var(--accent);
    color: var(--bg);
  }

  .btn-refresh:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .main {
    max-width: 1400px;
    margin: 0 auto;
    padding: 32px;
  }

  /* Stats bar */
  .stats-bar {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    margin-bottom: 32px;
  }

  .stat-cell {
    background: var(--surface);
    padding: 20px 24px;
    text-align: center;
  }

  .stat-num {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 2rem;
    font-weight: 600;
    color: var(--accent);
    line-height: 1;
  }

  .stat-label {
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-top: 6px;
  }

  /* Filter bar */
  .filter-bar {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 24px;
    align-items: center;
  }

  .filter-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-right: 4px;
  }

  .filter-btn {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 5px 12px;
    cursor: pointer;
    transition: all 0.15s;
    letter-spacing: 0.05em;
  }

  .filter-btn:hover, .filter-btn.active {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(0,255,157,0.05);
  }

  .search-input {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 12px;
    width: 220px;
    outline: none;
    margin-left: auto;
  }

  .search-input:focus {
    border-color: var(--accent);
  }

  .search-input::placeholder {
    color: var(--muted);
  }

  /* Events grid */
  .events-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 1px;
    background: var(--border);
  }

  .event-card {
    background: var(--surface);
    padding: 20px;
    transition: background 0.15s;
    display: flex;
    flex-direction: column;
    gap: 10px;
    animation: fadeIn 0.3s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .event-card:hover {
    background: var(--surface2);
  }

  .card-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
  }

  .card-source {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 3px 8px;
    border-radius: 2px;
    white-space: nowrap;
  }

  .cat-intel    { background: rgba(0,210,255,0.1);  color: var(--intel); }
  .cat-defence  { background: rgba(255,107,53,0.1); color: var(--defence); }
  .cat-cyber    { background: rgba(255,71,87,0.1);  color: var(--cyber); }
  .cat-tech     { background: rgba(123,94,167,0.1); color: var(--tech); }
  .cat-edu      { background: rgba(0,255,157,0.1);  color: var(--edu); }
  .cat-builder  { background: rgba(255,215,0,0.1);  color: var(--builder); }

  .card-emoji {
    font-size: 1.1rem;
    flex-shrink: 0;
  }

  .event-title {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text);
    line-height: 1.4;
    flex: 1;
  }

  .event-title a {
    color: inherit;
    text-decoration: none;
  }

  .event-title a:hover {
    color: var(--accent);
  }

  .card-meta {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .meta-date {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
  }

  .meta-link {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem;
    color: var(--accent);
    text-decoration: none;
    opacity: 0.7;
    transition: opacity 0.15s;
  }

  .meta-link:hover { opacity: 1; }

  /* Source panel */
  .layout {
    display: grid;
    grid-template-columns: 220px 1fr;
    gap: 24px;
  }

  .source-panel {
    position: sticky;
    top: 90px;
    height: fit-content;
  }

  .panel-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--muted);
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .source-list {
    background: var(--surface);
    border: 1px solid var(--border);
  }

  .source-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.15s;
    font-size: 0.78rem;
  }

  .source-item:last-child { border-bottom: none; }
  .source-item:hover { background: var(--surface2); }
  .source-item.active { background: rgba(0,255,157,0.05); border-left: 2px solid var(--accent); }

  .source-name {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text);
  }

  .source-count {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem;
    color: var(--muted);
    background: var(--surface2);
    padding: 2px 7px;
    border-radius: 10px;
  }

  /* Empty state */
  .empty-state {
    text-align: center;
    padding: 80px 20px;
    grid-column: 1 / -1;
    font-family: 'IBM Plex Mono', monospace;
    color: var(--muted);
  }

  .empty-state h3 {
    font-size: 1rem;
    margin-bottom: 8px;
    color: var(--text);
  }

  /* Loading */
  .loading-bar {
    height: 2px;
    background: var(--border);
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 200;
    overflow: hidden;
    display: none;
  }

  .loading-bar.active { display: block; }

  .loading-bar::after {
    content: '';
    position: absolute;
    top: 0; left: -50%;
    width: 50%;
    height: 100%;
    background: var(--accent);
    animation: loading 1.2s infinite ease-in-out;
  }

  @keyframes loading {
    0% { left: -50%; }
    100% { left: 150%; }
  }

  .count-badge {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
    margin-bottom: 12px;
  }


  .cal-section { max-width: 1400px; margin: 48px auto 0; padding: 0 32px 48px; }
  .cal-section-title { font-family: 'IBM Plex Mono', monospace; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); margin-bottom: 20px; display: flex; align-items: center; gap: 12px; }
  .cal-section-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .cal-nav { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .cal-month-label { font-family: 'IBM Plex Mono', monospace; font-size: 1rem; font-weight: 600; color: var(--text); letter-spacing: 0.05em; }
  .cal-nav-btn { font-family: 'IBM Plex Mono', monospace; font-size: 0.75rem; background: var(--surface); border: 1px solid var(--border); color: var(--muted); padding: 6px 14px; cursor: pointer; transition: all 0.15s; }
  .cal-nav-btn:hover { border-color: var(--accent); color: var(--accent); }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 1px; background: var(--border); border: 1px solid var(--border); }
  .cal-day-header { background: var(--surface); padding: 8px; text-align: center; font-family: 'IBM Plex Mono', monospace; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); }
  .cal-day { background: var(--surface); min-height: 100px; padding: 8px; transition: background 0.15s; }
  .cal-day:hover { background: var(--surface2); }
  .cal-day.today { background: rgba(0,255,157,0.04); }
  .cal-day.today .cal-day-num { color: var(--accent); }
  .cal-day.other-month { opacity: 0.3; }
  .cal-day-num { font-family: 'IBM Plex Mono', monospace; font-size: 0.7rem; color: var(--muted); margin-bottom: 6px; display: block; }
  .cal-event-pill { display: block; font-size: 0.62rem; padding: 2px 6px; margin-bottom: 3px; border-radius: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; text-decoration: none; transition: opacity 0.15s; line-height: 1.4; }
  .cal-event-pill:hover { opacity: 0.8; }
  .pill-intel { background: rgba(0,210,255,0.15); color: #00d2ff; }
  .pill-defence { background: rgba(255,107,53,0.15); color: #ff6b35; }
  .pill-cyber { background: rgba(255,71,87,0.15); color: #ff4757; }
  .pill-tech { background: rgba(123,94,167,0.15); color: #b39ddb; }
  .pill-edu { background: rgba(0,255,157,0.15); color: #00ff9d; }
  .pill-builder { background: rgba(255,215,0,0.15); color: #ffd700; }
  .cal-more { font-family: 'IBM Plex Mono', monospace; font-size: 0.6rem; color: var(--muted); cursor: pointer; padding: 1px 4px; margin-top: 2px; display: inline-block; }
  .cal-more:hover { color: var(--accent); }
  .cal-detail { background: var(--surface); border: 1px solid var(--border); margin-top: 1px; display: none; padding: 20px 24px; }
  .cal-detail.open { display: block; }
  .cal-detail-title { font-family: 'IBM Plex Mono', monospace; font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 16px; }
  .cal-detail-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1px; background: var(--border); }
  .cal-detail-item { background: var(--surface2); padding: 14px 16px; display: flex; flex-direction: column; gap: 6px; }
  .cal-detail-time { font-family: 'IBM Plex Mono', monospace; font-size: 0.65rem; color: var(--accent); }
  .cal-detail-name { font-size: 0.85rem; font-weight: 500; color: var(--text); line-height: 1.3; }
  .cal-detail-meta { display: flex; align-items: center; gap: 10px; }
  .cal-detail-source { font-family: 'IBM Plex Mono', monospace; font-size: 0.62rem; color: var(--muted); }
  .cal-detail-link { font-family: 'IBM Plex Mono', monospace; font-size: 0.62rem; color: var(--accent); text-decoration: none; opacity: 0.8; margin-left: auto; }
  .cal-detail-link:hover { opacity: 1; }
  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    .source-panel { position: static; }
  }
</style>
</head>
<body>

<div class="loading-bar" id="loadingBar"></div>

<header>
  <div class="logo">
    <div class="logo-dot"></div>
    ORBITCAST
  </div>
  <div class="header-right">
    <span class="last-run" id="lastRun">—</span>
    <button class="btn-refresh" id="refreshBtn" onclick="triggerRefresh()">⟳ REFRESH</button>
  </div>
</header>

<div class="main">
  <!-- Stats -->
  <div class="stats-bar" id="statsBar">
    <div class="stat-cell"><div class="stat-num" id="statTotal">—</div><div class="stat-label">Total Events</div></div>
    <div class="stat-cell"><div class="stat-num" id="statSources">—</div><div class="stat-label">Sources</div></div>
    <div class="stat-cell"><div class="stat-num" id="statIntel">—</div><div class="stat-label">Intel & Security</div></div>
    <div class="stat-cell"><div class="stat-num" id="statBuilder">—</div><div class="stat-label">Builder & Tech</div></div>
    <div class="stat-cell"><div class="stat-num" id="statDefence">—</div><div class="stat-label">Defence</div></div>
    <div class="stat-cell"><div class="stat-num" id="statEdu">—</div><div class="stat-label">Education</div></div>
  </div>

  <!-- Filter bar -->
  <div class="filter-bar">
    <span class="filter-label">Filter:</span>
    <button class="filter-btn active" onclick="setFilter('all', this)">ALL</button>
    <button class="filter-btn" onclick="setFilter('Intelligence & Security', this)">🔍 INTEL</button>
    <button class="filter-btn" onclick="setFilter('Defence & Geopolitics', this)">🎖️ DEFENCE</button>
    <button class="filter-btn" onclick="setFilter('Cyber & Infosec', this)">🔐 CYBER</button>
    <button class="filter-btn" onclick="setFilter('Tech & AI', this)">🤖 TECH</button>
    <button class="filter-btn" onclick="setFilter('Education & Research', this)">🎓 EDU</button>
    <button class="filter-btn" onclick="setFilter('Builder & Tech Community', this)">⚡ BUILDER</button>
    <button class="filter-btn" onclick="document.querySelector('.cal-section').scrollIntoView({behavior:'smooth'})" style="margin-left:auto;border-color:#3a3a55">📅 CALENDAR</button>
    <input class="search-input" id="searchInput" placeholder="Search events..." oninput="filterEvents()" style="margin-left:0">
  </div>

  <div class="layout">
    <!-- Source sidebar -->
    <div class="source-panel">
      <div class="source-list">
        <div class="panel-title">Sources</div>
        <div id="sourceList"></div>
      </div>
    </div>

    <!-- Events -->
    <div>
      <div class="count-badge" id="countBadge"></div>
      <div class="events-grid" id="eventsGrid"></div>
    </div>
  </div>
</div>

<script>
let allEvents = [];
let activeCategory = 'all';
let activeSource = null;

const CAT_CLASS = {
  'Intelligence & Security': 'cat-intel',
  'Defence & Geopolitics': 'cat-defence',
  'Cyber & Infosec': 'cat-cyber',
  'Tech & AI': 'cat-tech',
  'Education & Research': 'cat-edu',
  'Builder & Tech Community': 'cat-builder',
};

async function fetchEvents() {
  showLoading(true);
  try {
    const r = await fetch('/api/events');
    const data = await r.json();
    allEvents = data.events || [];
    updateStats(data);
    updateLastRun(data.last_run);
    renderEvents();
    renderSources();
    renderCalendar();
  } catch(e) {
    console.error(e);
  }
  showLoading(false);
}

async function fetchSummary() {
  const r = await fetch('/api/summary');
  const data = await r.json();
  renderSourceList(data.summary || []);
}

function updateStats(data) {
  document.getElementById('statTotal').textContent = data.total || 0;
  const events = data.events || [];
  const sources = [...new Set(events.map(e => e.source))];
  document.getElementById('statSources').textContent = sources.length;
  document.getElementById('statIntel').textContent = events.filter(e => e.category === 'Intelligence & Security').length;
  document.getElementById('statBuilder').textContent = events.filter(e => e.category === 'Builder & Tech Community').length;
  document.getElementById('statDefence').textContent = events.filter(e => e.category === 'Defence & Geopolitics').length;
  document.getElementById('statEdu').textContent = events.filter(e => e.category === 'Education & Research').length;
}

let _lastRunTs = null;
function updateLastRun(ts) {
  if (!ts) return;
  _lastRunTs = new Date(ts);
  renderLastRun();
}
function renderLastRun() {
  if (!_lastRunTs) return;
  const d = _lastRunTs;
  const now = new Date();
  const diffMs = now - d;
  const diffMins = Math.floor(diffMs / 60000);
  let elapsed;
  if (diffMins < 1) elapsed = 'just now';
  else if (diffMins < 60) elapsed = diffMins + 'm ago';
  else elapsed = Math.floor(diffMins/60) + 'h ago';
  document.getElementById('lastRun').innerHTML = 'Last run: ' + d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'}) + ' ' + d.toLocaleDateString('en-GB') + '<span class="elapsed">(' + elapsed + ')</span>';
}
setInterval(renderLastRun, 30000);

function getFiltered() {
  const q = document.getElementById('searchInput').value.toLowerCase();
  return allEvents.filter(ev => {
    const catMatch = activeCategory === 'all' || ev.category === activeCategory;
    const srcMatch = !activeSource || ev.source === activeSource;
    const searchMatch = !q || ev.title.toLowerCase().includes(q) || ev.source.toLowerCase().includes(q);
    return catMatch && srcMatch && searchMatch;
  });
}

function renderEvents() {
  const grid = document.getElementById('eventsGrid');
  const filtered = getFiltered();
  document.getElementById('countBadge').textContent = filtered.length + ' events';

  if (filtered.length === 0) {
    grid.innerHTML = '<div class="empty-state"><h3>No events found</h3><p>Try adjusting filters or run a refresh</p></div>';
    return;
  }

  grid.innerHTML = filtered.map(ev => {
    const catClass = CAT_CLASS[ev.category] || 'cat-tech';
    const dateStr = ev.date ? '<span class="meta-date">' + ev.date + '</span>' : '';
    const truncTitle = ev.title.length > 80 ? ev.title.substring(0, 80) + '…' : ev.title;
    return `
      <div class="event-card">
        <div class="card-top">
          <span class="card-emoji">${ev.emoji || '📅'}</span>
          <div class="event-title"><a href="${ev.url}" target="_blank" rel="noopener">${truncTitle}</a></div>
          <span class="card-source ${catClass}">${ev.source}</span>
        </div>
        <div class="card-meta">
          ${dateStr}
          <a class="meta-link" href="${ev.url}" target="_blank" rel="noopener">→ VIEW</a>
        </div>
      </div>
    `;
  }).join('');
}

function renderSources() {
  const counts = {};
  allEvents.forEach(ev => {
    counts[ev.source] = (counts[ev.source] || 0) + 1;
  });
  const sorted = Object.entries(counts).sort((a,b) => b[1]-a[1]);

  const allItem = `<div class="source-item ${!activeSource ? 'active' : ''}" onclick="setSource(null, this)">
    <span class="source-name">All sources</span>
    <span class="source-count">${allEvents.length}</span>
  </div>`;

  const items = sorted.map(([src, cnt]) => {
    const ev = allEvents.find(e => e.source === src);
    const emoji = ev ? ev.emoji : '📅';
    return `<div class="source-item ${activeSource === src ? 'active' : ''}" onclick="setSource('${src}', this)">
      <span class="source-name">${emoji} ${src}</span>
      <span class="source-count">${cnt}</span>
    </div>`;
  }).join('');

  document.getElementById('sourceList').innerHTML = allItem + items;
}

function setFilter(cat, btn) {
  activeCategory = cat;
  activeSource = null;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderEvents();
  renderSources();
}

function setSource(src, el) {
  activeSource = src;
  document.querySelectorAll('.source-item').forEach(i => i.classList.remove('active'));
  el.classList.add('active');
  renderEvents();
}

function filterEvents() {
  renderEvents();
}

async function triggerRefresh() {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  btn.textContent = '⟳ SCANNING...';
  showLoading(true);

  await fetch('/api/refresh', {method:'POST'});

  // Poll until done
  const poll = setInterval(async () => {
    const r = await fetch('/api/status');
    const s = await r.json();
    if (!s.scraping) {
      clearInterval(poll);
      await fetchEvents();
      btn.disabled = false;
      btn.textContent = '⟳ REFRESH';
      showLoading(false);
    }
  }, 2000);
}

function showLoading(on) {
  document.getElementById('loadingBar').classList.toggle('active', on);
}


// ── CALENDAR ─────────────────────────────────────

const CAT_PILL = {
  'Intelligence & Security': 'pill-intel',
  'Defence & Geopolitics':   'pill-defence',
  'Cyber & Infosec':         'pill-cyber',
  'Tech & AI':               'pill-tech',
  'Education & Research':    'pill-edu',
  'Builder & Tech Community':'pill-builder',
};

let calYear, calMonth, selectedDay = null;

function parseEventDate(ev) {
  const raw = ev.date;
  if (!raw) return null;
  // ISO format: 2026-05-19
  const iso = raw.match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
  if (iso) return new Date(iso[1], parseInt(iso[2])-1, parseInt(iso[3]));
  // Natural: "Wed 20 May 2026, ..." or "Sat 6 June 2026 – ..."
  const nat = raw.match(/(\\d{1,2})\\s+(\\w+)\\s+(\\d{4})/);
  if (nat) {
    const months = {January:0,February:1,March:2,April:3,May:4,June:5,July:6,August:7,September:8,October:9,November:10,December:11};
    const m = months[nat[2]];
    if (m !== undefined) return new Date(parseInt(nat[3]), m, parseInt(nat[1]));
  }
  return null;
}

function parseEventTime(ev) {
  const raw = ev.date || '';
  // e.g. "17.30–18.30BST" or "17.30–18.30"
  const t = raw.match(/(\\d{1,2})[\\.]( \\d{2})(?:[–-](\\d{1,2})[\\.]( \\d{2}))?/);
  if (t) {
    const h = t[1].padStart(2,'0');
    const m = (t[2]||'00').padStart(2,'0');
    return `${h}:${m}`;
  }
  return null;
}

function buildCalendarIndex() {
  const index = {};
  allEvents.forEach(ev => {
    const d = parseEventDate(ev);
    if (!d) return;
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    if (!index[key]) index[key] = [];
    index[key].push(ev);
  });
  return index;
}

function renderCalendar() {
  const now = new Date();
  if (calYear === undefined) { calYear = now.getFullYear(); calMonth = now.getMonth(); }

  const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  document.getElementById('calMonthLabel').textContent = `${monthNames[calMonth]} ${calYear}`;

  const index = buildCalendarIndex();
  const firstDay = new Date(calYear, calMonth, 1).getDay(); // 0=Sun
  const daysInMonth = new Date(calYear, calMonth+1, 0).getDate();
  const daysInPrev = new Date(calYear, calMonth, 0).getDate();
  const startOffset = (firstDay + 6) % 7; // Mon-start

  const dayHeaders = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  let html = dayHeaders.map(d => `<div class="cal-day-header">${d}</div>`).join('');

  const totalCells = Math.ceil((startOffset + daysInMonth) / 7) * 7;

  for (let i = 0; i < totalCells; i++) {
    let dayNum, monthOffset = 0;
    if (i < startOffset) {
      dayNum = daysInPrev - startOffset + i + 1;
      monthOffset = -1;
    } else if (i >= startOffset + daysInMonth) {
      dayNum = i - startOffset - daysInMonth + 1;
      monthOffset = 1;
    } else {
      dayNum = i - startOffset + 1;
    }

    const cellDate = new Date(calYear, calMonth + monthOffset, dayNum);
    const isToday = cellDate.toDateString() === now.toDateString();
    const isOther = monthOffset !== 0;
    const key = `${cellDate.getFullYear()}-${cellDate.getMonth()}-${cellDate.getDate()}`;
    const dayEvents = index[key] || [];

    const classes = ['cal-day', isToday ? 'today' : '', isOther ? 'other-month' : ''].filter(Boolean).join(' ');
    const dateStr = cellDate.toISOString().split('T')[0];

    let pillsHtml = '';
    const maxShow = 3;
    dayEvents.slice(0, maxShow).forEach(ev => {
      const pillClass = CAT_PILL[ev.category] || 'pill-tech';
      const time = parseEventTime(ev);
      const timePrefix = time ? `${time} ` : '';
      const short = (timePrefix + ev.title).substring(0, 28);
      pillsHtml += `<a class="cal-event-pill ${pillClass}" href="${ev.url}" target="_blank" rel="noopener" title="${ev.title}">${short}</a>`;
    });
    if (dayEvents.length > maxShow) {
      pillsHtml += `<span class="cal-more" onclick="showDayDetail('${dateStr}', event)">+${dayEvents.length - maxShow} more</span>`;
    }

    const clickHandler = dayEvents.length > 0 ? `onclick="showDayDetail('${cellDate.getFullYear()}-${String(cellDate.getMonth()+1).padStart(2,'0')}-${String(cellDate.getDate()).padStart(2,'0')}', event)"` : '';
    html += `<div class="${classes}" ${clickHandler}>
      <span class="cal-day-num">${dayNum}</span>
      ${pillsHtml}
    </div>`;
  }

  document.getElementById('calGrid').innerHTML = html;
}

function showDayDetail(dateStr, e) {
  if (e) e.stopPropagation();
  const index = buildCalendarIndex();
  const parts = dateStr.split('-');
  const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
  const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  const dayEvents = index[key] || [];
  if (!dayEvents.length) return;

  const panel = document.getElementById('calDetail');

  if (selectedDay === dateStr && panel.classList.contains('open')) {
    panel.classList.remove('open');
    selectedDay = null;
    return;
  }

  selectedDay = dateStr;
  const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const label = `${d.getDate()} ${monthNames[d.getMonth()]} ${d.getFullYear()} — ${dayEvents.length} event${dayEvents.length>1?'s':''}`;

  // Sort by time
  const sorted = [...dayEvents].sort((a,b) => {
    const ta = parseEventTime(a) || '23:59';
    const tb = parseEventTime(b) || '23:59';
    return ta.localeCompare(tb);
  });

  const items = sorted.map(ev => {
    const time = parseEventTime(ev);
    const pillClass = CAT_PILL[ev.category] || 'pill-tech';
    return `<div class="cal-detail-item">
      ${time ? `<span class="cal-detail-time">${time}</span>` : ''}
      <div class="cal-detail-name">${ev.title}</div>
      <div class="cal-detail-meta">
        <span class="cal-event-pill ${pillClass}" style="margin:0;cursor:default">${ev.emoji || ''} ${ev.source}</span>
        <a class="cal-detail-link" href="${ev.url}" target="_blank" rel="noopener">→ BOOK / VIEW</a>
      </div>
    </div>`;
  }).join('');

  panel.innerHTML = `<div class="cal-detail-title">${label}</div><div class="cal-detail-list">${items}</div>`;
  panel.classList.add('open');
  panel.scrollIntoView({behavior:'smooth', block:'nearest'});
}

function calPrev() {
  calMonth--;
  if (calMonth < 0) { calMonth = 11; calYear--; }
  selectedDay = null;
  document.getElementById('calDetail').classList.remove('open');
  renderCalendar();
}

function calNext() {
  calMonth++;
  if (calMonth > 11) { calMonth = 0; calYear++; }
  selectedDay = null;
  document.getElementById('calDetail').classList.remove('open');
  renderCalendar();
}

// Close detail on outside click
document.addEventListener('click', (e) => {
  const detail = document.getElementById('calDetail');
  if (detail && !detail.contains(e.target) && !e.target.closest('.cal-day')) {
    detail.classList.remove('open');
    selectedDay = null;
  }
});

// Boot
fetchEvents();
// Auto-refresh every 30 mins
setInterval(fetchEvents, 30 * 60 * 1000);
</script>

<!-- ── CALENDAR SECTION ──────────────────────── -->
<div class="cal-section">
  <div class="cal-section-title">📅 Event Calendar</div>
  <div class="cal-nav">
    <button class="cal-nav-btn" onclick="calPrev()">← PREV</button>
    <span class="cal-month-label" id="calMonthLabel"></span>
    <button class="cal-nav-btn" onclick="calNext()">NEXT →</button>
  </div>
  <div class="cal-grid" id="calGrid"></div>
  <div class="cal-detail" id="calDetail"></div>
</div>
</body>
</html>
"""

# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Run initial scrape on boot in background
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()

    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
