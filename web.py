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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import yfinance as yf
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


def _write_history_for_date(data: list[dict], date_str: str) -> bool:
    """Compute summary sections for `data` and append a row-set for date_str.

    Returns True if rows were written, False if skipped (no valid data, or
    date_str is already present in history.csv).
    """
    valid = [d for d in data if not d.get("error")]
    if not valid:
        return False

    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
            if any(row and row[0] == date_str for row in csv.reader(f)):
                return False

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
    return True


def append_history(data: list[dict]) -> None:
    """Append today's summary stats to history.csv."""
    date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if _write_history_for_date(data, date_str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] History recorded for {date_str}.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] History already recorded (or no data) for {date_str}.")


def backfill_history() -> None:
    """Reconstruct any missing trading days in history.csv.

    Railway rebuilds the app's filesystem from git on every deploy, so rows
    appended by the daily scheduled job since the last commit get wiped the
    next time code is pushed. This fetches historical closing prices for the
    gap and re-derives approximate daily rows (market cap/EV/multiple scaled
    by price movement, other fundamentals held at their latest fetched
    values) so the chart always reaches the most recent completed session,
    regardless of what got wiped between deploys.
    """
    with _lock:
        data = list(_cache.get("data", []))
    valid = [d for d in data if not d.get("error") and d.get("market_cap")]
    if not valid or not HISTORY_FILE.exists():
        return

    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
        existing_dates = {row[0] for row in csv.reader(f) if row and row[0] != "Date"}
    if not existing_dates:
        return

    eastern = ZoneInfo("America/New_York")
    now_et = datetime.now(eastern)
    last_date = datetime.strptime(max(existing_dates), "%Y-%m-%d").date()
    start = last_date + timedelta(days=1)
    cutoff = now_et.replace(hour=16, minute=30, second=0, microsecond=0)
    end = now_et.date() if now_et.replace(tzinfo=None) >= cutoff.replace(tzinfo=None) else now_et.date() - timedelta(days=1)
    if start > end:
        return

    tickers = [d["ticker"] for d in valid]
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Backfilling history from {start} to {end}…")
    try:
        hist = yf.download(
            tickers=tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
            progress=False, auto_adjust=False, group_by="ticker", threads=True,
        )
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Backfill download failed: {e}")
        return
    if hist is None or hist.empty:
        return

    by_ticker = {d["ticker"]: d for d in valid}
    ref_close = {}
    for t in tickers:
        try:
            closes = hist[t]["Close"].dropna()
        except Exception:
            continue
        if not closes.empty:
            ref_close[t] = float(closes.iloc[-1])

    trading_days = sorted(
        ts.strftime("%Y-%m-%d") for ts in hist.index
        if start.isoformat() <= ts.strftime("%Y-%m-%d") <= end.isoformat()
    )

    written = 0
    for day in trading_days:
        if day in existing_dates:
            continue
        day_ts = pd.Timestamp(day)
        synthetic = []
        for t in tickers:
            base = by_ticker[t]
            if t not in ref_close or not ref_close[t]:
                continue
            try:
                day_close = hist[t]["Close"].get(day_ts)
            except Exception:
                day_close = None
            if day_close is None or pd.isna(day_close) or base.get("market_cap") is None:
                continue
            ratio = float(day_close) / ref_close[t]
            market_cap = base["market_cap"] * ratio
            debt = base.get("debt") or 0
            cash = base.get("cash") or 0
            ev = market_cap + debt - cash
            revenue = base.get("revenue")
            rev_multiple = (ev / revenue) if revenue else None
            synthetic.append({**base, "market_cap": market_cap, "ev": ev, "rev_multiple": rev_multiple})
        if synthetic and _write_history_for_date(synthetic, day):
            written += 1
            existing_dates.add(day)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Backfill complete: {written} day(s) added.")


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
    """Daily 4:30 PM refresh — fetches data, backfills any gap, then appends today."""
    refresh_data()
    backfill_history()
    with _lock:
        data = list(_cache.get("data", []))
    append_history(data)


# ── HTML template (embedded so no separate templates/ folder is needed) ───────
#
# Row/stat markup lives in Jinja macros so the full page and the /api/table
# refresh fragment (used by "Refresh Now" to update numbers without touching
# the chart canvases) render from one source of truth.

