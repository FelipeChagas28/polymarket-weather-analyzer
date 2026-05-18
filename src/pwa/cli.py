from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.table import Table

from pwa.polymarket.gamma import GammaClient, event_markets

app = typer.Typer(add_completion=False, help="Polymarket Weather Analyzer")
console = Console()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@app.command("list")
def list_cmd(
    days: int = typer.Option(7, "--days", "-d", help="Janela em dias à frente para incluir eventos"),
    show_closed: bool = typer.Option(False, "--all", help="Inclui eventos já fechados (marcados como closed pela Polymarket)"),
) -> None:
    """Lista eventos ativos de temperatura diária na Polymarket."""
    now = datetime.now(timezone.utc)
    with GammaClient() as gamma:
        events = gamma.list_temperature_events()

    rows = []
    for ev in events:
        if not show_closed and ev.get("closed"):
            continue
        end = _parse_iso(ev.get("endDate"))
        if end is None:
            continue
        delta_days = (end - now).total_seconds() / 86400
        if delta_days < -1 or delta_days > days:
            continue
        rows.append((ev, end, delta_days))

    rows.sort(key=lambda r: r[1])

    table = Table(title=f"Mercados de temperatura ativos (próximos {days}d)", show_lines=False)
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("Slug", style="white", overflow="fold")
    table.add_column("Cidade / Pergunta", style="white")
    table.add_column("Resolução (UTC)", style="yellow")
    table.add_column("h restantes", justify="right", style="magenta")
    table.add_column("# bins", justify="right")
    table.add_column("Volume", justify="right", style="green")

    for ev, end, dd in rows:
        markets = list(event_markets(ev))
        vol = float(ev.get("volume") or 0)
        hours = dd * 24
        table.add_row(
            str(ev.get("id")),
            ev.get("slug", ""),
            ev.get("title", "")[:60],
            end.strftime("%Y-%m-%d %H:%M"),
            f"{hours:+.1f}",
            str(len(markets)),
            f"${vol:,.0f}",
        )

    if not rows:
        console.print("[yellow]Nenhum evento de temperatura encontrado na janela.[/yellow]")
        console.print("[dim]Tente aumentar --days ou usar --all para incluir closed.[/dim]")
        return

    console.print(table)
    console.print(f"[dim]Total: {len(rows)} eventos[/dim]")


@app.command("analyze")
def analyze_cmd(
    event: str = typer.Argument(..., help="ID numérico ou slug do evento"),
) -> None:
    """Análise estatística completa de um evento de temperatura. (NOT YET IMPLEMENTED)"""
    console.print(f"[yellow]TODO: implementar pipeline analítico para {event}[/yellow]")


if __name__ == "__main__":
    app()
