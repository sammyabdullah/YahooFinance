#!/usr/bin/env python3
"""
Yahoo Finance Web Dashboard
Serves a live webpage that refreshes data from Yahoo Finance daily.
Run with: python web.py
Then open: http://localhost:5000
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from apscheduler.schedulers.background import BackgroundScheduler

# Reuse data logic from app.py
from app import fetch_ticker_data, load_tickers, summary_stats, NUMERIC_KEYS

app = Flask(__name__)

CACHE_FILE = Path("web_cache.json")
_lock = threading.Lock()
_cache: dict = {"data": [], "last_updated": None}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def save_cache() -> None:
    with _lock:
        CACHE_FILE.write_text(json.dumps(_cache, default=str))


def load_cache() -> None:
    global _cache
    if CACHE_FILE.exists():
        try:
            _cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            pass


def fmt_billions(value) -> str:
    if value is None:
        return None
    try:
        return round(float(value) / 1e9, 1)
    except (TypeError, ValueError):
        return None


def fmt_pct(value) -> str:
    if value is None:
        return None
    try:
        return round(float(value) * 100, 1)
    except (TypeError, ValueError):
        return None


def build_row(d: dict) -> dict:
    """Convert a raw data dict into display-ready values for the template."""
    if d.get("error"):
        return {"ticker": d["ticker"], "error": d["error"]}
    rev_mult = d.get("rev_multiple")
    return {
        "ticker": d["ticker"],
        "name": d["name"],
        "market_cap": fmt_billions(d.get("market_cap")),
        "revenue": fmt_billions(d.get("revenue")),
        "rev_growth": fmt_pct(d.get("rev_growth")),
        "ebitda": fmt_billions(d.get("ebitda")),
        "ocf": fmt_billions(d.get("ocf")),
        "debt": fmt_billions(d.get("debt")),
        "cash": fmt_billions(d.get("cash")),
        "ev": fmt_billions(d.get("ev")),
        "rev_multiple": round(rev_mult, 1) if rev_mult is not None else None,
        "error": None,
    }


def build_stat_row(label: str, agg: dict, stat: str) -> dict:
    rm = agg["rev_multiple"][stat]
    g = agg["rev_growth"][stat]
    return {
        "name": label,
        "market_cap": fmt_billions(agg["market_cap"][stat]),
        "revenue": fmt_billions(agg["revenue"][stat]),
        "rev_growth": fmt_pct(g),
        "ebitda": fmt_billions(agg["ebitda"][stat]),
        "ocf": fmt_billions(agg["ocf"][stat]),
        "debt": fmt_billions(agg["debt"][stat]),
        "cash": fmt_billions(agg["cash"][stat]),
        "ev": fmt_billions(agg["ev"][stat]),
        "rev_multiple": round(rm, 1) if rm is not None else None,
    }


# ── Refresh logic ─────────────────────────────────────────────────────────────

def refresh_data() -> None:
    """Fetch fresh data for all tickers and update the cache."""
    global _cache
    tickers = load_tickers()
    if not tickers:
        return
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing {len(tickers)} tickers…")
    data = [fetch_ticker_data(t) for t in tickers]
    with _lock:
        _cache = {"data": data, "last_updated": datetime.now().isoformat()}
    save_cache()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Refresh complete.")


# ── HTML template (embedded so no separate templates/ folder is needed) ───────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Stock Watchlist</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px 20px 48px;
    }
    header { display: flex; align-items: baseline; gap: 16px; margin-bottom: 8px; }
    header h1 { font-size: 1.5rem; font-weight: 700; color: #58a6ff; letter-spacing: -0.02em; }
    header span.subtitle { font-size: 0.85rem; color: #8b949e; }
    .meta-bar { display: flex; align-items: center; gap: 20px; margin-bottom: 20px; font-size: 0.82rem; color: #8b949e; }
    .meta-bar strong { color: #c9d1d9; }
    #refresh-btn {
      display: inline-flex; align-items: center; gap: 6px; padding: 5px 14px;
      border-radius: 6px; border: 1px solid #30363d; background: #21262d;
      color: #c9d1d9; font-size: 0.82rem; cursor: pointer; transition: background 0.15s, border-color 0.15s;
    }
    #refresh-btn:hover { background: #30363d; border-color: #58a6ff; }
    #refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    #refresh-btn svg { width: 14px; height: 14px; }
    #refresh-status { font-size: 0.78rem; color: #8b949e; }
    .table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #21262d; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; white-space: nowrap; }
    thead th {
      background: #161b22; color: #8b949e; font-weight: 600; font-size: 0.75rem;
      text-transform: uppercase; letter-spacing: 0.04em; padding: 10px 14px;
      text-align: right; border-bottom: 1px solid #21262d; cursor: pointer; user-select: none;
    }
    thead th:first-child, thead th:nth-child(2) { text-align: left; }
    thead th:hover { color: #58a6ff; }
    thead th.sorted-asc::after  { content: " \2191"; color: #58a6ff; }
    thead th.sorted-desc::after { content: " \2193"; color: #58a6ff; }
    tbody tr { border-bottom: 1px solid #21262d; transition: background 0.1s; }
    tbody tr:last-child { border-bottom: none; }
    tbody tr:hover { background: #161b22; }
    td { padding: 10px 14px; text-align: right; color: #c9d1d9; }
    td:first-child { text-align: left; font-weight: 500; color: #e6edf3; }
    td:nth-child(2) { text-align: left; font-family: monospace; font-size: 0.8rem; color: #58a6ff; font-weight: 600; }
    .val-null { color: #484f58; }
    .val-pos  { color: #3fb950; }
    .val-neg  { color: #f85149; }
    .val-debt { color: #f85149; }
    .val-cash { color: #3fb950; }
    .val-ev   { color: #58a6ff; }
    .val-mult { color: #d2a8ff; font-weight: 600; }
    .stats-header td {
      background: #0d1117; color: #58a6ff; font-size: 0.72rem; text-transform: uppercase;
      letter-spacing: 0.06em; padding: 14px 14px 6px; font-weight: 700; border-top: 2px solid #21262d;
    }
    .stat-row td { background: #13181f; color: #8b949e; font-style: italic; font-size: 0.82rem; padding: 7px 14px; }
    .stat-row td:first-child { color: #8b949e; font-weight: 400; }
    .error-row td { color: #f85149; font-size: 0.82rem; }
    .empty-state { text-align: center; padding: 60px 20px; color: #8b949e; }
    .empty-state p { margin-bottom: 8px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spin { animation: spin 1s linear infinite; display: inline-block; }
  </style>
</head>
<body>
<header>
  <h1>Stock Watchlist</h1>
  <span class="subtitle">powered by <a href="https://www.blossomstreetventures.com" target="_blank" style="color:#58a6ff;font-weight:700;text-decoration:none;">Blossom Street Ventures</a></span>
</header>
<div class="meta-bar">
  <span>Last updated: <strong>{{ last_updated }}</strong></span>
  <span>{{ ticker_count }} ticker{{ 's' if ticker_count != 1 else '' }}</span>
  <button id="refresh-btn" onclick="triggerRefresh()">
    <svg viewBox="0 0 16 16" fill="currentColor">
      <path d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/>
      <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
    </svg>
    Refresh Now
  </button>
  <span id="refresh-status"></span>
</div>
<div class="table-wrap">
  {% if rows %}
  <table id="watchlist">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Company</th>
        <th onclick="sortTable(1)">Ticker</th>
        <th onclick="sortTable(2)" title="Market Capitalization ($B)">Mkt Cap</th>
        <th onclick="sortTable(3)" title="Last Twelve Months Revenue ($B)">LTM Rev</th>
        <th onclick="sortTable(4)" title="Year-over-Year Revenue Growth">Rev Growth</th>
        <th onclick="sortTable(5)" title="LTM EBITDA ($B)">LTM EBITDA</th>
        <th onclick="sortTable(6)" title="LTM Operating Cash Flow ($B)">LTM Op CF</th>
        <th onclick="sortTable(7)" title="Total Debt ($B)">Debt</th>
        <th onclick="sortTable(8)" title="Total Cash ($B)">Cash</th>
        <th onclick="sortTable(9)" title="Enterprise Value = Mkt Cap + Debt - Cash ($B)">Ent. Value</th>
        <th onclick="sortTable(10)" title="Enterprise Value / LTM Revenue">Rev Mult.</th>
      </tr>
    </thead>
    <tbody id="ticker-rows">
      {% for row in rows %}
        {% if row.error %}
        <tr class="error-row">
          <td colspan="11">Error ({{ row.ticker }}): {{ row.error[:80] }}</td>
        </tr>
        {% else %}
        <tr
          data-market-cap="{{ row.market_cap if row.market_cap is not none else '' }}"
          data-revenue="{{ row.revenue if row.revenue is not none else '' }}"
          data-rev-growth="{{ row.rev_growth if row.rev_growth is not none else '' }}"
          data-ebitda="{{ row.ebitda if row.ebitda is not none else '' }}"
          data-ocf="{{ row.ocf if row.ocf is not none else '' }}"
          data-debt="{{ row.debt if row.debt is not none else '' }}"
          data-cash="{{ row.cash if row.cash is not none else '' }}"
          data-ev="{{ row.ev if row.ev is not none else '' }}"
          data-rev-multiple="{{ row.rev_multiple if row.rev_multiple is not none else '' }}"
        >
          <td>{{ row.name }}</td>
          <td>{{ row.ticker }}</td>
          <td>{% if row.market_cap is not none %}<span class="val-pos">${{ row.market_cap }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.revenue is not none %}<span class="val-pos">${{ row.revenue }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>
            {% if row.rev_growth is not none %}
              <span class="{{ 'val-pos' if row.rev_growth >= 0 else 'val-neg' }}">{{ '+' if row.rev_growth >= 0 else '' }}{{ row.rev_growth }}%</span>
            {% else %}<span class="val-null">—</span>{% endif %}
          </td>
          <td>{% if row.ebitda is not none %}<span class="{{ 'val-pos' if row.ebitda >= 0 else 'val-neg' }}">${{ row.ebitda }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.ocf is not none %}<span class="{{ 'val-pos' if row.ocf >= 0 else 'val-neg' }}">${{ row.ocf }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.debt is not none %}<span class="val-debt">${{ row.debt }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.cash is not none %}<span class="val-cash">${{ row.cash }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.ev is not none %}<span class="val-ev">${{ row.ev }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.rev_multiple is not none %}<span class="val-mult">{{ row.rev_multiple }}x</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
        </tr>
        {% endif %}
      {% endfor %}
    </tbody>
    {% if stats %}
    <tbody>
      <tr class="stats-header"><td colspan="11">Summary Statistics</td></tr>
      {% for s in stats %}
      <tr class="stat-row">
        <td colspan="2">{{ s.name }}</td>
        <td>{% if s.market_cap is not none %}${{ s.market_cap }}B{% else %}—{% endif %}</td>
        <td>{% if s.revenue is not none %}${{ s.revenue }}B{% else %}—{% endif %}</td>
        <td>{% if s.rev_growth is not none %}{{ '+' if s.rev_growth >= 0 else '' }}{{ s.rev_growth }}%{% else %}—{% endif %}</td>
        <td>{% if s.ebitda is not none %}${{ s.ebitda }}B{% else %}—{% endif %}</td>
        <td>{% if s.ocf is not none %}${{ s.ocf }}B{% else %}—{% endif %}</td>
        <td>{% if s.debt is not none %}${{ s.debt }}B{% else %}—{% endif %}</td>
        <td>{% if s.cash is not none %}${{ s.cash }}B{% else %}—{% endif %}</td>
        <td>{% if s.ev is not none %}${{ s.ev }}B{% else %}—{% endif %}</td>
        <td>{% if s.rev_multiple is not none %}{{ s.rev_multiple }}x{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
    </tbody>
    {% endif %}
  </table>
  {% else %}
  <div class="empty-state">
    <p>No data yet.</p>
    <p>Add tickers in the CLI app (<code>python app.py</code>), then click <strong>Refresh Now</strong>.</p>
  </div>
  {% endif %}
</div>
<script>
  let sortCol = -1, sortAsc = true;
  const dataAttrs = [null, null, "market-cap", "revenue", "rev-growth", "ebitda", "ocf", "debt", "cash", "ev", "rev-multiple"];

  function sortTable(col) {
    const tbody = document.getElementById("ticker-rows");
    const rows  = Array.from(tbody.querySelectorAll("tr"));
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    rows.sort((a, b) => {
      let av, bv;
      if (col <= 1) {
        av = a.cells[col === 0 ? 0 : 1]?.textContent.trim() ?? "";
        bv = b.cells[col === 0 ? 0 : 1]?.textContent.trim() ?? "";
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      const attr = dataAttrs[col];
      const key = attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      av = parseFloat(a.dataset[key]);
      bv = parseFloat(b.dataset[key]);
      if (isNaN(av)) av = sortAsc ? Infinity : -Infinity;
      if (isNaN(bv)) bv = sortAsc ? Infinity : -Infinity;
      return sortAsc ? av - bv : bv - av;
    });
    rows.forEach(r => tbody.appendChild(r));
    document.querySelectorAll("thead th").forEach((th, i) => {
      th.classList.remove("sorted-asc", "sorted-desc");
      if (i === col) th.classList.add(sortAsc ? "sorted-asc" : "sorted-desc");
    });
  }

  function triggerRefresh() {
    const btn = document.getElementById("refresh-btn");
    const status = document.getElementById("refresh-status");
    btn.disabled = true;
    btn.querySelector("svg").classList.add("spin");
    status.textContent = "Fetching data...";
    fetch("/api/refresh", { method: "POST" })
      .then(r => r.json())
      .then(() => { status.textContent = "Fetching... page will reload when done."; pollForUpdate(); })
      .catch(() => { status.textContent = "Error."; btn.disabled = false; btn.querySelector("svg").classList.remove("spin"); });
  }

  function pollForUpdate() {
    const start = Date.now();
    const interval = setInterval(() => {
      fetch("/api/status").then(r => r.json()).then(data => {
        if (data.last_updated && new Date(data.last_updated).getTime() > start - 2000) {
          clearInterval(interval); location.reload();
        }
      });
    }, 3000);
    setTimeout(() => clearInterval(interval), 180000);
  }
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with _lock:
        raw_data = list(_cache.get("data", []))
        last_updated = _cache.get("last_updated")

    rows = [build_row(d) for d in raw_data]
    valid = [d for d in raw_data if not d.get("error")]

    stats = []
    if valid:
        all_agg, above_agg, _, n_above = summary_stats(raw_data)
        stats = [
            build_stat_row("All — Median", all_agg, "median"),
            build_stat_row("All — Average", all_agg, "mean"),
            build_stat_row(f"Above Median Growth — Median (n={n_above})", above_agg, "median"),
            build_stat_row(f"Above Median Growth — Average (n={n_above})", above_agg, "mean"),
        ]

    if last_updated:
        try:
            dt = datetime.fromisoformat(last_updated)
            last_updated_fmt = dt.strftime("%B %d, %Y at %I:%M %p")
        except Exception:
            last_updated_fmt = last_updated
    else:
        last_updated_fmt = "Never"

    return render_template_string(
        HTML_TEMPLATE,
        rows=rows,
        stats=stats,
        last_updated=last_updated_fmt,
        ticker_count=len(load_tickers()),
    )


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger an immediate data refresh (runs in background thread)."""
    t = threading.Thread(target=refresh_data, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify({
            "last_updated": _cache.get("last_updated"),
            "ticker_count": len(_cache.get("data", [])),
        })


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_cache()

    # If no cached data yet, fetch immediately on startup
    if not _cache.get("data"):
        t = threading.Thread(target=refresh_data, daemon=True)
        t.start()

    # Schedule daily refresh at 4:30 PM (after US market close)
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_data, "cron", hour=16, minute=30, id="daily_refresh")
    scheduler.start()

    print("Starting Yahoo Finance Dashboard at http://localhost:5000")
    print("Data refreshes daily at 4:30 PM. Press Ctrl+C to stop.\n")

    app.run(debug=False, port=5000, use_reloader=False)
