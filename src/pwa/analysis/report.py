"""Rich-formatted analysis report for a temperature event."""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pwa.analysis.edge import EdgeRow
from pwa.models.bias import BiasReport
from pwa.weather.open_meteo import EnsembleResult
from pwa.weather.stations import Station


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    event_title: str
    event_slug: str
    station: Station
    ensemble: EnsembleResult
    bias: BiasReport | None
    unit: str = "F"  # "F" or "C"


REC_STYLE = {
    "STRONG BUY": "bold green",
    "BUY": "green",
    "SKIP": "dim",
}


def render(ctx: AnalysisContext, rows: list[EdgeRow], console: Console | None = None) -> None:
    console = console or Console()

    members = ctx.ensemble.members_daily
    u = "°C" if ctx.unit.upper() == "C" else "°F"
    url = f"https://polymarket.com/event/{ctx.event_slug}" if ctx.event_slug else ""
    header_lines = [
        f"[bold]{ctx.event_title}[/bold]",
        f"link: [link={url}]{url}[/link]" if url else "link: [dim]n/a[/dim]",
        f"resolution: [yellow]{ctx.station.resolution_station}[/yellow]  ({ctx.station.display_name})",
        f"target date: [yellow]{ctx.ensemble.target_date.isoformat()}[/yellow]  tz={ctx.station.tz}",
        f"ensemble: [magenta]{ctx.ensemble.n_members}[/magenta] members  "
        f"mean={members.mean():.2f}{u}  std={members.std(ddof=1):.2f}{u}  "
        f"min={members.min():.1f}{u}  max={members.max():.1f}{u}",
    ]
    if ctx.bias is not None and ctx.bias.n_days > 0:
        header_lines.append(
            f"bias correction: applied [magenta]{ctx.bias.mean_bias:+.2f}{u}[/magenta] "
            f"(based on {ctx.bias.n_days}d; residual sd={ctx.bias.std_residual:.2f}{u})"
        )
    else:
        header_lines.append("bias correction: [dim]not available[/dim]")

    console.print(Panel("\n".join(header_lines), title="Event", border_style="cyan"))

    table = Table(show_lines=False, title="Per-bin analysis (best side YES vs NO)")
    table.add_column("Bin", style="white")
    table.add_column("P(model)", justify="right", style="cyan")
    table.add_column("YES ask", justify="right", style="dim")
    table.add_column("YES bid", justify="right", style="dim")
    table.add_column("Side", justify="center")
    table.add_column("Price", justify="right")
    table.add_column("Edge (pp)", justify="right", style="magenta")
    table.add_column("EV", justify="right")
    table.add_column("Kelly (capped)", justify="right", style="yellow")
    table.add_column("Recommendation", justify="center")

    for r in rows:
        ya = f"{r.yes_ask:.3f}" if r.yes_ask is not None else "—"
        yb = f"{r.yes_bid:.3f}" if r.yes_bid is not None else "—"
        side_style = "green" if r.side == "YES" else "blue"
        price_s = f"{r.side_price:.3f}" if r.side_price is not None else "—"
        edge_s = f"{r.edge*100:+.1f}" if r.edge is not None else "—"
        ev_s = f"{r.ev:+.3f}" if r.ev is not None else "—"
        kelly_s = f"{r.kelly.capped*100:.2f}%" if r.kelly is not None else "—"
        style = REC_STYLE.get(r.recommendation, "white")
        table.add_row(
            r.bin.label,
            f"{r.p_model*100:.1f}%",
            ya,
            yb,
            f"[{side_style}]{r.side}[/{side_style}]",
            price_s,
            edge_s,
            ev_s,
            kelly_s,
            f"[{style}]{r.recommendation}[/{style}]",
        )

    console.print(table)

    buys = [r for r in rows if r.recommendation in ("BUY", "STRONG BUY")]
    if buys:
        yes_count = sum(1 for r in buys if r.side == "YES")
        no_count = sum(1 for r in buys if r.side == "NO")
        console.print(
            f"[bold green]{len(buys)}[/bold green] bin(s) recomendado(s) "
            f"([green]YES={yes_count}[/green] · [blue]NO={no_count}[/blue]). "
            f"Tamanho total Kelly sugerido: "
            f"[yellow]{sum(r.kelly.capped for r in buys if r.kelly)*100:.2f}%[/yellow] do bankroll."
        )
    else:
        console.print("[dim]Nenhum bin atinge o threshold de edge — SKIP geral.[/dim]")
    console.print("[dim italic]Lembre-se: o sistema só recomenda, você decide e aposta manualmente.[/dim italic]")
