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

from flask import Flask, jsonify, render_template, request
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

    return render_template(
        "index.html",
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