ROW_MACROS = r"""
{% macro ticker_row(row) %}
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
{% endmacro %}

{% macro stat_row(s) %}
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
{% endmacro %}

{% macro stats_block(title, stats) %}
<tr class="stats-header"><td colspan="12">{{ title }}</td></tr>
<tr class="section-cols"><td></td><td></td><td>Rev Mult.</td><td>Ent. Value</td><td>Mkt Cap</td><td>LTM Rev</td><td>Rev Growth</td><td>LTM EBITDA</td><td>EBITDA Margin</td><td>LTM Op CF</td><td>Debt</td><td>Cash</td></tr>
{% for s in stats %}{{ stat_row(s) }}{% endfor %}
{% endmacro %}
"""


def render_fragment(body: str, **ctx) -> str:
    """Render a small Jinja snippet with the shared row/stat macros in scope."""
    return render_template_string(ROW_MACROS + body, **ctx)


def format_last_updated(last_updated) -> str:
    if not last_updated:
        return "Never"
    try:
        dt = datetime.fromisoformat(last_updated)
        eastern = ZoneInfo("America/New_York")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_et = dt.astimezone(eastern)
        return dt_et.strftime("%B %-d, %Y at %-I:%M %p ET")
    except Exception:
        return last_updated


def compute_table_context() -> dict:
    """Build the row/stat data shown in the table — shared by the full page
    render and the /api/table refresh fragment."""
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

    return {
        "rows": rows,
        "stats": stats,
        "above_stats": above_stats,
        "top30_stats": top30_stats,
        "top30_profitable_stats": top30_profitable_stats,
        "last_updated": format_last_updated(last_updated),
        "ticker_count": len(load_tickers()),
    }


