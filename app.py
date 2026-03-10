#!/usr/bin/env python3
"""
Yahoo Finance Stock Tracker
Tracks market cap, LTM revenue, EBITDA, operating cash flow, debt, and cash
for a watchlist of tickers.
"""

import json
import os
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

        name = info.get("shortName") or info.get("longName") or ticker

        return {
            "ticker": ticker.upper(),
            "name": name,
            "market_cap": market_cap,
            "revenue": revenue,
            "ebitda": ebitda,
            "ocf": ocf,
            "debt": debt,
            "cash": cash,
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

    table.add_column("Ticker", style="bold cyan", no_wrap=True)
    table.add_column("Company", style="white", max_width=28)
    table.add_column("Mkt Cap", justify="right", style="green")
    table.add_column("LTM Rev", justify="right", style="green")
    table.add_column("LTM EBITDA", justify="right", style="green")
    table.add_column("LTM Op CF", justify="right", style="green")
    table.add_column("Debt", justify="right", style="red")
    table.add_column("Cash", justify="right", style="bright_green")

    for d in data:
        if d.get("error"):
            table.add_row(
                d["ticker"], f"[red]Error: {d['error'][:40]}[/]",
                "—", "—", "—", "—", "—", "—",
            )
        else:
            table.add_row(
                d["ticker"],
                d["name"],
                fmt(d["market_cap"]),
                fmt(d["revenue"]),
                fmt(d["ebitda"]),
                fmt(d["ocf"]),
                fmt(d["debt"]),
                fmt(d["cash"]),
            )

    console.print(table)


# ── Main loop ─────────────────────────────────────────────────────────────────

HELP = """
[bold]Commands:[/bold]
  [cyan]add[/cyan]     <TICKER> [TICKER …]  — Add tickers to watchlist
  [cyan]remove[/cyan]  <TICKER> [TICKER …]  — Remove tickers from watchlist
  [cyan]refresh[/cyan]                      — Re-fetch data for all tickers
  [cyan]list[/cyan]                         — Show current watchlist
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

        else:
            console.print(f"[red]Unknown command:[/red] [bold]{cmd}[/bold]. Type [cyan]help[/cyan] for options.")


if __name__ == "__main__":
    main()
