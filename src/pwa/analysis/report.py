"""Rich-formatted analysis report for a temperature event."""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pwa.analysis.consensus import ConsensusRow
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

AGREEMENT_STYLE = {
    "strong": "green",
    "moderate": "yellow",
    "weak": "red",
}


def _short_label(name: str) -> str:
    aliases = {
        "open-meteo-ensemble": "OM-ens",
        "ecmwf_ifs025": "ECMWF",
        "gfs_seamless": "GFS",
        "icon_seamless": "ICON",
        "jma_seamless": "JMA",
        "kma_seamless": "KMA",
        "yr-no": "yr.no",
    }
    return aliases.get(name, name[:8])


def _render_consensus(console: Console, consensus_rows: list[ConsensusRow]) -> None:
    if not consensus_rows:
        return
    sample_row = consensus_rows[0]
    source_names = list(sample_row.per_source_prob.keys())

    table = Table(show_lines=False, title="Consenso entre fontes (P(YES) por fonte)")
    table.add_column("Bin", style="white")
    for name in source_names:
        table.add_column(_short_label(name), justify="right", style="dim")
    table.add_column("Consenso", justify="right", style="bold cyan")
    table.add_column("Spread", justify="right", style="magenta")
    table.add_column("Concord.", justify="center")
    table.add_column("Lado", justify="center")

    for row in consensus_rows:
        cells = [row.bin.label]
        for name in source_names:
            p = row.per_source_prob.get(name)
            cells.append(f"{p*100:.0f}%" if p is not None else "—")
        cells.append(f"{row.consensus_prob*100:.1f}%")
        cells.append(f"{row.spread_pp:.0f}pp")
        agr_style = AGREEMENT_STYLE.get(row.agreement, "white")
        cells.append(f"[{agr_style}]{row.agreement}[/{agr_style}]")
        side_label = row.side
        if row.conflicts_with_primary:
            side_label = f"{row.side} (!)"
            side_style = "bold red"
        elif row.side == "YES":
            side_style = "green"
        elif row.side == "NO":
            side_style = "blue"
        else:
            side_style = "dim"
        cells.append(f"[{side_style}]{side_label}[/{side_style}]")
        table.add_row(*cells)

    console.print(table)
    console.print(
        "[dim]Leitura: cada coluna de fonte mostra P(YES) — a chance do bin acontecer segundo aquela fonte. "
        "[green]strong[/green]=fontes concordam · [yellow]moderate[/yellow]=divergência razoável · "
        "[red]weak[/red]=fontes discordam muito. (!) marca conflito entre lado do consenso e lado da recomendação primária.[/dim]"
    )


def render(
    ctx: AnalysisContext,
    rows: list[EdgeRow],
    consensus_rows: list[ConsensusRow] | None = None,
    console: Console | None = None,
) -> None:
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

    if consensus_rows:
        _render_consensus(console, consensus_rows)

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
        if consensus_rows:
            consensus_by_label = {c.bin.label: c for c in consensus_rows}
            confirmed: list[str] = []
            conflicted: list[str] = []
            weak: list[str] = []
            for r in buys:
                c = consensus_by_label.get(r.bin.label)
                if c is None:
                    continue
                if c.conflicts_with_primary:
                    conflicted.append(f"{r.bin.label} ({r.side})")
                elif c.agreement == "weak":
                    weak.append(f"{r.bin.label} ({r.side})")
                elif c.agreement == "strong" and c.side == r.side:
                    confirmed.append(f"{r.bin.label} ({r.side})")
            if confirmed:
                console.print(
                    f"[green][OK] Confirmadas por consenso forte:[/green] {', '.join(confirmed)}"
                )
            if weak:
                console.print(
                    f"[yellow](!) Concordância fraca entre fontes — cautela:[/yellow] {', '.join(weak)}"
                )
            if conflicted:
                console.print(
                    f"[bold red](!) Conflito direto com consenso (apenas Open-Meteo apoia):[/bold red] {', '.join(conflicted)}"
                )
    else:
        console.print("[dim]Nenhum bin atinge o threshold de edge — SKIP geral.[/dim]")
    console.print("[dim italic]Lembre-se: o sistema só recomenda, você decide e aposta manualmente.[/dim italic]")