HTML_TEMPLATE = ROW_MACROS + r"""<!DOCTYPE html>
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
    .table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #21262d; }
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
    .stat-row:last-child td { border-bottom: 1px solid #21262d; }
    .error-row td { color: #f85149; font-size: 0.82rem; }
    .empty-state { text-align: center; padding: 60px 20px; color: #8b949e; }
    .empty-state p { margin-bottom: 8px; }
    .section-cols td {
      background: #161b22; color: #8b949e; font-weight: 600; font-size: 0.75rem;
      text-transform: uppercase; letter-spacing: 0.04em; padding: 10px 14px;
      text-align: right; border-bottom: 1px solid #21262d;
    }
    .section-cols td:first-child, .section-cols td:nth-child(2) { text-align: left; }
    .chart-row td { background: #0d1117; padding: 16px 20px 20px; border-top: none; }
    .chart-pair { display: flex; gap: 16px; height: 280px; }
    .chart-box { flex: 1 1 0; min-width: 0; position: relative; }
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
  <span>Last updated: <strong id="last-updated">{{ last_updated }}</strong></span>
  <span id="ticker-count-label">{{ ticker_count }} ticker{{ 's' if ticker_count != 1 else '' }}</span>
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
      {% for row in rows %}{{ ticker_row(row) }}{% endfor %}
    </tbody>
    {% if stats %}
    <tbody id="stats-body-all">
      {{ stats_block("Summary Statistics", stats) }}
    </tbody>
    <tbody>
      <tr class="chart-row"><td colspan="12"><div class="chart-pair"><div class="chart-box"><canvas id="chart-all-median"></canvas></div><div class="chart-box"><canvas id="chart-all-average"></canvas></div></div></td></tr>
    </tbody>
    {% endif %}
    {% if above_stats %}
    <tbody id="stats-body-above">
      {{ stats_block("Above Median Growth Companies", above_stats) }}
    </tbody>
    <tbody>
      <tr class="chart-row"><td colspan="12"><div class="chart-pair"><div class="chart-box"><canvas id="chart-above-median"></canvas></div><div class="chart-box"><canvas id="chart-above-average"></canvas></div></div></td></tr>
    </tbody>
    {% endif %}
    {% if top30_stats %}
    <tbody id="stats-body-top30grow">
      {{ stats_block("Top 30 Fastest Growing Companies", top30_stats) }}
    </tbody>
    <tbody>
      <tr class="chart-row"><td colspan="12"><div class="chart-pair"><div class="chart-box"><canvas id="chart-top30grow-median"></canvas></div><div class="chart-box"><canvas id="chart-top30grow-average"></canvas></div></div></td></tr>
    </tbody>
    {% endif %}
    {% if top30_profitable_stats %}
    <tbody id="stats-body-top30prof">
      {{ stats_block("Top 30 Most Profitable Companies", top30_profitable_stats) }}
    </tbody>
    <tbody>
      <tr class="chart-row"><td colspan="12"><div class="chart-pair"><div class="chart-box"><canvas id="chart-top30prof-median"></canvas></div><div class="chart-box"><canvas id="chart-top30prof-average"></canvas></div></div></td></tr>
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

  function applySort() {
    if (sortCol === -1) return;
    const tbody = document.getElementById("ticker-rows");
    const rows  = Array.from(tbody.querySelectorAll("tr"));
    const col = sortCol;
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

  function sortTable(col) {
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    applySort();
  }

  function triggerRefresh() {
    const btn = document.getElementById("refresh-btn");
    const status = document.getElementById("refresh-status");
    btn.disabled = true;
    btn.querySelector("svg").classList.add("spin");
    status.textContent = "Fetching data...";
    fetch("/api/refresh", { method: "POST" })
      .then(r => r.json())
      .then(() => { status.textContent = "Fetching..."; pollForUpdate(); })
      .catch(() => { status.textContent = "Error."; btn.disabled = false; btn.querySelector("svg").classList.remove("spin"); });
  }

  function pollForUpdate() {
    const start = Date.now();
    const interval = setInterval(() => {
      fetch("/api/status").then(r => r.json()).then(data => {
        if (data.last_updated && new Date(data.last_updated).getTime() > start - 2000) {
          clearInterval(interval);
          applyTableUpdate();
        }
      });
    }, 3000);
    setTimeout(() => clearInterval(interval), 180000);
  }

  // Patches the ticker table and summary-stat numbers in place. Deliberately
  // does NOT touch the chart canvases — those only move once a day when
  // history.csv gets a new row, not on every manual refresh.
  async function applyTableUpdate() {
    const btn = document.getElementById("refresh-btn");
    const status = document.getElementById("refresh-status");
    const tickerRows = document.getElementById("ticker-rows");
    if (!tickerRows) { location.reload(); return; }  // first-ever load had no data yet
    try {
      const data = await fetch("/api/table").then(r => r.json());
      // If a section had no data on initial load, its tbody was never rendered.
      // Don't silently drop newly-appeared data — fall back to a full reload.
      const setBody = (id, html) => {
        const el = document.getElementById(id);
        if (el) { el.innerHTML = html; return true; }
        return !html;
      };
      const allPresent = [
        setBody("stats-body-all", data.stats_html),
        setBody("stats-body-above", data.above_html),
        setBody("stats-body-top30grow", data.top30_html),
        setBody("stats-body-top30prof", data.top30_profitable_html),
      ].every(Boolean);
      if (!allPresent) { location.reload(); return; }
      tickerRows.innerHTML = data.rows_html;
      document.getElementById("last-updated").textContent = data.last_updated;
      document.getElementById("ticker-count-label").textContent =
        `${data.ticker_count} ticker${data.ticker_count !== 1 ? "s" : ""}`;
      applySort();
      status.textContent = "Updated.";
    } catch (e) {
      status.textContent = "Error.";
    } finally {
      btn.disabled = false;
      btn.querySelector("svg").classList.remove("spin");
      setTimeout(() => { status.textContent = ""; }, 4000);
    }
  }

  const CHART_DEFS = [
    { id: "chart-all-median",        section: "All Companies",          stat: "Median",  color: "#58a6ff" },
    { id: "chart-all-average",       section: "All Companies",          stat: "Average", color: "#d2a8ff" },
    { id: "chart-above-median",      section: "Above Median Growth",    stat: "Median",  color: "#58a6ff" },
    { id: "chart-above-average",     section: "Above Median Growth",    stat: "Average", color: "#d2a8ff" },
    { id: "chart-top30grow-median",  section: "Top 30 Fastest Growing", stat: "Median",  color: "#58a6ff" },
    { id: "chart-top30grow-average", section: "Top 30 Fastest Growing", stat: "Average", color: "#d2a8ff" },
    { id: "chart-top30prof-median",  section: "Top 30 Most Profitable", stat: "Median",  color: "#58a6ff" },
    { id: "chart-top30prof-average", section: "Top 30 Most Profitable", stat: "Average", color: "#d2a8ff" },
  ];
  const chartInstances = {};
  const CHART_DEFAULTS = {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      title: { display: true, color: "#8b949e", font: { size: 11, weight: "600" }, padding: { bottom: 8 } },
      tooltip: { callbacks: { label: ctx => " " + ctx.parsed.y.toFixed(1) + "x" } }
    },
    scales: {
      x: { ticks: { color: "#8b949e", font: { size: 10 }, maxRotation: 0 }, grid: { color: "#21262d" } },
      y: { ticks: { color: "#8b949e", font: { size: 10 }, callback: v => v.toFixed(1) + "x" }, grid: { color: "#21262d" } }
    }
  };

  function chartOptions(title) {
    return {
      ...CHART_DEFAULTS,
      plugins: { ...CHART_DEFAULTS.plugins, title: { ...CHART_DEFAULTS.plugins.title, text: title } },
    };
  }

  function showPlaceholder(id) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    const row = canvas.closest("tr");
    if (row) row.style.display = "none";
  }

  async function renderCharts() {
    let rows;
    try { rows = await fetch("/api/history").then(r => r.json()); }
    catch(e) { CHART_DEFS.forEach(c => showPlaceholder(c.id)); return; }
    if (!rows.length) { CHART_DEFS.forEach(c => showPlaceholder(c.id)); return; }

    for (const { id, section, stat, color } of CHART_DEFS) {
      const canvas = document.getElementById(id);
      if (!canvas) continue;
      const sRows = rows.filter(r => r.Section === section && r.Type === stat);
      const dates = [...new Set(sRows.map(r => r.Date))].sort();
      if (!dates.length) { showPlaceholder(id); continue; }

      const get = (date) => {
        const r = sRows.find(r => r.Date === date);
        return r ? (parseFloat(r["Rev Multiple (x)"]) || null) : null;
      };

      const values = dates.map(get);

      if (chartInstances[id]) chartInstances[id].destroy();
      chartInstances[id] = new Chart(canvas, {
        type: "line",
        data: {
          labels: dates,
          datasets: [
            { label: stat, data: values, borderColor: color, backgroundColor: color + "14", fill: true, tension: 0.3, pointRadius: 3 },
          ]
        },
        options: chartOptions(stat)
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
    return render_template_string(HTML_TEMPLATE, **compute_table_context())


@app.route("/api/table")
def api_table():
    """Return freshly rendered table/stat HTML for the "Refresh Now" button.

    Deliberately excludes the charts — those only move once a day when
    history.csv gets a new row, so a manual refresh must update the live
    numbers without re-rendering (and re-animating) the chart canvases.
    """
    ctx = compute_table_context()
    return jsonify({
        "last_updated": ctx["last_updated"],
        "ticker_count": ctx["ticker_count"],
        "rows_html": render_fragment(
            "{% for row in rows %}{{ ticker_row(row) }}{% endfor %}", rows=ctx["rows"]
        ),
        "stats_html": render_fragment(
            '{{ stats_block("Summary Statistics", stats) }}', stats=ctx["stats"]
        ) if ctx["stats"] else "",
        "above_html": render_fragment(
            '{{ stats_block("Above Median Growth Companies", above_stats) }}', above_stats=ctx["above_stats"]
        ) if ctx["above_stats"] else "",
        "top30_html": render_fragment(
            '{{ stats_block("Top 30 Fastest Growing Companies", top30_stats) }}', top30_stats=ctx["top30_stats"]
        ) if ctx["top30_stats"] else "",
        "top30_profitable_html": render_fragment(
            '{{ stats_block("Top 30 Most Profitable Companies", top30_profitable_stats) }}',
            top30_profitable_stats=ctx["top30_profitable_stats"],
        ) if ctx["top30_profitable_stats"] else "",
    })


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

def _startup_refresh_and_backfill() -> None:
    if not _cache.get("data"):
        refresh_data()
    backfill_history()


load_cache()
threading.Thread(target=_startup_refresh_and_backfill, daemon=True).start()

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_refresh, "cron", hour=16, minute=30, id="daily_refresh", timezone="America/New_York")
scheduler.start()

if __name__ == "__main__":
    print("Starting Yahoo Finance Dashboard at http://localhost:5000")
    print("Data refreshes daily at 4:30 PM. Press Ctrl+C to stop.\n")
    app.run(debug=False, port=5000, use_reloader=False)
