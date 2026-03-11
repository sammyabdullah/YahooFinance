#!/usr/bin/env python3
"""
Yahoo Finance Stock Tracker
Tracks market cap, LTM revenue, EBITDA, operating cash flow, debt, and cash
for a watchlist of tickers.
"""

import csv
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

import yfinance as yf
from rich.console import Console
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.panel import Panel
from rich.text import Text

TICKERS_FILE = Path("tickers.json")
console = Console()


# ── Persistence ──────────────────────────────────────────────────────────────

def load_tickers() -> list[str]:
    if TICKERS_FILE.exists():
        return json.loads(TICKERS_FILE.read_text())
    return []


def save_tickers(tickers: list[str]) -> None:
    TICKERS_FILE.write_text(json.dumps(tickers))


# ── Data fetching ─────────────────────────────────────────────────────────────

def fmt(value, prefix="$", scale=1e9, decimals=1, suffix="B") -> str:
    """Format a numeric value for display."""
    if value is None:
        return "—"
    try:
        v = float(value) / scale
        return f"{prefix}{v:,.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def fmt_pct(value, decimals=1) -> str:
    """Format a ratio (0.15) as a signed percentage (+15.0%)."""
    if value is None:
        return "—"
    try:
        return f"{float(value)*100:+.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def fetch_ticker_data(ticker: str) -> dict:
    """Fetch all required metrics for a single ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.info

        # Market cap
        market_cap = info.get("marketCap")

        # LTM Revenue — prefer trailing, fall back to annual
        revenue = info.get("totalRevenue")

        # LTM EBITDA
        ebitda = info.get("ebitda")

        # LTM Operating Cash Flow — from cash flow statement
        ocf = None
        try:
            cf = t.cashflow  # columns = periods, index = line items
            if cf is not None and not cf.empty:
                ocf_row = None
                for label in cf.index:
                    if "operating" in str(label).lower() and "cash" in str(label).lower():
                        ocf_row = label
                        break
                if ocf_row is not None:
                    ocf = float(cf.loc[ocf_row].iloc[0])
        except Exception:
            pass

        # Debt (total debt)
        debt = info.get("totalDebt")

        # Cash (total cash)
        cash = info.get("totalCash")

        # YoY Revenue Growth (trailing)
        rev_growth = info.get("revenueGrowth")

        name = info.get("shortName") or info.get("longName") or ticker

        # Enterprise Value = Market Cap + Debt - Cash
        if market_cap is not None and debt is not None and cash is not None:
            ev = market_cap + debt - cash
        elif market_cap is not None:
            ev = market_cap + (debt or 0) - (cash or 0)
        else:
            ev = None

        # Revenue Multiple = EV / LTM Revenue
        rev_multiple = (ev / revenue) if (ev is not None and revenue) else None

        return {
            "ticker": ticker.upper(),
            "name": name,
            "market_cap": market_cap,
            "revenue": revenue,
            "rev_growth": rev_growth,
            "ebitda": ebitda,
            "ocf": ocf,
            "debt": debt,
            "cash": cash,
            "ev": ev,
            "rev_multiple": rev_multiple,
            "error": None,
        }
    except Exception as e:
        return {"ticker": ticker.upper(), "name": "—", "error": str(e)}


def fetch_all(tickers: list[str]) -> list[dict]:
    results = []
    with console.status("[bold cyan]Fetching data from Yahoo Finance…[/]"):
        for ticker in tickers:
            results.append(fetch_ticker_data(ticker))
    return results


# ── Summary stats ─────────────────────────────────────────────────────────────

NUMERIC_KEYS = ["market_cap", "revenue", "rev_growth", "ebitda", "ocf", "debt", "cash", "ev", "rev_multiple"]


def _agg(subset: list[dict]) -> dict:
    """Return median and mean for each numeric key over a subset of rows."""
    result = {}
    for k in NUMERIC_KEYS:
        vals = [d[k] for d in subset if d.get(k) is not None]
        result[k] = {
            "median": statistics.median(vals) if vals else None,
            "mean":   statistics.mean(vals)   if vals else None,
        }
    return result


def summary_stats(data: list[dict]):
    """Return (all_agg, above_agg, median_growth, n_above) for valid rows."""
    valid = [d for d in data if not d.get("error")]
    all_agg = _agg(valid)
    median_growth = all_agg["rev_growth"]["median"]
    if median_growth is not None:
        above = [d for d in valid if d.get("rev_growth") is not None and d["rev_growth"] > median_growth]
    else:
        above = []
    return all_agg, _agg(above), median_growth, len(above)


# ── Display ───────────────────────────────────────────────────────────────────

def render_table(data: list[dict]) -> None:
    if not data:
        console.print("[yellow]No tickers in watchlist. Add some with [bold]add[/bold].[/]")
        return

    table = Table(
        box=box.ROUNDED,
        header_style="bold white on dark_blue",
        show_lines=True,
        title=f"[bold]Yahoo Finance Watchlist[/bold]  [dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
        title_justify="left",
    )

    table.add_column("Company",     style="white",       max_width=28)
    table.add_column("Ticker",      style="bold cyan",   no_wrap=True)
    table.add_column("Mkt Cap",     justify="right",     style="green")
    table.add_column("LTM Rev",     justify="right",     style="green")
    table.add_column("Rev Growth",  justify="right",     style="yellow")
    table.add_column("LTM EBITDA",  justify="right",     style="green")
    table.add_column("LTM Op CF",   justify="right",     style="green")
    table.add_column("Debt",        justify="right",     style="red")
    table.add_column("Cash",        justify="right",     style="bright_green")
    table.add_column("Ent. Value",  justify="right",     style="green")
    table.add_column("Rev Mult.",   justify="right",     style="magenta")

    for i, d in enumerate(data):
        last = (i == len(data) - 1)
        if d.get("error"):
            table.add_row(
                f"[red]Error: {d['error'][:40]}[/]", d["ticker"],
                "—", "—", "—", "—", "—", "—", "—", "—", "—",
                end_section=last,
            )
        else:
            rev_mult = d.get("rev_multiple")
            table.add_row(
                d["name"], d["ticker"],
                fmt(d["market_cap"]),
                fmt(d["revenue"]),
                fmt_pct(d.get("rev_growth")),
                fmt(d["ebitda"]),
                fmt(d["ocf"]),
                fmt(d["debt"]),
                fmt(d["cash"]),
                fmt(d.get("ev")),
                f"{rev_mult:.1f}x" if rev_mult is not None else "—",
                end_section=last,
            )

    valid = [d for d in data if not d.get("error")]
    if valid:
        all_agg, above_agg, _, n_above = summary_stats(data)

        def stat_row(label: str, agg: dict, stat: str) -> list:
            g = agg["rev_growth"][stat]
            rm = agg["rev_multiple"][stat]
            return [
                f"[dim italic]{label}[/]", "",
                fmt(agg["market_cap"][stat]),
                fmt(agg["revenue"][stat]),
                fmt_pct(g) if g is not None else "—",
                fmt(agg["ebitda"][stat]),
                fmt(agg["ocf"][stat]),
                fmt(agg["debt"][stat]),
                fmt(agg["cash"][stat]),
                fmt(agg["ev"][stat]),
                f"{rm:.1f}x" if rm is not None else "—",
            ]

        table.add_row(*stat_row("All — Median", all_agg, "median"))
        table.add_row(*stat_row("All — Average", all_agg, "mean"), end_section=True)
        table.add_row(*stat_row(f"Above Median Growth — Median  (n={n_above})", above_agg, "median"))
        table.add_row(*stat_row(f"Above Median Growth — Average (n={n_above})", above_agg, "mean"))

    console.print(table)


# ── Export ────────────────────────────────────────────────────────────────────

def export_csv(data: list[dict], path: Path) -> None:
    """Write current data (plus summary rows) to a CSV file."""
    fields = [
        "Company", "Ticker",
        "Market Cap ($)", "LTM Revenue ($)", "Rev Growth (YoY)",
        "LTM EBITDA ($)", "LTM Op Cash Flow ($)", "Total Debt ($)", "Cash ($)",
        "Enterprise Value ($)", "Revenue Multiple",
        "As of",
    ]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def raw(value) -> str:
        if value is None:
            return ""
        try:
            return str(round(float(value)))
        except (TypeError, ValueError):
            return ""

    def pct(value) -> str:
        if value is None:
            return ""
        try:
            return f"{float(value)*100:+.2f}%"
        except (TypeError, ValueError):
            return ""

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for d in data:
            if d.get("error"):
                writer.writerow({"Company": f"ERROR: {d['error']}", "Ticker": d["ticker"]})
            else:
                rev_mult = d.get("rev_multiple")
                writer.writerow({
                    "Company": d["name"],
                    "Ticker": d["ticker"],
                    "Market Cap ($)": raw(d["market_cap"]),
                    "LTM Revenue ($)": raw(d["revenue"]),
                    "Rev Growth (YoY)": pct(d.get("rev_growth")),
                    "LTM EBITDA ($)": raw(d["ebitda"]),
                    "LTM Op Cash Flow ($)": raw(d["ocf"]),
                    "Total Debt ($)": raw(d["debt"]),
                    "Cash ($)": raw(d["cash"]),
                    "Enterprise Value ($)": raw(d.get("ev")),
                    "Revenue Multiple": f"{rev_mult:.2f}x" if rev_mult is not None else "",
                    "As of": timestamp,
                })

        valid = [d for d in data if not d.get("error")]
        if valid:
            all_agg, above_agg, _, n_above = summary_stats(data)
            writer.writerow({})  # blank separator

            def stat_csv_row(label: str, agg: dict, stat: str) -> dict:
                rm = agg["rev_multiple"][stat]
                return {
                    "Company": label,
                    "Market Cap ($)": raw(agg["market_cap"][stat]),
                    "LTM Revenue ($)": raw(agg["revenue"][stat]),
                    "Rev Growth (YoY)": pct(agg["rev_growth"][stat]),
                    "LTM EBITDA ($)": raw(agg["ebitda"][stat]),
                    "LTM Op Cash Flow ($)": raw(agg["ocf"][stat]),
                    "Total Debt ($)": raw(agg["debt"][stat]),
                    "Cash ($)": raw(agg["cash"][stat]),
                    "Enterprise Value ($)": raw(agg["ev"][stat]),
                    "Revenue Multiple": f"{rm:.2f}x" if rm is not None else "",
                }

            writer.writerow(stat_csv_row("All — Median", all_agg, "median"))
            writer.writerow(stat_csv_row("All — Average", all_agg, "mean"))
            writer.writerow({})
            writer.writerow(stat_csv_row(f"Above Median Growth — Median (n={n_above})", above_agg, "median"))
            writer.writerow(stat_csv_row(f"Above Median Growth — Average (n={n_above})", above_agg, "mean"))


# ── Main loop ─────────────────────────────────────────────────────────────────

HELP = """
[bold]Commands:[/bold]
  [cyan]add[/cyan]     <TICKER> [TICKER …]  — Add tickers to watchlist
  [cyan]remove[/cyan]  <TICKER> [TICKER …]  — Remove tickers from watchlist
  [cyan]refresh[/cyan]                      — Re-fetch data for all tickers
  [cyan]list[/cyan]                         — Show current watchlist
  [cyan]export[/cyan]  [filename.csv]       — Export data to CSV (opens in Excel)
  [cyan]help[/cyan]                         — Show this message
  [cyan]quit[/cyan]                         — Exit
