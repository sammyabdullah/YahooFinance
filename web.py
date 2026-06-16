#!/usr/bin/env python3
"""
Yahoo Finance Web Dashboard
Serves a live webpage that refreshes data from Yahoo Finance daily.
Run with: python web.py
Then open: http://localhost:5000
"""

import csv
import io
import json
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, Response
from apscheduler.schedulers.background import BackgroundScheduler

# Reuse data logic from app.py
from app import fetch_ticker_data, load_tickers, summary_stats, _agg, NUMERIC_KEYS

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
        "ebitda_margin": fmt_pct(d.get("ebitda_margin")),
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
    em = agg["ebitda_margin"][stat]
    return {
        "name": label,
        "market_cap": fmt_billions(agg["market_cap"][stat]),
        "revenue": fmt_billions(agg["revenue"][stat]),
        "rev_growth": fmt_pct(g),
        "ebitda": fmt_billions(agg["ebitda"][stat]),
        "ebitda_margin": fmt_pct(em),
        "ocf": fmt_billions(agg["ocf"][stat]),
        "debt": fmt_billions(agg["debt"][stat]),
        "cash": fmt_billions(agg["cash"][stat]),
        "ev": fmt_billions(agg["ev"][stat]),
        "rev_multiple": round(rm, 1) if rm is not None else None,
    }


# ── Refresh logic ─────────────────────────────────────────────────────────────

HISTORY_FILE = Path("history.csv")
HISTORY_HEADERS = [
    "Date", "Section", "Type",
    "Rev Multiple (x)", "Enterprise Value ($B)", "Market Cap ($B)",
    "LTM Revenue ($B)", "Rev Growth (%)", "LTM EBITDA ($B)",
    "EBITDA Margin (%)", "LTM Op CF ($B)", "Debt ($B)", "Cash ($B)",
]


def _fmt_h(value, scale=1.0) -> str:
    """Format a raw value for history CSV (already scaled by build_stat_row)."""
    if value is None:
        return ""
    try:
        return str(round(float(value) * scale, 2))
    except (TypeError, ValueError):
        return ""


def append_history(data: list[dict]) -> None:
    """Append today's summary stats to history.csv."""
    valid = [d for d in data if not d.get("error")]
    if not valid:
        return

    all_agg, above_agg, _, n_above = summary_stats(data)
    top30 = sorted(
        [d for d in valid if d.get("rev_growth") is not None],
        key=lambda d: d["rev_growth"], reverse=True
    )[:30]
    top30_profitable = sorted(
        [d for d in valid if d.get("ebitda") is not None and d.get("revenue")],
        key=lambda d: d["ebitda"] / d["revenue"], reverse=True
    )[:30]

    sections = [
        ("All Companies", all_agg),
        ("Above Median Growth", above_agg),
        ("Top 30 Fastest Growing", _agg(top30) if top30 else None),
        ("Top 30 Most Profitable", _agg(top30_profitable) if top30_profitable else None),
    ]

    date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    write_header = not HISTORY_FILE.exists()

    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(HISTORY_HEADERS)
        for section_name, agg in sections:
            if agg is None:
                continue
            for stat_type in ("Median", "Average"):
                stat = "median" if stat_type == "Median" else "mean"
                writer.writerow([
                    date_str, section_name, stat_type,
                    _fmt_h(agg["rev_multiple"][stat]),
                    _fmt_h(agg["ev"][stat], 1/1e9),
                    _fmt_h(agg["market_cap"][stat], 1/1e9),
                    _fmt_h(agg["revenue"][stat], 1/1e9),
                    _fmt_h(agg["rev_growth"][stat], 100),
                    _fmt_h(agg["ebitda"][stat], 1/1e9),
                    _fmt_h(agg["ebitda_margin"][stat], 100),
                    _fmt_h(agg["ocf"][stat], 1/1e9),
                    _fmt_h(agg["debt"][stat], 1/1e9),
                    _fmt_h(agg["cash"][stat], 1/1e9),
                ])
    print(f"[{datetime.now().strftime('%H:%M:%S')}] History recorded for {date_str}.")