"""


def main() -> None:
    console.print(Panel.fit(
        "[bold cyan]Yahoo Finance Stock Tracker[/bold cyan]\n"
        "[dim]Tracks market cap, LTM revenue, EBITDA, operating cash flow, debt & cash[/dim]",
        border_style="cyan",
    ))
    console.print(HELP)

    tickers = load_tickers()
    last_data: list[dict] = []

    if tickers:
        last_data = fetch_all(tickers)
        render_table(last_data)

    while True:
        try:
            raw = Prompt.ask("\n[bold green]>[/bold green]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye![/dim]")
            sys.exit(0)

        if not raw:
            continue

        parts = raw.split()
        cmd, args = parts[0].lower(), [a.upper() for a in parts[1:]]

        if cmd in ("quit", "exit", "q"):
            console.print("[dim]Bye![/dim]")
            sys.exit(0)

        elif cmd == "help":
            console.print(HELP)

        elif cmd == "list":
            if tickers:
                console.print("[bold]Current watchlist:[/bold] " + "  ".join(f"[cyan]{t}[/cyan]" for t in tickers))
            else:
                console.print("[yellow]Watchlist is empty.[/yellow]")

        elif cmd == "add":
            if not args:
                console.print("[red]Usage: add <TICKER> [TICKER …][/red]")
                continue
            added = []
            for t in args:
                if t not in tickers:
                    tickers.append(t)
                    added.append(t)
            if added:
                save_tickers(tickers)
                console.print(f"[green]Added:[/green] {', '.join(added)}")
                new_data = fetch_all(added)
                last_data = [d for d in last_data if d["ticker"] not in added] + new_data
                render_table(last_data)
            else:
                console.print("[yellow]All tickers already in watchlist.[/yellow]")

        elif cmd == "remove":
            if not args:
                console.print("[red]Usage: remove <TICKER> [TICKER …][/red]")
                continue
            removed = []
            for t in args:
                if t in tickers:
                    tickers.remove(t)
                    removed.append(t)
            if removed:
                save_tickers(tickers)
                last_data = [d for d in last_data if d["ticker"] not in removed]
                console.print(f"[yellow]Removed:[/yellow] {', '.join(removed)}")
                render_table(last_data)
            else:
                console.print("[yellow]None of those tickers were in the watchlist.[/yellow]")

        elif cmd == "refresh":
            if not tickers:
                console.print("[yellow]Watchlist is empty.[/yellow]")
            else:
                last_data = fetch_all(tickers)
                render_table(last_data)

        elif cmd == "export":
            if not last_data:
                console.print("[yellow]No data to export. Run [bold]refresh[/bold] first.[/yellow]")
                continue
            filename = args[0] if args else f"watchlist_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            if not filename.lower().endswith(".csv"):
                filename += ".csv"
            out = Path(filename)
            export_csv(last_data, out)
            console.print(f"[green]Exported to:[/green] [bold]{out.resolve()}[/bold]")

        else:
            console.print(f"[red]Unknown command:[/red] [bold]{cmd}[/bold]. Type [cyan]help[/cyan] for options.")


if __name__ == "__main__":
    main()