def refresh_data() -> None:
    """Fetch fresh data for all tickers and update the cache."""
    global _cache
    tickers = load_tickers()
    if not tickers:
        return
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing {len(tickers)} tickers…")
    data = [fetch_ticker_data(t) for t in tickers]
    with _lock:
        _cache = {"data": data, "last_updated": datetime.now(timezone.utc).isoformat()}
    save_cache()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Refresh complete.")


def scheduled_refresh() -> None:
    """Daily 4:30 PM refresh — fetches data then appends summary to history."""
    refresh_data()
    with _lock:
        data = list(_cache.get("data", []))
    append_history(data)


# ── HTML template (embedded so no separate templates/ folder is needed) ───────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SaaS Multiples Index</title>
  <link rel="icon" type="image/png" href="/static/favicon.png" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
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
    .meta-btn {
      display: inline-flex; align-items: center; gap: 6px; padding: 5px 14px;
      border-radius: 6px; border: 1px solid #30363d; background: #21262d;
      color: #c9d1d9; font-size: 0.82rem; cursor: pointer; transition: background 0.15s, border-color 0.15s;
      text-decoration: none;
    }
    .meta-btn:hover { background: #30363d; border-color: #58a6ff; }
    .meta-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .meta-btn svg { width: 14px; height: 14px; }
    #refresh-btn { display: inline-flex; align-items: center; gap: 6px; padding: 5px 14px;
      border-radius: 6px; border: 1px solid #30363d; background: #21262d;
      color: #c9d1d9; font-size: 0.82rem; cursor: pointer; transition: background 0.15s, border-color 0.15s;
    }
    #refresh-btn:hover { background: #30363d; border-color: #58a6ff; }
    #refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    #refresh-btn svg { width: 14px; height: 14px; }
    #refresh-status { font-size: 0.78rem; color: #8b949e; }
    .table-wrap { overflow-x: auto; overflow-y: auto; max-height: calc(100vh - 120px); border-radius: 10px; border: 1px solid #21262d; }
    .table-wrap::-webkit-scrollbar { width: 8px; height: 8px; }
    .table-wrap::-webkit-scrollbar-track { background: #0d1117; }
    .table-wrap::-webkit-scrollbar-thumb { background: #58a6ff; border-radius: 4px; }
    .table-wrap::-webkit-scrollbar-thumb:hover { background: #79b8ff; }
    .table-wrap { scrollbar-color: #58a6ff #0d1117; scrollbar-width: thin; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; white-space: nowrap; }
    thead th {
      background: #161b22; color: #8b949e; font-weight: 600; font-size: 0.75rem;
      text-transform: uppercase; letter-spacing: 0.04em; padding: 10px 14px;
      text-align: right; border-bottom: 1px solid #21262d; cursor: pointer; user-select: none;
      position: sticky; top: 0; z-index: 1;
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
    .stat-row td:first-child { color: #8b949e; font-weight: 400; text-align: left; }
    .stat-row td:nth-child(n+2) { text-align: right; font-family: inherit; font-size: 0.82rem; color: #8b949e; font-weight: 400; }
    .error-row td { color: #f85149; font-size: 0.82rem; }
    .empty-state { text-align: center; padding: 60px 20px; color: #8b949e; }
    .empty-state p { margin-bottom: 8px; }
    .chart-row td { background: #0d1117; padding: 16px 20px 20px; border-top: none; }
    .chart-placeholder { color: #484f58; font-size: 0.78rem; font-style: italic; text-align: center; padding: 40px 0; border: 1px dashed #21262d; border-radius: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spin { animation: spin 1s linear infinite; display: inline-block; }
  </style>
</head>
<body>
<header>
  <h1>SaaS Multiples Index</h1>
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
  <a href="/export.csv" class="meta-btn" download>
    <svg viewBox="0 0 16 16" fill="currentColor">
      <path d="M.5 9.9a.5.5 0 0 1 .5.5v2.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2.5a.5.5 0 0 1 1 0v2.5a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2v-2.5a.5.5 0 0 1 .5-.5z"/>
      <path d="M7.646 11.854a.5.5 0 0 0 .708 0l3-3a.5.5 0 0 0-.708-.708L8.5 10.293V1.5a.5.5 0 0 0-1 0v8.793L5.354 8.146a.5.5 0 1 0-.708.708l3 3z"/>
    </svg>
    Export CSV
  </a>
  <span id="refresh-status"></span>
</div>
<div class="table-wrap">
  {% if rows %}
  <table id="watchlist">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Company</th>
        <th onclick="sortTable(1)">Ticker</th>
        <th onclick="sortTable(2)" title="Enterprise Value / LTM Revenue">Rev Mult.</th>
        <th onclick="sortTable(3)" title="Enterprise Value = Mkt Cap + Debt - Cash ($B)">Ent. Value</th>
        <th onclick="sortTable(4)" title="Market Capitalization ($B)">Mkt Cap</th>
        <th onclick="sortTable(5)" title="Last Twelve Months Revenue ($B)">LTM Rev</th>
        <th onclick="sortTable(6)" title="Year-over-Year Revenue Growth">Rev Growth</th>
        <th onclick="sortTable(7)" title="LTM EBITDA ($B)">LTM EBITDA</th>
        <th onclick="sortTable(8)" title="EBITDA / LTM Revenue">EBITDA Margin</th>
        <th onclick="sortTable(9)" title="LTM Operating Cash Flow ($B)">LTM Op CF</th>
        <th onclick="sortTable(10)" title="Total Debt ($B)">Debt</th>
        <th onclick="sortTable(11)" title="Total Cash ($B)">Cash</th>
      </tr>
    </thead>
    <tbody id="ticker-rows">
      {% for row in rows %}
        {% if row.error %}
        <tr class="error-row">
          <td colspan="12">Error ({{ row.ticker }}): {{ row.error[:80] }}</td>
        </tr>
        {% else %}
        <tr
          data-market-cap="{{ row.market_cap if row.market_cap is not none else '' }}"
          data-revenue="{{ row.revenue if row.revenue is not none else '' }}"
          data-rev-growth="{{ row.rev_growth if row.rev_growth is not none else '' }}"
          data-ebitda="{{ row.ebitda if row.ebitda is not none else '' }}"
          data-ebitda-margin="{{ row.ebitda_margin if row.ebitda_margin is not none else '' }}"
          data-ocf="{{ row.ocf if row.ocf is not none else '' }}"
          data-debt="{{ row.debt if row.debt is not none else '' }}"
          data-cash="{{ row.cash if row.cash is not none else '' }}"
          data-ev="{{ row.ev if row.ev is not none else '' }}"
          data-rev-multiple="{{ row.rev_multiple if row.rev_multiple is not none else '' }}"
        >
          <td>{{ row.name }}</td>
          <td>{{ row.ticker }}</td>
          <td>{% if row.rev_multiple is not none %}<span class="val-mult">{{ row.rev_multiple }}x</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.ev is not none %}<span class="val-ev">${{ row.ev }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.market_cap is not none %}<span class="val-pos">${{ row.market_cap }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.revenue is not none %}<span class="val-pos">${{ row.revenue }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>
            {% if row.rev_growth is not none %}
              <span class="{{ 'val-pos' if row.rev_growth >= 0 else 'val-neg' }}">{{ '+' if row.rev_growth >= 0 else '' }}{{ row.rev_growth }}%</span>
            {% else %}<span class="val-null">—</span>{% endif %}
          </td>
          <td>{% if row.ebitda is not none %}<span class="{{ 'val-pos' if row.ebitda >= 0 else 'val-neg' }}">${{ row.ebitda }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>
            {% if row.ebitda_margin is not none %}
              <span class="{{ 'val-pos' if row.ebitda_margin >= 0 else 'val-neg' }}">{{ '+' if row.ebitda_margin >= 0 else '' }}{{ row.ebitda_margin }}%</span>
            {% else %}<span class="val-null">—</span>{% endif %}
          </td>
          <td>{% if row.ocf is not none %}<span class="{{ 'val-pos' if row.ocf >= 0 else 'val-neg' }}">${{ row.ocf }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.debt is not none %}<span class="val-debt">${{ row.debt }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
          <td>{% if row.cash is not none %}<span class="val-cash">${{ row.cash }}B</span>{% else %}<span class="val-null">—</span>{% endif %}</td>
        </tr>
        {% endif %}
      {% endfor %}
    </tbody>
    {% if stats %}
    <tbody>
      <tr class="stats-header"><td colspan="12">Summary Statistics</td></tr>
      {% for s in stats %}
      <tr class="stat-row">
        <td colspan="2">{{ s.name }}</td>
        <td>{% if s.rev_multiple is not none %}{{ s.rev_multiple }}x{% else %}—{% endif %}</td>
        <td>{% if s.ev is not none %}${{ s.ev }}B{% else %}—{% endif %}</td>
        <td>{% if s.market_cap is not none %}${{ s.market_cap }}B{% else %}—{% endif %}</td>
        <td>{% if s.revenue is not none %}${{ s.revenue }}B{% else %}—{% endif %}</td>
        <td>{% if s.rev_growth is not none %}{{ '+' if s.rev_growth >= 0 else '' }}{{ s.rev_growth }}%{% else %}—{% endif %}</td>
        <td>{% if s.ebitda is not none %}${{ s.ebitda }}B{% else %}—{% endif %}</td>
        <td>{% if s.ebitda_margin is not none %}{{ '+' if s.ebitda_margin >= 0 else '' }}{{ s.ebitda_margin }}%{% else %}—{% endif %}</td>
        <td>{% if s.ocf is not none %}${{ s.ocf }}B{% else %}—{% endif %}</td>
        <td>{% if s.debt is not none %}${{ s.debt }}B{% else %}—{% endif %}</td>
        <td>{% if s.cash is not none %}${{ s.cash }}B{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
      <tr class="chart-row"><td colspan="12"><div style="height:140px;position:relative;"><canvas id="chart-all"></canvas></div></td></tr>
    </tbody>
    {% endif %}
    {% if above_stats %}
    <tbody>
      <tr class="stats-header"><td colspan="12">Above Median Growth Companies</td></tr>
      {% for s in above_stats %}
      <tr class="stat-row">
        <td colspan="2">{{ s.name }}</td>
        <td>{% if s.rev_multiple is not none %}{{ s.rev_multiple }}x{% else %}—{% endif %}</td>
        <td>{% if s.ev is not none %}${{ s.ev }}B{% else %}—{% endif %}</td>
        <td>{% if s.market_cap is not none %}${{ s.market_cap }}B{% else %}—{% endif %}</td>
        <td>{% if s.revenue is not none %}${{ s.revenue }}B{% else %}—{% endif %}</td>
        <td>{% if s.rev_growth is not none %}{{ '+' if s.rev_growth >= 0 else '' }}{{ s.rev_growth }}%{% else %}—{% endif %}</td>
        <td>{% if s.ebitda is not none %}${{ s.ebitda }}B{% else %}—{% endif %}</td>
        <td>{% if s.ebitda_margin is not none %}{{ '+' if s.ebitda_margin >= 0 else '' }}{{ s.ebitda_margin }}%{% else %}—{% endif %}</td>
        <td>{% if s.ocf is not none %}${{ s.ocf }}B{% else %}—{% endif %}</td>
        <td>{% if s.debt is not none %}${{ s.debt }}B{% else %}—{% endif %}</td>
        <td>{% if s.cash is not none %}${{ s.cash }}B{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
      <tr class="chart-row"><td colspan="12"><div style="height:140px;position:relative;"><canvas id="chart-above"></canvas></div></td></tr>
    </tbody>
    {% endif %}
    {% if top30_stats %}
    <tbody>
      <tr class="stats-header"><td colspan="12">Top 30 Fastest Growing Companies</td></tr>
      {% for s in top30_stats %}
      <tr class="stat-row">
        <td colspan="2">{{ s.name }}</td>
        <td>{% if s.rev_multiple is not none %}{{ s.rev_multiple }}x{% else %}—{% endif %}</td>
        <td>{% if s.ev is not none %}${{ s.ev }}B{% else %}—{% endif %}</td>
        <td>{% if s.market_cap is not none %}${{ s.market_cap }}B{% else %}—{% endif %}</td>
        <td>{% if s.revenue is not none %}${{ s.revenue }}B{% else %}—{% endif %}</td>
        <td>{% if s.rev_growth is not none %}{{ '+' if s.rev_growth >= 0 else '' }}{{ s.rev_growth }}%{% else %}—{% endif %}</td>
        <td>{% if s.ebitda is not none %}${{ s.ebitda }}B{% else %}—{% endif %}</td>
        <td>{% if s.ebitda_margin is not none %}{{ '+' if s.ebitda_margin >= 0 else '' }}{{ s.ebitda_margin }}%{% else %}—{% endif %}</td>
        <td>{% if s.ocf is not none %}${{ s.ocf }}B{% else %}—{% endif %}</td>
        <td>{% if s.debt is not none %}${{ s.debt }}B{% else %}—{% endif %}</td>
        <td>{% if s.cash is not none %}${{ s.cash }}B{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
      <tr class="chart-row"><td colspan="12"><div style="height:140px;position:relative;"><canvas id="chart-top30grow"></canvas></div></td></tr>
    </tbody>
    {% endif %}
    {% if top30_profitable_stats %}
    <tbody>
      <tr class="stats-header"><td colspan="12">Top 30 Most Profitable Companies</td></tr>
      {% for s in top30_profitable_stats %}
      <tr class="stat-row">
        <td colspan="2">{{ s.name }}</td>
        <td>{% if s.rev_multiple is not none %}{{ s.rev_multiple }}x{% else %}—{% endif %}</td>
        <td>{% if s.ev is not none %}${{ s.ev }}B{% else %}—{% endif %}</td>
        <td>{% if s.market_cap is not none %}${{ s.market_cap }}B{% else %}—{% endif %}</td>
        <td>{% if s.revenue is not none %}${{ s.revenue }}B{% else %}—{% endif %}</td>
        <td>{% if s.rev_growth is not none %}{{ '+' if s.rev_growth >= 0 else '' }}{{ s.rev_growth }}%{% else %}—{% endif %}</td>
        <td>{% if s.ebitda is not none %}${{ s.ebitda }}B{% else %}—{% endif %}</td>
        <td>{% if s.ebitda_margin is not none %}{{ '+' if s.ebitda_margin >= 0 else '' }}{{ s.ebitda_margin }}%{% else %}—{% endif %}</td>
        <td>{% if s.ocf is not none %}${{ s.ocf }}B{% else %}—{% endif %}</td>
        <td>{% if s.debt is not none %}${{ s.debt }}B{% else %}—{% endif %}</td>
        <td>{% if s.cash is not none %}${{ s.cash }}B{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
      <tr class="chart-row"><td colspan="12"><div style="height:140px;position:relative;"><canvas id="chart-top30prof"></canvas></div></td></tr>
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
  const dataAttrs = [null, null, "rev-multiple", "ev", "market-cap", "revenue", "rev-growth", "ebitda", "ebitda-margin", "ocf", "debt", "cash"];

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

  const CHART_DEFS = [
    { id: "chart-all",       section: "All Companies" },
    { id: "chart-above",     section: "Above Median Growth" },
    { id: "chart-top30grow", section: "Top 30 Fastest Growing" },
    { id: "chart-top30prof", section: "Top 30 Most Profitable" },
  ];
  const chartInstances = {};
  const CHART_DEFAULTS = {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: "#8b949e", font: { size: 11 }, boxWidth: 12 } },
      tooltip: { callbacks: { label: ctx => " " + ctx.dataset.label + ": " + ctx.parsed.y + "x" } }
    },
    scales: {
      x: { ticks: { color: "#8b949e", font: { size: 10 }, maxRotation: 0 }, grid: { color: "#21262d" } },
      y: { ticks: { color: "#8b949e", font: { size: 10 }, callback: v => v + "x" }, grid: { color: "#21262d" } }
    }
  };

  function showPlaceholder(id) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    const wrap = canvas.parentElement;
    wrap.style.height = "auto";
    canvas.style.display = "none";
    const p = document.createElement("div");
    p.className = "chart-placeholder";
    p.textContent = "Revenue Multiple chart — data will appear after the 4:30 PM ET refresh";
    wrap.appendChild(p);
  }

  async function renderCharts() {
    let rows;
    try { rows = await fetch("/api/history").then(r => r.json()); }
    catch(e) { CHART_DEFS.forEach(c => showPlaceholder(c.id)); return; }
    if (!rows.length) { CHART_DEFS.forEach(c => showPlaceholder(c.id)); return; }

    for (const { id, section } of CHART_DEFS) {
      const canvas = document.getElementById(id);
      if (!canvas) continue;
      const sRows = rows.filter(r => r.Section === section);
      const dates = [...new Set(sRows.map(r => r.Date))].sort();
      if (!dates.length) { showPlaceholder(id); continue; }

      const get = (date, type, col) => {
        const r = sRows.find(r => r.Date === date && r.Type === type);
        return r ? (parseFloat(r[col]) || null) : null;
      };

      const medians = dates.map(d => get(d, "Median", "Rev Multiple (x)"));
      const avgs    = dates.map(d => get(d, "Average", "Rev Multiple (x)"));

      if (chartInstances[id]) chartInstances[id].destroy();
      chartInstances[id] = new Chart(canvas, {
        type: "line",
        data: {
          labels: dates,
          datasets: [
            { label: "Median", data: medians, borderColor: "#58a6ff", backgroundColor: "rgba(88,166,255,0.08)", fill: true,  tension: 0.3, pointRadius: 3 },
            { label: "Average", data: avgs,   borderColor: "#d2a8ff", backgroundColor: "transparent",           fill: false, tension: 0.3, pointRadius: 3 },
          ]
        },
        options: { ...CHART_DEFAULTS, plugins: { ...CHART_DEFAULTS.plugins, title: { display: true, text: "Revenue Multiple (x)", color: "#8b949e", font: { size: 11 } } } }
      });
    }
  }

  renderCharts();

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
    above_stats = []
    top30_stats = []
    top30_profitable_stats = []
    if valid:
        all_agg, above_agg, _, n_above = summary_stats(raw_data)
        stats = [
            build_stat_row("All — Median", all_agg, "median"),
            build_stat_row("All — Average", all_agg, "mean"),
        ]
        above_stats = [
            build_stat_row(f"Above Median Growth — Median (n={n_above})", above_agg, "median"),
            build_stat_row(f"Above Median Growth — Average (n={n_above})", above_agg, "mean"),
        ]
        top30 = sorted(
            [d for d in valid if d.get("rev_growth") is not None],
            key=lambda d: d["rev_growth"], reverse=True
        )[:30]
        if top30:
            top30_agg = _agg(top30)
            n30 = len(top30)
            top30_stats = [
                build_stat_row(f"Top {n30} Fastest Growing — Median", top30_agg, "median"),
                build_stat_row(f"Top {n30} Fastest Growing — Average", top30_agg, "mean"),
            ]
        top30_profitable = sorted(
            [d for d in valid if d.get("ebitda") is not None and d.get("revenue")],
            key=lambda d: d["ebitda"] / d["revenue"], reverse=True
        )[:30]
        if top30_profitable:
            top30_prof_agg = _agg(top30_profitable)
            n30p = len(top30_profitable)
            top30_profitable_stats = [
                build_stat_row(f"Top {n30p} Most Profitable — Median", top30_prof_agg, "median"),
                build_stat_row(f"Top {n30p} Most Profitable — Average", top30_prof_agg, "mean"),
            ]

    if last_updated:
        try:
            dt = datetime.fromisoformat(last_updated)
            eastern = ZoneInfo("America/New_York")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_et = dt.astimezone(eastern)
            last_updated_fmt = dt_et.strftime("%B %-d, %Y at %-I:%M %p ET")
        except Exception:
            last_updated_fmt = last_updated
    else:
        last_updated_fmt = "Never"

    return render_template_string(
        HTML_TEMPLATE,
        rows=rows,
        stats=stats,
        above_stats=above_stats,
        top30_stats=top30_stats,
        top30_profitable_stats=top30_profitable_stats,
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


@app.route("/export.csv")
def export_csv():
    """Return all ticker data as a downloadable CSV file."""
    with _lock:
        raw_data = list(_cache.get("data", []))
        last_updated = _cache.get("last_updated", "")

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Company", "Ticker",
        "Market Cap ($B)", "LTM Revenue ($B)", "Rev Growth (%)",
        "LTM EBITDA ($B)", "EBITDA Margin (%)", "LTM Op CF ($B)",
        "Debt ($B)", "Cash ($B)", "Enterprise Value ($B)", "Rev Multiple (x)",
        "As Of",
    ])

    for d in raw_data:
        if d.get("error"):
            writer.writerow([d.get("ticker", ""), "", "", "", "", "", "", "", "", "", "", "ERROR: " + d["error"]])
            continue
        writer.writerow([
            d.get("name", ""),
            d.get("ticker", ""),
            fmt_billions(d.get("market_cap")),
            fmt_billions(d.get("revenue")),
            fmt_pct(d.get("rev_growth")),
            fmt_billions(d.get("ebitda")),
            fmt_pct(d.get("ebitda_margin")),
            fmt_billions(d.get("ocf")),
            fmt_billions(d.get("debt")),
            fmt_billions(d.get("cash")),
            fmt_billions(d.get("ev")),
            round(d["rev_multiple"], 1) if d.get("rev_multiple") is not None else "",
            last_updated[:10] if last_updated else "",
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=saas-multiples.csv"},
    )


@app.route("/api/history")
def api_history():
    """Return historical summary stats as JSON for charting."""
    if not HISTORY_FILE.exists():
        return jsonify([])
    rows = []
    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(dict(row))
    return jsonify(rows)


@app.route("/history.csv")
def download_history():
    """Download the full historical summary stats CSV."""
    if not HISTORY_FILE.exists():
        return Response("No history recorded yet.", mimetype="text/plain", status=404)
    return Response(
        HISTORY_FILE.read_bytes(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=saas-multiples-history.csv"},
    )


# ── Startup ───────────────────────────────────────────────────────────────────
# Runs at import time so both `python web.py` and `gunicorn web:app` initialize.

load_cache()
if not _cache.get("data"):
    threading.Thread(target=refresh_data, daemon=True).start()

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_refresh, "cron", hour=16, minute=30, id="daily_refresh", timezone="America/New_York")
scheduler.start()

if __name__ == "__main__":
    print("Starting Yahoo Finance Dashboard at http://localhost:5000")
    print("Data refreshes daily at 4:30 PM. Press Ctrl+C to stop.\n")
    app.run(debug=False, port=5000, use_reloader=False)
